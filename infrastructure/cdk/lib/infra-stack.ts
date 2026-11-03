import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3Notifications from 'aws-cdk-lib/aws-s3-notifications';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';

const VPC_CIDR = '10.0.0.0/16';

/** The ECS services, each with its log group. */
export const SERVICES = [
    'api',
    'frontend',
    'ai-service',
    'ingestion-worker',
    'tei-embed',
    'llm-gateway'
] as const;
export type ServiceName = (typeof SERVICES)[number];

/** The images this repository builds and deploy.yml pushes; TEI's is Hugging Face's. */
export const BUILT_IMAGES = [
    'api',
    'frontend',
    'ai-service',
    'ingestion-worker',
    'llm-gateway'
] as const;
export type BuiltImage = (typeof BUILT_IMAGES)[number];

/**
 * The keys the services read from Secrets Manager. CloudFormation creates each with a
 * generated placeholder, so no value is ever in a template; the real values are put
 * after the first deploy of this stack (README.md, Deploying). The masking key's generated value
 * is used as it is.
 */
export type SecretName =
    | 'gemini'
    | 'qdrant'
    | 'langfuse'
    | 'masking'
    | 'gatewayMaster'
    | 'gatewayKey';

export interface DocumentsInfraStackProps extends cdk.StackProps {
    removalPolicy: cdk.RemovalPolicy;
    /** Browser origins besides the CloudFront domain, e.g. a custom domain. */
    extraOrigins: string[];
}

/**
 * Everything the services stand on: the network, storage, queues, the edge (CloudFront in
 * front of an internal load balancer), image repositories, log groups, secrets and the
 * gateway's database. Deployed first: deploy.yml reads its outputs to build the frontend.
 */
export class DocumentsInfraStack extends cdk.Stack {
    readonly vpc: ec2.Vpc;
    readonly bucket: s3.Bucket;
    readonly table: dynamodb.Table;
    readonly processingQueue: sqs.Queue;
    readonly processingDlq: sqs.Queue;
    readonly notificationsQueue: sqs.Queue;
    readonly notificationsTopic: sns.Topic;
    readonly alertsTopic: sns.Topic;
    readonly repositories: Record<BuiltImage, ecr.Repository>;
    readonly logGroups: Record<ServiceName, logs.LogGroup>;
    readonly secrets: Record<SecretName, secretsmanager.Secret>;
    readonly gatewayDb: rds.DatabaseInstance;
    readonly albSecurityGroup: ec2.SecurityGroup;
    readonly listener: elbv2.ApplicationListener;
    readonly distribution: cloudfront.Distribution;
    /** The public origin: https:// and the CloudFront domain. */
    readonly publicOrigin: string;

