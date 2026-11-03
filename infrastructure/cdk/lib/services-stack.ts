import * as cdk from 'aws-cdk-lib';
import * as appscaling from 'aws-cdk-lib/aws-applicationautoscaling';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as servicediscovery from 'aws-cdk-lib/aws-servicediscovery';
import { Construct } from 'constructs';
import { Alarms } from './alarms';
import { addCollector } from './collector';
import { DeployConfig } from './config';
import { DocumentsInfraStack, ServiceName } from './infra-stack';

/** Hugging Face's CPU image for arm64, pinned by digest; the model downloads at start. */
export const TEI_IMAGE =
    'ghcr.io/huggingface/text-embeddings-inference:cpu-arm64-1.9.4@sha256:798cd7615e060a72ccbdf19aa0ada52289c6a0147490195bafaece6ce04d70f1';
export const TEI_MODEL = 'intfloat/multilingual-e5-small';
/** Every model role defaults to the cheapest Gemini model. */
const MODEL = 'google_genai:gemini-3.1-flash-lite';

export interface DocumentsServicesStackProps extends cdk.StackProps {
    infra: DocumentsInfraStack;
    config: DeployConfig;
}

interface ServiceSpec {
    name: ServiceName;
    image: ecs.ContainerImage;
    /** Task size: CPU units and MiB, the collector and Service Connect's proxy included. */
    cpu: number;
    memoryMiB: number;
    architecture: ecs.CpuArchitecture;
    /** The container's port; other services reach it as http://<name>:<port>. */
    port?: number;
    /** Offered to other services through Service Connect, with this per-request timeout. */
    serves?: cdk.Duration;
    /** A Service Connect client: it calls other services by name. */
    calls?: boolean;
    environment: Record<string, string>;
    secrets?: Record<string, ecs.Secret>;
    command?: string[];
    healthCheck?: string[];
    startPeriod?: cdk.Duration;
    /** The service's Prometheus endpoint, scraped by a collector beside it. */
    metricsPort?: number;
    desiredCount: number;
}

interface Service {
    service: ecs.FargateService;
    task: ecs.FargateTaskDefinition;
    securityGroup: ec2.SecurityGroup;
}

/**
 * The six services on ECS Fargate (docs/ARCHITECTURE.md, AWS):
 * - `api` and `frontend` behind the internal load balancer, which CloudFront fronts;
 * - `ai-service`, reached by the API through Service Connect only;
 * - `ingestion-worker`, scaling with the processing queue, down to zero;
 * - `tei-embed` and `llm-gateway`, reached by the two Python services.
 * Each has its own task role, granted only the calls its code makes, and its own security
 * group, open only to the services that call it.
 */
export class DocumentsServicesStack extends cdk.Stack {
    readonly services: Record<ServiceName, Service>;

