/**
 * The stacks describe the system as built.
 * Synthesized as CI does, with placeholder config and no AWS credentials.
 */
import { AwsSolutionsChecks } from 'cdk-nag';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { PROMQL_RULES } from '../lib/alarms';
import { COLLECTOR_IMAGE } from '../lib/collector';
import { estimate, markdown } from '../lib/cost';
import { SERVICES } from '../lib/infra-stack';
import { TEI_IMAGE } from '../lib/services-stack';
import {
    describe as resourceName,
    grants,
    resources,
    synthesize,
    target,
    taskDefinition
} from './support';

const stacks = synthesize();
const { infraTemplate: infra, servicesTemplate: services } = stacks;
const REPOSITORY = join(__dirname, '..', '..', '..');

const containers = (service: string): any[] =>
    taskDefinition(services, service)[1].Properties.ContainerDefinitions;
const app = (service: string): any => containers(service).find((c) => c.Name === service);
const roleOf = (service: string, role: 'TaskRoleArn' | 'ExecutionRoleArn'): string =>
    target(taskDefinition(services, service)[1].Properties[role])!;

describe('every service, with its image, CPU and memory', () => {
    const EXPECTED: Record<string, { image: string; cpu: string; memory: string; arch: string }> = {
        api: { image: 'apiRepo', cpu: '512', memory: '1024', arch: 'ARM64' },
        frontend: { image: 'frontendRepo', cpu: '256', memory: '512', arch: 'ARM64' },
        'ai-service': { image: 'aiserviceRepo', cpu: '512', memory: '2048', arch: 'ARM64' },
        'ingestion-worker': {
            image: 'ingestionworkerRepo',
            cpu: '2048',
            memory: '4096',
            arch: 'ARM64'
        },
        'tei-embed': { image: TEI_IMAGE, cpu: '512', memory: '2048', arch: 'ARM64' },
        // LiteLLM publishes the pinned version for amd64 only.
        'llm-gateway': { image: 'llmgatewayRepo', cpu: '512', memory: '1024', arch: 'X86_64' }
    };

    test('the six services and no others', () => {
        const names = Object.values(resources(services, 'AWS::ECS::Service')).map(
            (s) => s.Properties.ServiceName
        );
        expect(names.sort()).toEqual(SERVICES.map((s) => `documents-${s}`).sort());
        expect(Object.keys(EXPECTED).sort()).toEqual([...SERVICES].sort());
    });

    test.each(Object.entries(EXPECTED))('%s', (service, expected) => {
        const task = taskDefinition(services, service)[1].Properties;
        expect([task.Cpu, task.Memory]).toEqual([expected.cpu, expected.memory]);
        expect(task.RuntimePlatform.CpuArchitecture).toBe(expected.arch);
        const image = JSON.stringify(app(service).Image);
        if (expected.image.includes('/')) {
            expect(app(service).Image).toBe(expected.image);
        } else {
            // An image this repository builds, from its ECR repository, at the deploy's tag.
            expect(image).toContain(`ExportsOutputFnGetAtt${expected.image}`);
            expect(image).toContain(':synth');
        }
    });

    test('third-party images are pinned by digest', () => {
        expect(TEI_IMAGE).toMatch(/@sha256:[0-9a-f]{64}$/);
        expect(COLLECTOR_IMAGE).toMatch(/@sha256:[0-9a-f]{64}$/);
        const dockerfile = readFileSync(join(REPOSITORY, 'docker', 'Dockerfile.gateway'), 'utf8');
        const compose = readFileSync(join(REPOSITORY, 'docker-compose.yml'), 'utf8');
        const gateway = /FROM (ghcr\.io\/berriai\/litellm-database:\S+@sha256:[0-9a-f]{64})/.exec(
            dockerfile
        )?.[1];
        expect(gateway).toBeDefined();
        expect(compose).toContain(`image: ${gateway}`); // the same as locally
    });
});