    constructor(scope: Construct, id: string, props: DocumentsInfraStackProps) {
        super(scope, id, props);
        const removal = props.removalPolicy;
        const destroy = removal === cdk.RemovalPolicy.DESTROY;

        // Network. Tasks run in private subnets behind one NAT gateway (Gemini, Langfuse,
        // Qdrant Cloud and Hugging Face are outside AWS); S3 and DynamoDB traffic, image
        // layers included, takes the free gateway endpoints instead.
        const flowLogs = new logs.LogGroup(this, 'FlowLogs', {
            logGroupName: '/documents/vpc-flow-logs',
            retention: logs.RetentionDays.ONE_MONTH,
            removalPolicy: cdk.RemovalPolicy.DESTROY
        });
        this.vpc = new ec2.Vpc(this, 'Vpc', {
            ipAddresses: ec2.IpAddresses.cidr(VPC_CIDR),
            maxAzs: 2,
            natGateways: 1,
            subnetConfiguration: [
                { name: 'Public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
                { name: 'Private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 24 },
                { name: 'Database', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 28 }
            ],
            gatewayEndpoints: {
                S3: { service: ec2.GatewayVpcEndpointAwsService.S3 },
                DynamoDB: { service: ec2.GatewayVpcEndpointAwsService.DYNAMODB }
            },
            // Rejected traffic only: what a security review reads, at a fraction of the volume.
            flowLogs: {
                Rejected: {
                    destination: ec2.FlowLogDestination.toCloudWatchLogs(flowLogs),
                    trafficType: ec2.FlowLogTrafficType.REJECT
                }
            }
        });

        // Access logs of the bucket and of CloudFront, which every request passes through;
        // its standard logs are delivered with ACLs, hence the object ownership.
        const accessLogs = new s3.Bucket(this, 'AccessLogs', {
            encryption: s3.BucketEncryption.S3_MANAGED,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            enforceSSL: true,
            lifecycleRules: [{ id: 'expire', expiration: cdk.Duration.days(90) }],
            removalPolicy: removal,
            autoDeleteObjects: destroy
        });

        // Edge: CloudFront serves HTTPS on its own domain and reaches the load balancer
        // through a VPC origin, so the load balancer is internal and has no public
        // address. The listener's routes are added by the services stack.
        this.albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSg', {
            vpc: this.vpc,
            description: 'Internal ALB: HTTP from CloudFront VPC origin ENIs inside the VPC',
            allowAllOutbound: true
        });
        this.albSecurityGroup.addIngressRule(
            ec2.Peer.ipv4(VPC_CIDR),
            ec2.Port.tcp(80),
            'CloudFront VPC origin'
        );
        const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
            vpc: this.vpc,
            internetFacing: false,
            vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
            securityGroup: this.albSecurityGroup,
            dropInvalidHeaderFields: true
        });
        this.listener = alb.addListener('Http', {
            port: 80,
            open: false,
            defaultAction: elbv2.ListenerAction.fixedResponse(404, {
                contentType: 'text/plain',
                messageBody: 'Not found'
            })
        });

        const albOrigin = origins.VpcOrigin.withApplicationLoadBalancer(alb, {
            httpPort: 80,
            protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            // A chat stream's first event comes after the question is routed; 60 s is
            // CloudFront's largest timeout without a quota increase.
            readTimeout: cdk.Duration.seconds(60),
            keepaliveTimeout: cdk.Duration.seconds(60)
        });
        // The API authenticates with a bearer token on GET requests too, and CloudFront
        // forwards the Authorization header only when it is part of the cache key; with a
        // default TTL of 0 nothing is cached unless the API says so.
        const apiCachePolicy = new cloudfront.CachePolicy(this, 'ApiCachePolicy', {
            comment: 'API: Authorization in the key, nothing cached by default',
            minTtl: cdk.Duration.seconds(0),
            defaultTtl: cdk.Duration.seconds(0),
            maxTtl: cdk.Duration.seconds(1),
            headerBehavior: cloudfront.CacheHeaderBehavior.allowList('Authorization'),
            queryStringBehavior: cloudfront.CacheQueryStringBehavior.all(),
            cookieBehavior: cloudfront.CacheCookieBehavior.none()
        });
        const dynamic: Omit<cloudfront.BehaviorOptions, 'origin'> = {
            viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
            cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
            originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER
        };
        this.distribution = new cloudfront.Distribution(this, 'Distribution', {
            comment: 'Documents: the frontend, the API and its WebSocket',
            defaultBehavior: { origin: albOrigin, ...dynamic },
            additionalBehaviors: {
                '/api/*': {
                    origin: albOrigin,
                    ...dynamic,
                    cachePolicy: apiCachePolicy,
                    originRequestPolicy:
                        cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER
                },
                '/socket.io/*': { origin: albOrigin, ...dynamic },
                '/_next/static/*': {
                    origin: albOrigin,
                    viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED
                }
            },
            priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
            httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
            enableLogging: true,
            logBucket: accessLogs,
            logFilePrefix: 'cloudfront/'
        });
        this.publicOrigin = `https://${this.distribution.distributionDomainName}`;

        // Uploads, parsed documents and thumbnails. The browser puts and gets objects with
        // presigned URLs, so the bucket allows the public origin. Old versions of
        // overwritten or deleted objects expire after 30 days.
        this.bucket = new s3.Bucket(this, 'DocumentsBucket', {
            bucketName: `documents-uploads-${this.account}-${this.region}`,
            versioned: true,
            encryption: s3.BucketEncryption.S3_MANAGED,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            enforceSSL: true,
            serverAccessLogsBucket: accessLogs,
            serverAccessLogsPrefix: 's3/',
            removalPolicy: removal,
            autoDeleteObjects: destroy,
            lifecycleRules: [
                {
                    id: 'expire-old-versions',
                    noncurrentVersionExpiration: cdk.Duration.days(30)
                }
            ],
            cors: [
                {
                    allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.PUT],
                    allowedOrigins: [this.publicOrigin, ...props.extraOrigins],
                    allowedHeaders: ['*'],
                    maxAge: 3000
                }
            ]
        });

        // Documents, conversations and messages (docs/ARCHITECTURE.md, Storage).
        this.table = new dynamodb.Table(this, 'DocumentsTable', {
            tableName: 'documents-documents',
            partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
            encryption: dynamodb.TableEncryption.AWS_MANAGED,
            removalPolicy: removal
        });
        this.table.addGlobalSecondaryIndex({
            indexName: 'GSI1',
            partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
            projectionType: dynamodb.ProjectionType.ALL
        });

        // The processing queue: S3 events for uploads and deletions under uploads/. The
        // worker extends a message's visibility while a long document is processed.
        this.processingDlq = new sqs.Queue(this, 'ProcessingDlq', {
            queueName: 'documents-dlq',
            retentionPeriod: cdk.Duration.days(14),
            enforceSSL: true
        });
        this.processingQueue = new sqs.Queue(this, 'ProcessingQueue', {
            queueName: 'documents-processing',
            visibilityTimeout: cdk.Duration.seconds(120),
            retentionPeriod: cdk.Duration.days(4),
            deadLetterQueue: { queue: this.processingDlq, maxReceiveCount: 3 },
            enforceSSL: true
        });
        for (const event of [s3.EventType.OBJECT_CREATED, s3.EventType.OBJECT_REMOVED]) {
            this.bucket.addEventNotification(
                event,
                new s3Notifications.SqsDestination(this.processingQueue),
                { prefix: 'uploads/' }
            );
        }

        // Status events: the worker publishes, the API forwards them over its WebSocket.
        this.notificationsTopic = new sns.Topic(this, 'NotificationsTopic', {
            topicName: 'documents-notifications',
            enforceSSL: true
        });
        this.notificationsQueue = new sqs.Queue(this, 'ApiNotificationsQueue', {
            queueName: 'documents-api-notifications',
            retentionPeriod: cdk.Duration.minutes(5),
            enforceSSL: true
        });
        this.notificationsTopic.addSubscription(
            new snsSubscriptions.SqsSubscription(this.notificationsQueue, {
                rawMessageDelivery: false
            })
        );
        this.alertsTopic = new sns.Topic(this, 'AlertsTopic', {
            topicName: 'documents-alerts',
            enforceSSL: true
        });

        // Images: tagged by commit and never overwritten.
        this.repositories = {} as Record<BuiltImage, ecr.Repository>;
        for (const image of BUILT_IMAGES) {
            this.repositories[image] = new ecr.Repository(this, `${image}Repo`, {
                repositoryName: `documents/${image}`,
                imageTagMutability: ecr.TagMutability.IMMUTABLE,
                imageScanOnPush: true,
                removalPolicy: removal,
                emptyOnDelete: destroy,
                lifecycleRules: [{ description: 'Keep the last 10 images', maxImageCount: 10 }]
            });
        }

        this.logGroups = {} as Record<ServiceName, logs.LogGroup>;
        for (const service of SERVICES) {
            this.logGroups[service] = new logs.LogGroup(this, `${service}Logs`, {
                logGroupName: `/documents/${service}`,
                retention: logs.RetentionDays.ONE_MONTH,
                removalPolicy: cdk.RemovalPolicy.DESTROY
            });
        }

        const secret = (
            id: string,
            name: string,
            description: string,
            generate?: secretsmanager.SecretStringGenerator
        ) =>
            new secretsmanager.Secret(this, id, {
                secretName: name,
                description,
                generateSecretString: generate,
                removalPolicy: removal
            });
        this.secrets = {
            gemini: secret(
                'GeminiApiKey',
                'documents/gemini-api-key',
                'Gemini API key (the gateway only)'
            ),
            qdrant: secret('QdrantApiKey', 'documents/qdrant-api-key', 'Qdrant Cloud API key'),
            langfuse: secret(
                'Langfuse',
                'documents/langfuse',
                'Langfuse project keys, as JSON with publicKey and secretKey',
                {
                    secretStringTemplate: JSON.stringify({ publicKey: 'set-after-deploy' }),
                    generateStringKey: 'secretKey'
                }
            ),
            masking: secret(
                'MaskingKey',
                'documents/masking-key',
                'Keys the hashes that replace identifiers in traces',
                { passwordLength: 64, excludePunctuation: true }
            ),
            gatewayMaster: secret(
                'LitellmMasterKey',
                'documents/litellm-master-key',
                "The gateway's admin key (sk-...)"
            ),
            gatewayKey: secret(
                'LlmGatewayKey',
                'documents/llm-gateway-key',
                "The services' virtual key in the gateway (sk-...)"
            )
        };

        // The gateway's budgets and keys (Stage 10b). One small instance in the isolated
        // subnets; LiteLLM joins its host, user, password and name into a URL.
        this.gatewayDb = new rds.DatabaseInstance(this, 'GatewayDb', {
            engine: rds.DatabaseInstanceEngine.postgres({
                version: rds.PostgresEngineVersion.VER_18
            }),
            instanceType: ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.MICRO),
            vpc: this.vpc,
            vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
            databaseName: 'litellm',
            credentials: rds.Credentials.fromGeneratedSecret('litellm', {
                secretName: 'documents/llm-gateway-db'
            }),
            allocatedStorage: 20,
            storageType: rds.StorageType.GP3,
            storageEncrypted: true,
            multiAz: false,
            publiclyAccessible: false,
            backupRetention: cdk.Duration.days(7),
            deletionProtection: !destroy,
            removalPolicy: destroy ? cdk.RemovalPolicy.DESTROY : cdk.RemovalPolicy.SNAPSHOT
        });

        new cdk.CfnOutput(this, 'PublicUrl', {
            value: this.publicOrigin,
            description: 'The frontend; deploy.yml builds it with this address'
        });
        new cdk.CfnOutput(this, 'BucketName', { value: this.bucket.bucketName });
        new cdk.CfnOutput(this, 'AlertsTopicArn', { value: this.alertsTopic.topicArn });
        for (const [image, repo] of Object.entries(this.repositories)) {
            new cdk.CfnOutput(this, `${image}RepoUri`, { value: repo.repositoryUri });
        }
    }
}