    constructor(scope: Construct, id: string, props: DocumentsServicesStackProps) {
        super(scope, id, props);
        const { infra, config } = props;

        const cluster = new ecs.Cluster(this, 'Cluster', {
            vpc: infra.vpc,
            clusterName: 'documents',
            // Off: about 160 custom metrics for six services, some $49 a month; ECS's
            // own CPU and memory metrics per service are free (lib/nag.ts).
            containerInsightsV2: ecs.ContainerInsights.DISABLED,
            defaultCloudMapNamespace: {
                name: 'documents.local',
                type: servicediscovery.NamespaceType.HTTP,
                useForServiceConnect: true
            }
        });

        const image = (name: keyof DocumentsInfraStack['repositories']) =>
            ecs.ContainerImage.fromEcrRepository(infra.repositories[name], config.imageTag);
        const secret = (name: keyof DocumentsInfraStack['secrets'], field?: string) =>
            ecs.Secret.fromSecretsManager(infra.secrets[name], field);
        const region = { AWS_REGION: this.region };
        const issuer = `https://cognito-idp.${this.region}.amazonaws.com/${config.cognitoUserPoolId}`;
        const auth = {
            AUTH_ISSUER: issuer,
            AUTH_AUDIENCE: config.cognitoClientId,
            AUTH_JWKS_URI: `${issuer}/.well-known/jwks.json`
        };
        const tracing = { LANGFUSE_BASE_URL: config.langfuseBaseUrl };
        const tracingKeys = {
            LANGFUSE_PUBLIC_KEY: secret('langfuse', 'publicKey'),
            LANGFUSE_SECRET_KEY: secret('langfuse', 'secretKey')
        };
        // The Python services call Gemini only through the gateway, so they never hold
        // its key (docai.core.settings).
        const search = {
            QDRANT_URL: config.qdrantUrl,
            QDRANT_ALIAS: 'chunks',
            TEI_EMBED_URL: 'http://tei-embed:80',
            LLM_GATEWAY_URL: 'http://llm-gateway:4000'
        };
        const pythonKeys = {
            ...tracingKeys,
            QDRANT_API_KEY: secret('qdrant'),
            MASKING_KEY: secret('masking'),
            LLM_GATEWAY_KEY: secret('gatewayKey')
        };
        const python = (port: number, path: string) => [
            'CMD',
            'python',
            '-c',
            `import urllib.request; urllib.request.urlopen('http://localhost:${port}${path}', timeout=3)`
        ];

        const make = (spec: ServiceSpec): Service => {
            const logGroup = infra.logGroups[spec.name];
            const task = new ecs.FargateTaskDefinition(this, `${spec.name}Task`, {
                family: `documents-${spec.name}`,
                cpu: spec.cpu,
                memoryLimitMiB: spec.memoryMiB,
                runtimePlatform: {
                    cpuArchitecture: spec.architecture,
                    operatingSystemFamily: ecs.OperatingSystemFamily.LINUX
                }
            });
            task.addContainer(spec.name, {
                image: spec.image,
                command: spec.command,
                environment: spec.environment,
                secrets: spec.secrets,
                portMappings: spec.port
                    ? [
                          {
                              containerPort: spec.port,
                              name: spec.name,
                              appProtocol: ecs.AppProtocol.http
                          }
                      ]
                    : undefined,
                healthCheck: spec.healthCheck && {
                    command: spec.healthCheck,
                    interval: cdk.Duration.seconds(30),
                    timeout: cdk.Duration.seconds(5),
                    retries: 3,
                    startPeriod: spec.startPeriod ?? cdk.Duration.seconds(30)
                },
                logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: spec.name })
            });
            if (spec.metricsPort) {
                addCollector(task, spec.name, spec.metricsPort, logGroup);
            }
            const securityGroup = new ec2.SecurityGroup(this, `${spec.name}Sg`, {
                vpc: infra.vpc,
                description: `${spec.name} tasks`,
                allowAllOutbound: true
            });
            const service = new ecs.FargateService(this, `${spec.name}Service`, {
                cluster,
                serviceName: `documents-${spec.name}`,
                taskDefinition: task,
                desiredCount: spec.desiredCount,
                minHealthyPercent: 100,
                maxHealthyPercent: 200,
                circuitBreaker: { enable: true, rollback: true },
                securityGroups: [securityGroup],
                vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
                serviceConnectConfiguration:
                    spec.serves || spec.calls
                        ? {
                              services: spec.serves
                                  ? [
                                        {
                                            portMappingName: spec.name,
                                            dnsName: spec.name,
                                            port: spec.port!,
                                            // Envoy's default of 15 s would cut chat streams.
                                            perRequestTimeout: spec.serves,
                                            idleTimeout: cdk.Duration.minutes(5)
                                        }
                                    ]
                                  : undefined,
                              logDriver: ecs.LogDrivers.awsLogs({
                                  logGroup,
                                  streamPrefix: 'service-connect'
                              })
                          }
                        : undefined
            });
            return { service, task, securityGroup };
        };

        const api = make({
            name: 'api',
            image: image('api'),
            cpu: 512,
            memoryMiB: 1024,
            architecture: ecs.CpuArchitecture.ARM64,
            port: 3001,
            calls: true,
            environment: {
                ...region,
                ...auth,
                ...tracing,
                NODE_ENV: 'production',
                PORT: '3001',
                S3_BUCKET: infra.bucket.bucketName,
                DYNAMODB_TABLE: infra.table.tableName,
                SQS_QUEUE_URL: infra.processingQueue.queueUrl,
                SQS_API_NOTIFICATIONS_QUEUE_URL: infra.notificationsQueue.queueUrl,
                CORS_ORIGIN: infra.publicOrigin,
                AUTH_PROVIDER: 'cognito',
                COGNITO_USER_POOL_ID: config.cognitoUserPoolId,
                COGNITO_CLIENT_ID: config.cognitoClientId,
                AI_SERVICE_URL: 'http://ai-service:8000',
                METRICS_PORT: '9464'
            },
            secrets: tracingKeys,
            healthCheck: [
                'CMD-SHELL',
                'wget -qO- http://localhost:3001/api/health/ready || exit 1'
            ],
            metricsPort: 9464,
            // One task: status events reach browsers through one SQS queue that API tasks
            // would compete for, so a second task would get half of them.
            desiredCount: 1
        });

        const frontend = make({
            name: 'frontend',
            image: image('frontend'),
            cpu: 256,
            memoryMiB: 512,
            architecture: ecs.CpuArchitecture.ARM64,
            port: 3000,
            environment: { NODE_ENV: 'production' },
            desiredCount: 1
        });

        const ai = make({
            name: 'ai-service',
            image: image('ai-service'),
            cpu: 512,
            memoryMiB: 2048,
            architecture: ecs.CpuArchitecture.ARM64,
            port: 8000,
            serves: cdk.Duration.minutes(2),
            calls: true,
            environment: {
                ...region,
                ...auth,
                ...tracing,
                ...search,
                DYNAMODB_TABLE: infra.table.tableName,
                CHAT_MODEL: MODEL,
                FAST_MODEL: MODEL,
                LANGFUSE_PROMPT_LABEL: 'production',
                OTEL_SERVICE_NAME: 'ai-service'
            },
            secrets: pythonKeys,
            healthCheck: python(8000, '/health'),
            metricsPort: 8000,
            desiredCount: 1
        });
        // Creates the services' key and the per-user budget in the gateway, as
        // `docai gateway setup` does locally; idempotent, so every task start runs it.
        // It lives here because the gateway's task is x86 and this image is arm64.
        ai.task.addContainer('gateway-setup', {
            image: image('ai-service'),
            essential: false,
            entryPoint: ['sh', '-c'],
            command: [
                'for attempt in $(seq 30); do docai gateway setup --url http://llm-gateway:4000 && exit 0; sleep 10; done; exit 1'
            ],
            environment: { LLM_GATEWAY_USER_DAILY_BUDGET: '0.5' },
            secrets: {
                LITELLM_MASTER_KEY: secret('gatewayMaster'),
                LLM_GATEWAY_KEY: secret('gatewayKey')
            },
            memoryLimitMiB: 256,
            logging: ecs.LogDrivers.awsLogs({
                logGroup: infra.logGroups['llm-gateway'],
                streamPrefix: 'setup'
            })
        });

        const worker = make({
            name: 'ingestion-worker',
            image: image('ingestion-worker'),
            // Docling peaks at about 1.4 GB on long PDFs; the image runs PyTorch on 2 threads.
            cpu: 2048,
            memoryMiB: 4096,
            architecture: ecs.CpuArchitecture.ARM64,
            calls: true,
            environment: {
                ...region,
                ...tracing,
                ...search,
                S3_BUCKET: infra.bucket.bucketName,
                DYNAMODB_TABLE: infra.table.tableName,
                SNS_TOPIC_ARN: infra.notificationsTopic.topicArn,
                SQS_QUEUE_URL: infra.processingQueue.queueUrl,
                VISION_MODEL: MODEL,
                EXTRACT_MODEL: MODEL,
                OTEL_SERVICE_NAME: 'ingestion-worker',
                METRICS_PORT: '9101'
            },
            secrets: pythonKeys,
            healthCheck: ['CMD', 'python', '-m', 'docai.ingest.health'],
            metricsPort: 9101,
            desiredCount: 0
        });

        const tei = make({
            name: 'tei-embed',
            image: ecs.ContainerImage.fromRegistry(TEI_IMAGE),
            cpu: 512,
            memoryMiB: 2048,
            architecture: ecs.CpuArchitecture.ARM64,
            port: 80,
            serves: cdk.Duration.seconds(30),
            command: [
                '--model-id',
                TEI_MODEL,
                '--tokenization-workers',
                '1',
                '--max-batch-tokens',
                '2048'
            ],
            environment: {},
            healthCheck: ['CMD-SHELL', 'curl -sf http://localhost:80/health || exit 1'],
            startPeriod: cdk.Duration.seconds(120),
            desiredCount: 1
        });

        const db = infra.gatewayDb;
        const gateway = make({
            name: 'llm-gateway',
            image: image('llm-gateway'),
            cpu: 512,
            memoryMiB: 1024,
            // LiteLLM publishes this version for amd64 only.
            architecture: ecs.CpuArchitecture.X86_64,
            port: 4000,
            // One call: up to 30 s on the primary, a retry, then the fallback.
            serves: cdk.Duration.minutes(2),
            environment: {
                ...tracing,
                LLM_GATEWAY_DAILY_BUDGET: '5',
                LITELLM_LOCAL_MODEL_COST_MAP: 'True',
                OTEL_SERVICE_NAME: 'llm-gateway',
                DATABASE_HOST: `${db.dbInstanceEndpointAddress}:${db.dbInstanceEndpointPort}`,
                DATABASE_NAME: 'litellm',
                DATABASE_SSLMODE: 'require'
            },
            secrets: {
                ...tracingKeys,
                GEMINI_API_KEY: secret('gemini'),
                LITELLM_MASTER_KEY: secret('gatewayMaster'),
                DATABASE_USERNAME: ecs.Secret.fromSecretsManager(db.secret!, 'username'),
                DATABASE_PASSWORD: ecs.Secret.fromSecretsManager(db.secret!, 'password')
            },
            healthCheck: python(4000, '/health/liveliness'),
            startPeriod: cdk.Duration.seconds(120),
            // One task: LiteLLM keeps budget counters in memory between database writes.
            desiredCount: 1
        });

        this.services = {
            api,
            frontend,
            'ai-service': ai,
            'ingestion-worker': worker,
            'tei-embed': tei,
            'llm-gateway': gateway
        };

        // Network: each service accepts only its callers.
        const allow = (to: Service, from: ec2.ISecurityGroup, port: number, why: string) =>
            to.securityGroup.addIngressRule(from, ec2.Port.tcp(port), why);
        allow(api, infra.albSecurityGroup, 3001, 'From the load balancer');
        allow(frontend, infra.albSecurityGroup, 3000, 'From the load balancer');
        allow(ai, api.securityGroup, 8000, 'From the API');
        for (const caller of [ai, worker]) {
            allow(tei, caller.securityGroup, 80, `From ${caller.service.node.id}`);
            allow(gateway, caller.securityGroup, 4000, `From ${caller.service.node.id}`);
        }
        // Placed in this stack (remote rule), so the infra stack doesn't refer back here.
        db.connections.securityGroups[0].addIngressRule(
            gateway.securityGroup,
            ec2.Port.tcp(5432),
            'From the LLM gateway',
            true
        );

        this.grantAws(infra);
        this.route(infra, api, frontend);
        this.scale(infra);
        new Alarms(this, 'Alarms', {
            alertsTopic: infra.alertsTopic,
            processingQueue: infra.processingQueue,
            processingDlq: infra.processingDlq
        });
    }

    /** The AWS calls each service's code makes, and nothing else. */
    private grantAws(infra: DocumentsInfraStack): void {
        const { bucket, table } = infra;
        const allow = (
            name: ServiceName,
            actions: string[],
            resources: string[],
            conditions?: Record<string, unknown>
        ) =>
            this.services[name].task.addToTaskRolePolicy(
                new iam.PolicyStatement({ actions, resources, conditions })
            );

        // The API presigns uploads and downloads, deletes uploads, checks the bucket and
        // the processing queue for /api/health, and forwards status events.
        allow('api', ['s3:PutObject', 's3:DeleteObject'], [bucket.arnForObjects('uploads/*')]);
        allow(
            'api',
            ['s3:GetObject'],
            [bucket.arnForObjects('uploads/*'), bucket.arnForObjects('thumbnails/*')]
        );
        allow('api', ['s3:ListBucket'], [bucket.bucketArn]);
        allow(
            'api',
            [
                'dynamodb:GetItem',
                'dynamodb:PutItem',
                'dynamodb:UpdateItem',
                'dynamodb:DeleteItem',
                'dynamodb:Query',
                'dynamodb:BatchWriteItem'
            ],
            [table.tableArn]
        );
        allow(
            'api',
            ['sqs:ReceiveMessage', 'sqs:DeleteMessage'],
            [infra.notificationsQueue.queueArn]
        );
        allow('api', ['sqs:GetQueueAttributes'], [infra.processingQueue.queueArn]);

        // The AI service reads the user's documents' fields.
        allow('ai-service', ['dynamodb:Query'], [table.tableArn]);

        // The worker reads an upload, writes its parse and thumbnail (removed with the
        // upload), updates the document and publishes its status.
        allow('ingestion-worker', ['s3:GetObject'], [bucket.arnForObjects('uploads/*')]);
        allow(
            'ingestion-worker',
            ['s3:PutObject', 's3:DeleteObject'],
            [bucket.arnForObjects('parsed/*'), bucket.arnForObjects('thumbnails/*')]
        );
        allow('ingestion-worker', ['s3:ListBucket'], [bucket.bucketArn], {
            StringLike: { 's3:prefix': ['parsed/*'] }
        });
        allow('ingestion-worker', ['dynamodb:GetItem', 'dynamodb:UpdateItem'], [table.tableArn]);
        allow(
            'ingestion-worker',
            [
                'sqs:ReceiveMessage',
                'sqs:DeleteMessage',
                'sqs:ChangeMessageVisibility',
                'sqs:GetQueueAttributes'
            ],
            [infra.processingQueue.queueArn]
        );
        allow('ingestion-worker', ['sns:Publish'], [infra.notificationsTopic.topicArn]);
    }

    /** The load balancer's routes: the API and its WebSocket, then the frontend. */
    private route(infra: DocumentsInfraStack, api: Service, frontend: Service): void {
        const targetGroup = (
            id: string,
            service: Service,
            port: number,
            path: string,
            sticky = false
        ) =>
            new elbv2.ApplicationTargetGroup(this, id, {
                vpc: infra.vpc,
                port,
                protocol: elbv2.ApplicationProtocol.HTTP,
                targetType: elbv2.TargetType.IP,
                targets: [
                    service.service.loadBalancerTarget({
                        containerName:
                            service.service.taskDefinition.defaultContainer!.containerName,
                        containerPort: port
                    })
                ],
                healthCheck: {
                    path,
                    healthyHttpCodes: '200-399',
                    interval: cdk.Duration.seconds(15),
                    healthyThresholdCount: 2
                },
                stickinessCookieDuration: sticky ? cdk.Duration.days(1) : undefined,
                deregistrationDelay: cdk.Duration.seconds(30)
            });
        const routes: [string, number, string[], elbv2.ApplicationTargetGroup][] = [
            ['ApiRoute', 10, ['/api/*'], targetGroup('ApiTg', api, 3001, '/api/health/ready')],
            [
                'WsRoute',
                11,
                ['/socket.io/*'],
                // Socket.IO's polling fallback needs every request on the same task.
                targetGroup('WsTg', api, 3001, '/api/health/ready', true)
            ],
            ['FrontendRoute', 100, ['/*'], targetGroup('FrontendTg', frontend, 3000, '/')]
        ];
        for (const [id, priority, paths, group] of routes) {
            new elbv2.ApplicationListenerRule(this, id, {
                listener: infra.listener,
                priority,
                conditions: [elbv2.ListenerCondition.pathPatterns(paths)],
                targetGroups: [group]
            });
        }
    }

    private scale(infra: DocumentsInfraStack): void {
        for (const [name, max] of [
            ['frontend', 2],
            ['ai-service', 2],
            ['tei-embed', 2]
        ] as const) {
            this.services[name].service
                .autoScaleTaskCount({ minCapacity: 1, maxCapacity: max })
                .scaleOnCpuUtilization('Cpu', {
                    targetUtilizationPercent: 70,
                    scaleInCooldown: cdk.Duration.minutes(5),
                    scaleOutCooldown: cdk.Duration.minutes(1)
                });
        }

        // The worker follows the queue's backlog, waiting and in flight, from zero to
        // three tasks. In-flight messages count, so a task isn't stopped mid-document; it
        // scales in only after ten minutes with nothing on the queue.
        const queue = infra.processingQueue;
        const backlog = new cloudwatch.MathExpression({
            expression: 'visible + inFlight',
            usingMetrics: {
                visible: queue.metricApproximateNumberOfMessagesVisible({ statistic: 'Maximum' }),
                inFlight: queue.metricApproximateNumberOfMessagesNotVisible({
                    statistic: 'Maximum'
                })
            },
            period: cdk.Duration.minutes(1)
        });
        const scaling = this.services['ingestion-worker'].service.autoScaleTaskCount({
            minCapacity: 0,
            maxCapacity: 3
        });
        scaling.scaleOnMetric('BacklogOut', {
            metric: backlog,
            scalingSteps: [
                { upper: 0, change: 0 },
                { lower: 1, change: +1 },
                { lower: 20, change: +2 }
            ],
            adjustmentType: appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            cooldown: cdk.Duration.minutes(1)
        });
        scaling.scaleOnMetric('BacklogIn', {
            metric: backlog,
            scalingSteps: [
                { upper: 0, change: -1 },
                { lower: 1, change: 0 }
            ],
            adjustmentType: appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            evaluationPeriods: 10,
            cooldown: cdk.Duration.minutes(5)
        });
    }
}