describe('no load balancer listener for the ai-service', () => {
    test('the ai-service has no target group and is reached through Service Connect only', () => {
        const ai = Object.values(resources(services, 'AWS::ECS::Service')).find(
            (s) => s.Properties.ServiceName === 'documents-ai-service'
        )!;
        expect(ai.Properties.LoadBalancers).toBeUndefined();
        const ports = Object.values(resources(services, 'AWS::ElasticLoadBalancingV2::TargetGroup'))
            .map((g) => g.Properties.Port)
            .sort();
        expect(ports).toEqual([3000, 3001, 3001]);
        const served = ai.Properties.ServiceConnectConfiguration.Services[0];
        expect(served.ClientAliases[0]).toMatchObject({ DnsName: 'ai-service', Port: 8000 });
    });

    test('the listener routes the API, its WebSocket and the frontend', () => {
        const routes = Object.values(
            resources(services, 'AWS::ElasticLoadBalancingV2::ListenerRule')
        )
            .map((r) => r.Properties.Conditions[0].PathPatternConfig.Values[0])
            .sort();
        expect(routes).toEqual(['/*', '/api/*', '/socket.io/*']);
    });

    test('the load balancer is internal, behind CloudFront', () => {
        const albs = Object.values(resources(infra, 'AWS::ElasticLoadBalancingV2::LoadBalancer'));
        expect(albs).toHaveLength(1);
        expect(albs[0].Properties.Scheme).toBe('internal');
        const listeners = Object.values(resources(infra, 'AWS::ElasticLoadBalancingV2::Listener'));
        expect(listeners.map((l) => l.Properties.Port)).toEqual([80]);
        const distribution = Object.values(resources(infra, 'AWS::CloudFront::Distribution'))[0];
        const origins = distribution.Properties.DistributionConfig.Origins;
        expect(origins).toHaveLength(1);
        expect(origins[0].VpcOriginConfig).toBeDefined();
        expect(
            distribution.Properties.DistributionConfig.DefaultCacheBehavior.ViewerProtocolPolicy
        ).toBe('redirect-to-https');
    });

    test('each service accepts only its callers', () => {
        const groupOf = (service: string) => {
            const s = Object.values(resources(services, 'AWS::ECS::Service')).find(
                (x) => x.Properties.ServiceName === `documents-${service}`
            )!;
            return target(s.Properties.NetworkConfiguration.AwsvpcConfiguration.SecurityGroups[0]);
        };
        const rules = Object.values(resources(services, 'AWS::EC2::SecurityGroupIngress')).map(
            (r) => [
                resourceName(r.Properties.GroupId),
                resourceName(r.Properties.SourceSecurityGroupId),
                r.Properties.FromPort
            ]
        );
        const sg = Object.fromEntries(SERVICES.map((s) => [s, groupOf(s)]));
        expect(rules.sort()).toEqual(
            [
                [sg.api, 'AlbSg', 3001],
                [sg.frontend, 'AlbSg', 3000],
                [sg['ai-service'], sg.api, 8000],
                [sg['tei-embed'], sg['ai-service'], 80],
                [sg['tei-embed'], sg['ingestion-worker'], 80],
                [sg['llm-gateway'], sg['ai-service'], 4000],
                [sg['llm-gateway'], sg['ingestion-worker'], 4000],
                ['GatewayDbSecurityGroup', sg['llm-gateway'], 5432]
            ].sort()
        );
        const alb = Object.values(resources(infra, 'AWS::EC2::SecurityGroup')).find((g) =>
            g.Properties.GroupDescription.startsWith('Internal ALB')
        )!;
        expect(alb.Properties.SecurityGroupIngress).toEqual([
            expect.objectContaining({ CidrIp: '10.0.0.0/16', FromPort: 80, ToPort: 80 })
        ]);
    });
});

