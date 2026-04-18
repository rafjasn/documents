import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import { DocumentsInfraStack } from './infra-stack';

export interface DocumentsServicesStackProps extends cdk.StackProps {
    infra: DocumentsInfraStack;
    imageTag: string;
}

export class DocumentsServicesStack extends cdk.Stack {
    constructor(scope: Construct, id: string, props: DocumentsServicesStackProps) {
        super(scope, id, props);

        const { infra, imageTag } = props;

        // Secrets
        // aws secretsmanager put-secret-value --secret-id documents/jwt-secret --secret-string "STRING"
        const jwtSecret = secretsmanager.Secret.fromSecretNameV2(
            this,
            'JwtSecret',
            'documents/jwt-secret'
        );
        const openaiKey = secretsmanager.Secret.fromSecretNameV2(
            this,
            'OpenAiKey',
            'documents/openai-api-key'
        );
        const anthropicKey = secretsmanager.Secret.fromSecretNameV2(
            this,
            'AnthropicKey',
            'documents/anthropic-api-key'
        );

        // ECS Cluster
        const cluster = new ecs.Cluster(this, 'Cluster', {
            vpc: infra.vpc,
            clusterName: 'documents',
            containerInsightsV2: ecs.ContainerInsights.ENABLED
        });

        cluster.addDefaultCloudMapNamespace({
            name: 'documents.local',
            vpc: infra.vpc
        });

        const apiLogging = ecs.LogDrivers.awsLogs({
            logGroup: infra.logGroups.api,
            streamPrefix: 'api'
        });

        const workerLogging = ecs.LogDrivers.awsLogs({
            logGroup: infra.logGroups.worker,
            streamPrefix: 'worker'
        });

        const frontendLogging = ecs.LogDrivers.awsLogs({
            logGroup: new logs.LogGroup(this, 'FrontendLogGroup', {
                logGroupName: '/documents/frontend',
                retention: logs.RetentionDays.ONE_WEEK,
                removalPolicy: cdk.RemovalPolicy.DESTROY
            }),
            streamPrefix: 'frontend'
        });

        const awsEnv: Record<string, string> = {
            AWS_REGION: this.region,
            S3_BUCKET: infra.bucket.bucketName,
            DYNAMODB_TABLE: infra.table.tableName,
            SNS_TOPIC_ARN: infra.notificationsTopic.topicArn,
            SQS_QUEUE_URL: infra.processingQueue.queueUrl,
            SQS_THUMBNAIL_QUEUE_URL: infra.thumbnailQueue.queueUrl
        };

        // ALB
        const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
            vpc: infra.vpc,
            internetFacing: true,
            loadBalancerName: 'documents'
        });

        const listener = alb.addListener('HttpListener', {
            port: 80,
            // open: false,
            defaultAction: elbv2.ListenerAction.fixedResponse(404, {
                contentType: 'text/plain',
                messageBody: 'Not found'
            })
        });

        // Security groups
        const albSg = new ec2.SecurityGroup(this, 'AlbSg', {
            vpc: infra.vpc,
            description: 'ALB — allow HTTP from internet',
            allowAllOutbound: true
        });

        albSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80));

        const servicesSg = new ec2.SecurityGroup(this, 'ServicesSg', {
            vpc: infra.vpc,
            description: 'ECS services — allow ALB and intra-cluster traffic',
            allowAllOutbound: true
        });

        servicesSg.addIngressRule(albSg, ec2.Port.allTcp(), 'From ALB');
        servicesSg.addIngressRule(servicesSg, ec2.Port.allTcp(), 'Intra-cluster');

        // build fargate task definition
        const makeTaskDef = (name: string, cpu: number, memoryMiB: number) =>
            new ecs.FargateTaskDefinition(this, `${name}Task`, {
                cpu,
                memoryLimitMiB: memoryMiB,
                taskRole: infra.taskRole,
                family: `documents-${name}`
            });

        // create fargate service and target group
        const makeService = (
            name: string,
            task: ecs.FargateTaskDefinition,
            opts?: { minCount?: number; maxCount?: number }
        ) => {
            const service = new ecs.FargateService(this, `${name}Service`, {
                cluster,
                taskDefinition: task,
                serviceName: `documents-${name}`,
                desiredCount: opts?.minCount ?? 1,
                minHealthyPercent: 100,
                maxHealthyPercent: 200,
                securityGroups: [servicesSg],
                vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
                enableExecuteCommand: true,
                serviceConnectConfiguration: {
                    namespace: 'documents.local'
                }
            });

            const scaling = service.autoScaleTaskCount({
                minCapacity: opts?.minCount ?? 1,
                maxCapacity: opts?.maxCount ?? 4
            });
            scaling.scaleOnCpuUtilization(`${name}CpuScaling`, {
                targetUtilizationPercent: 70,
                scaleInCooldown: cdk.Duration.seconds(60),
                scaleOutCooldown: cdk.Duration.seconds(30)
            });

            return service;
        };

        // API Service
        const apiTask = makeTaskDef('api', 512, 1024);
        apiTask.addContainer('api', {
            image: ecs.ContainerImage.fromEcrRepository(infra.repositories.api, imageTag),
            portMappings: [{ containerPort: 3001, name: 'api' }],
            environment: {
                ...awsEnv,
                PORT: '3001',
                NODE_ENV: 'production',
                CORS_ORIGIN: `http://${alb.loadBalancerDnsName}`
            },
            secrets: {
                JWT_SECRET: ecs.Secret.fromSecretsManager(jwtSecret)
            },
            logging: apiLogging,
            healthCheck: {
                command: [
                    'CMD-SHELL',
                    'wget -qO- http://localhost:3001/api/health/ready || exit 1'
                ],
                interval: cdk.Duration.seconds(15),
                timeout: cdk.Duration.seconds(5),
                retries: 3,
                startPeriod: cdk.Duration.seconds(30)
            }
        });

        const apiService = makeService('api', apiTask, { minCount: 1, maxCount: 4 });

        const apiTg = new elbv2.ApplicationTargetGroup(this, 'ApiTg', {
            vpc: infra.vpc,
            port: 3001,
            protocol: elbv2.ApplicationProtocol.HTTP,
            targetType: elbv2.TargetType.IP,
            healthCheck: {
                path: '/api/health/ready',
                interval: cdk.Duration.seconds(15),
                healthyThresholdCount: 2
            },
            deregistrationDelay: cdk.Duration.seconds(30)
        });

        apiService.attachToApplicationTargetGroup(apiTg);

        const wsTg = new elbv2.ApplicationTargetGroup(this, 'WsTg', {
            vpc: infra.vpc,
            port: 3001,
            protocol: elbv2.ApplicationProtocol.HTTP,
            targetType: elbv2.TargetType.IP,
            healthCheck: {
                path: '/api/health/ready',
                interval: cdk.Duration.seconds(15),
                healthyThresholdCount: 2
            },
            stickinessCookieDuration: cdk.Duration.days(1),
            deregistrationDelay: cdk.Duration.seconds(30)
        });
        apiService.attachToApplicationTargetGroup(wsTg);

        listener.addAction('ApiRoute', {
            priority: 10,
            conditions: [elbv2.ListenerCondition.pathPatterns(['/api/*'])],
            action: elbv2.ListenerAction.forward([apiTg])
        });

        listener.addAction('WsRoute', {
            priority: 11,
            conditions: [elbv2.ListenerCondition.pathPatterns(['/socket.io/*'])],
            action: elbv2.ListenerAction.forward([wsTg])
        });

        // Worker Service
        const workerTask = makeTaskDef('worker', 512, 1024);
        workerTask.addContainer('worker', {
            image: ecs.ContainerImage.fromEcrRepository(infra.repositories.worker, imageTag),
            environment: {
                ...awsEnv,
                PORT: '3002',
                NODE_ENV: 'production',
                SQS_QUEUE_URL: infra.processingQueue.queueUrl,
                SQS_POLLING_INTERVAL: '5000',
                SQS_MAX_MESSAGES: '5',
                AI_PROVIDER: 'bedrock',
                AI_MODEL: 'anthropic.claude-3-haiku-20240307-v1:0'
            },
            secrets: {
                OPENAI_API_KEY: ecs.Secret.fromSecretsManager(openaiKey),
                ANTHROPIC_API_KEY: ecs.Secret.fromSecretsManager(anthropicKey)
            },
            logging: workerLogging,
            healthCheck: {
                command: ['CMD-SHELL', 'wget -qO- http://localhost:3002/health || exit 1'],
                interval: cdk.Duration.seconds(15),
                timeout: cdk.Duration.seconds(5),
                retries: 3,
                startPeriod: cdk.Duration.seconds(30)
            }
        });

        const workerService = new ecs.FargateService(this, 'WorkerService', {
            cluster,
            taskDefinition: workerTask,
            serviceName: 'documents-worker',
            desiredCount: 1,
            minHealthyPercent: 100,
            maxHealthyPercent: 200,
            securityGroups: [servicesSg],
            vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
            enableExecuteCommand: true
        });

        const workerScaling = workerService.autoScaleTaskCount({ minCapacity: 1, maxCapacity: 6 });
        workerScaling.scaleOnMetric('WorkerQueueDepthScaling', {
            metric: infra.processingQueue.metricApproximateNumberOfMessagesVisible({
                period: cdk.Duration.minutes(1),
                statistic: 'Maximum'
            }),
            scalingSteps: [
                { upper: 0, change: -1 },
                { lower: 10, change: +1 },
                { lower: 50, change: +2 }
            ],
            adjustmentType: cdk.aws_applicationautoscaling.AdjustmentType.CHANGE_IN_CAPACITY
        });

        // Frontend Service
        const frontendTask = makeTaskDef('frontend', 512, 1024);
        frontendTask.addContainer('frontend', {
            image: ecs.ContainerImage.fromEcrRepository(infra.repositories.frontend, imageTag),
            portMappings: [{ containerPort: 3000 }],
            environment: {
                NODE_ENV: 'production',
                NEXT_PUBLIC_API_URL: `http://${alb.loadBalancerDnsName}`,
                NEXT_PUBLIC_WS_URL: `ws://${alb.loadBalancerDnsName}`
            },
            logging: frontendLogging,
            healthCheck: {
                command: ['CMD-SHELL', 'wget -qO- http://localhost:3000 || exit 1'],
                interval: cdk.Duration.seconds(15),
                timeout: cdk.Duration.seconds(5),
                retries: 3,
                startPeriod: cdk.Duration.seconds(60)
            }
        });

        const frontendService = makeService('frontend', frontendTask, { minCount: 1, maxCount: 2 });

        const frontendTg = new elbv2.ApplicationTargetGroup(this, 'FrontendTg', {
            vpc: infra.vpc,
            port: 3000,
            protocol: elbv2.ApplicationProtocol.HTTP,
            targetType: elbv2.TargetType.IP,
            healthCheck: {
                path: '/',
                interval: cdk.Duration.seconds(15),
                healthyThresholdCount: 2
            },
            deregistrationDelay: cdk.Duration.seconds(30)
        });
        frontendService.attachToApplicationTargetGroup(frontendTg);

        listener.addAction('FrontendDefault', {
            priority: 100,
            conditions: [elbv2.ListenerCondition.pathPatterns(['/*'])],
            action: elbv2.ListenerAction.forward([frontendTg])
        });

        new cdk.CfnOutput(this, 'AlbDns', {
            value: alb.loadBalancerDnsName,
            description: 'Application Load Balancer DNS name'
        });
    }
}