describe("each service's IAM role allows only the actions named here", () => {
    const TASK_GRANTS: Record<string, string[]> = {
        api: [
            's3:PutObject DocumentsBucket/uploads/*',
            's3:DeleteObject DocumentsBucket/uploads/*',
            's3:GetObject DocumentsBucket/uploads/*',
            's3:GetObject DocumentsBucket/thumbnails/*',
            's3:ListBucket DocumentsBucket',
            'dynamodb:GetItem DocumentsTable',
            'dynamodb:PutItem DocumentsTable',
            'dynamodb:UpdateItem DocumentsTable',
            'dynamodb:DeleteItem DocumentsTable',
            'dynamodb:Query DocumentsTable',
            'dynamodb:BatchWriteItem DocumentsTable',
            'sqs:ReceiveMessage ApiNotificationsQueue',
            'sqs:DeleteMessage ApiNotificationsQueue',
            'sqs:GetQueueAttributes ProcessingQueue',
            'cloudwatch:PutMetricData *'
        ],
        frontend: [],
        'ai-service': ['dynamodb:Query DocumentsTable', 'cloudwatch:PutMetricData *'],
        'ingestion-worker': [
            's3:GetObject DocumentsBucket/uploads/*',
            's3:PutObject DocumentsBucket/parsed/*',
            's3:PutObject DocumentsBucket/thumbnails/*',
            's3:DeleteObject DocumentsBucket/parsed/*',
            's3:DeleteObject DocumentsBucket/thumbnails/*',
            's3:ListBucket DocumentsBucket',
            'dynamodb:GetItem DocumentsTable',
            'dynamodb:UpdateItem DocumentsTable',
            'sqs:ReceiveMessage ProcessingQueue',
            'sqs:DeleteMessage ProcessingQueue',
            'sqs:ChangeMessageVisibility ProcessingQueue',
            'sqs:GetQueueAttributes ProcessingQueue',
            'sns:Publish NotificationsTopic',
            'cloudwatch:PutMetricData *'
        ],
        'tei-embed': [],
        'llm-gateway': []
    };

    test.each(Object.entries(TASK_GRANTS))('%s', (service, expected) => {
        const actual = grants(services, roleOf(service, 'TaskRoleArn')).map(
            ([a, r]) => `${a} ${r}`
        );
        expect(actual.sort()).toEqual([...expected].sort());
    });

    test('the worker lists only the parsed/ prefix', () => {
        const policies = Object.values(resources(services, 'AWS::IAM::Policy')).filter((p) =>
            p.Properties.Roles.some(
                (r: any) => target(r) === roleOf('ingestion-worker', 'TaskRoleArn')
            )
        );
        const list = policies
            .flatMap((p) => p.Properties.PolicyDocument.Statement)
            .find((s: any) => s.Action === 's3:ListBucket');
        expect(list.Condition).toEqual({ StringLike: { 's3:prefix': ['parsed/*'] } });
    });

    test('no task role holds a Bedrock, wildcard-action or IAM permission', () => {
        for (const service of SERVICES) {
            for (const [action] of grants(services, roleOf(service, 'TaskRoleArn'))) {
                expect(action).not.toMatch(/^bedrock:|\*|^iam:/);
            }
        }
    });
});

describe('secrets come from Secrets Manager', () => {
    // Each container's secrets: the variable and the Secrets Manager secret it reads.
    const PYTHON = {
        LANGFUSE_PUBLIC_KEY: 'Langfuse',
        LANGFUSE_SECRET_KEY: 'Langfuse',
        QDRANT_API_KEY: 'QdrantApiKey',
        MASKING_KEY: 'MaskingKey',
        LLM_GATEWAY_KEY: 'LlmGatewayKey'
    };
    const SECRETS: Record<string, Record<string, Record<string, string>>> = {
        api: { api: { LANGFUSE_PUBLIC_KEY: 'Langfuse', LANGFUSE_SECRET_KEY: 'Langfuse' } },
        frontend: {},
        'ai-service': {
            'ai-service': PYTHON,
            'gateway-setup': {
                LITELLM_MASTER_KEY: 'LitellmMasterKey',
                LLM_GATEWAY_KEY: 'LlmGatewayKey'
            }
        },
        'ingestion-worker': { 'ingestion-worker': PYTHON },
        'tei-embed': {},
        'llm-gateway': {
            'llm-gateway': {
                LANGFUSE_PUBLIC_KEY: 'Langfuse',
                LANGFUSE_SECRET_KEY: 'Langfuse',
                GEMINI_API_KEY: 'GeminiApiKey',
                LITELLM_MASTER_KEY: 'LitellmMasterKey',
                DATABASE_USERNAME: 'GatewayDbSecretAttachment',
                DATABASE_PASSWORD: 'GatewayDbSecretAttachment'
            }
        }
    };

    test.each(Object.entries(SECRETS))('%s', (service, expected) => {
        const actual: Record<string, Record<string, string>> = {};
        for (const container of containers(service)) {
            if (container.Secrets) {
                actual[container.Name] = Object.fromEntries(
                    container.Secrets.map((s: any) => [s.Name, resourceName(s.ValueFrom)])
                );
            }
        }
        expect(actual).toEqual(expected);
    });

    test('only the gateway holds the Gemini key', () => {
        const holders = SERVICES.filter((s) =>
            containers(s).some((c) =>
                (c.Secrets ?? []).some((x: any) => x.Name === 'GEMINI_API_KEY')
            )
        );
        expect(holders).toEqual(['llm-gateway']);
    });

    test('no key, secret or password is in a task environment variable', () => {
        for (const service of SERVICES) {
            for (const container of containers(service)) {
                for (const variable of container.Environment ?? []) {
                    expect(variable.Name).not.toMatch(/KEY|SECRET|PASSWORD|TOKEN/);
                }
            }
        }
    });

    test('every secret is generated by CloudFormation, never written in the template', () => {
        const secrets = Object.values(resources(infra, 'AWS::SecretsManager::Secret'));
        expect(secrets).toHaveLength(7);
        for (const secret of secrets) {
            expect(secret.Properties.SecretString).toBeUndefined();
            expect(secret.Properties.GenerateSecretString).toBeDefined();
        }
    });
});

test('the bucket notification covers both events, for uploads only', () => {
    const notification = Object.values(resources(infra, 'Custom::S3BucketNotifications'))[0];
    const queues = notification.Properties.NotificationConfiguration.QueueConfigurations;
    expect(queues.map((q: any) => q.Events).sort()).toEqual([
        ['s3:ObjectCreated:*'],
        ['s3:ObjectRemoved:*']
    ]);
    for (const queue of queues) {
        expect(target(queue.QueueArn)).toMatch(/^ProcessingQueue/);
        expect(queue.Filter.Key.FilterRules).toEqual([{ Name: 'prefix', Value: 'uploads/' }]);
    }
});

test('no Bedrock permission and no legacy worker remain', () => {
    const json = JSON.stringify([infra.toJSON(), services.toJSON()]);
    expect(json).not.toMatch(/bedrock/i);
    expect(json).not.toContain('documents-worker');
    expect(json).not.toContain('documents/worker');
    expect(json).not.toContain('thumbnail-jobs');
    expect(json).not.toMatch(/OPENAI|ANTHROPIC|JWT_SECRET/);
    const repositories = Object.values(resources(infra, 'AWS::ECR::Repository')).map(
        (r) => r.Properties.RepositoryName
    );
    expect(repositories.sort()).toEqual(
        ['ai-service', 'api', 'frontend', 'ingestion-worker', 'llm-gateway'].map(
            (r) => `documents/${r}`
        )
    );
});

test("cdk-nag's AWS Solutions checks pass, with every acknowledgment reasoned", () => {
    const report = new AwsSolutionsChecks(stacks.app).validateScope(stacks.app);
    expect(report.violations).toEqual([]);
    expect(report.success).toBe(true);
});

test('the templates contain no account ID or secret value', () => {
    const json = JSON.stringify([infra.toJSON(), services.toJSON()]);
    expect(json).not.toMatch(/(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])/);
    expect(json).not.toMatch(/AIza[0-9A-Za-z_-]{20}|sk-lf-|pk-lf-|sk-[A-Za-z0-9]{20}/);
    expect(json).toContain('AWS::AccountId'); // the account comes from the deploy
});

describe('operations', () => {
    test('the worker scales on its queue backlog, from zero to three tasks', () => {
        const worker = Object.values(
            resources(services, 'AWS::ApplicationAutoScaling::ScalableTarget')
        ).find((t) => JSON.stringify(t.Properties.ResourceId).includes('ingestionworker'))!;
        expect([worker.Properties.MinCapacity, worker.Properties.MaxCapacity]).toEqual([0, 3]);
        const alarms = Object.values(resources(services, 'AWS::CloudWatch::Alarm')).filter(
            (a) => a.Properties.Metrics?.[0]?.Expression === 'visible + inFlight'
        );
        expect(alarms.map((a) => a.Properties.EvaluationPeriods).sort()).toEqual([1, 10]);
    });

    test('the API and the gateway run one task each, without scaling', () => {
        for (const service of ['api', 'llm-gateway']) {
            const ecs = Object.values(resources(services, 'AWS::ECS::Service')).find(
                (s) => s.Properties.ServiceName === `documents-${service}`
            )!;
            expect(ecs.Properties.DesiredCount).toBe(1);
        }
        const scaled = Object.values(
            resources(services, 'AWS::ApplicationAutoScaling::ScalableTarget')
        ).map((t) =>
            t.Properties.ResourceId['Fn::Join'][1][3]['Fn::GetAtt'][0].replace(
                /Service[0-9A-F]{8}$/,
                ''
            )
        );
        expect(scaled.sort()).toEqual(['aiservice', 'frontend', 'ingestionworker', 'teiembed']);
    });

    test('Service Connect lets a chat stream and a gateway call run for two minutes', () => {
        for (const [service, seconds] of [
            ['ai-service', 120],
            ['llm-gateway', 120],
            ['tei-embed', 30]
        ] as const) {
            const ecs = Object.values(resources(services, 'AWS::ECS::Service')).find(
                (s) => s.Properties.ServiceName === `documents-${service}`
            )!;
            expect(
                ecs.Properties.ServiceConnectConfiguration.Services[0].Timeout
                    .PerRequestTimeoutSeconds
            ).toBe(seconds);
        }
    });

    test('a collector beside each service with metrics scrapes its port', () => {
        for (const [service, port] of [
            ['api', 9464],
            ['ai-service', 8000],
            ['ingestion-worker', 9101]
        ] as const) {
            const collector = containers(service).find((c) => c.Name === 'collector');
            expect(collector.Image).toBe(COLLECTOR_IMAGE);
            expect(collector.Essential).toBe(false);
            expect(JSON.stringify(collector.Environment)).toContain(`localhost:${port}`);
        }
        for (const service of ['frontend', 'tei-embed', 'llm-gateway']) {
            expect(containers(service).map((c) => c.Name)).not.toContain('collector');
        }
    });

    test('the Stage 10c alerts are alarms: three PromQL, three on the queues', () => {
        const alarms = Object.values(resources(services, 'AWS::CloudWatch::Alarm'));
        const promql = alarms.filter((a) => a.Properties.EvaluationCriteria);
        expect(promql.map((a) => a.Properties.AlarmName).sort()).toEqual(
            PROMQL_RULES.map((r) => r.name).sort()
        );
        const queue = alarms
            .map((a) => a.Properties.AlarmName)
            .filter((n) => n && !promql.some((p) => p.Properties.AlarmName === n));
        expect(queue.sort()).toEqual([
            'documents-dlq-depth',
            'documents-messages-old',
            'documents-queue-held-up'
        ]);
        for (const alarm of alarms.filter((a) => a.Properties.AlarmName)) {
            expect(JSON.stringify(alarm.Properties.AlarmActions)).toContain('AlertsTopic');
        }
    });

    test('the PromQL alarms read metrics the services define', () => {
        const telemetry = readFileSync(
            join(REPOSITORY, 'services', 'ai', 'src', 'docai', 'core', 'telemetry.py'),
            'utf8'
        );
        for (const rule of PROMQL_RULES) {
            for (const metric of rule.query.match(/docai_[a-z_]+/g) ?? []) {
                // prometheus_client adds _total to counters.
                expect(telemetry).toContain(`"${metric.replace(/_total$/, '')}"`);
            }
        }
    });
});

test("the README's cost table is the estimate of these stacks", () => {
    const readme = readFileSync(join(REPOSITORY, 'infrastructure', 'cdk', 'README.md'), 'utf8');
    const table = /<!-- cost-estimate:start -->\n([\s\S]*?)\n<!-- cost-estimate:end -->/.exec(
        readme
    )?.[1];
    expect(table).toBe(markdown(estimate([infra.toJSON() as any, services.toJSON() as any])));
});
