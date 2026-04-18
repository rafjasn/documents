import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatchActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import { Construct } from 'constructs';

export interface DocumentsInfraStackProps extends cdk.StackProps {
    removalPolicy?: cdk.RemovalPolicy;
    /**
     * Origins allowed to issue CORS requests against the documents bucket
     * (presigned PUT/GET from the browser). Must include the frontend origin —
     * e.g. the ALB DNS, the CloudFront domain, or a custom domain.
     */
    allowedOrigins: string[];
}

export class DocumentsInfraStack extends cdk.Stack {
    readonly vpc: ec2.Vpc;
    readonly bucket: s3.Bucket;
    readonly table: dynamodb.Table;
    readonly processingQueue: sqs.Queue;
    readonly thumbnailQueue: sqs.Queue;
    readonly apiNotificationsQueue: sqs.Queue;
    readonly notificationsTopic: sns.Topic;
    readonly repositories: Record<'api' | 'worker' | 'frontend', ecr.Repository>;
    readonly taskRole: iam.Role;
    readonly logGroups: Record<'api' | 'worker' | 'ai', logs.LogGroup>;

    constructor(scope: Construct, id: string, props: DocumentsInfraStackProps) {
        super(scope, id, props);

        const removal = props.removalPolicy ?? cdk.RemovalPolicy.RETAIN;

        // VPC
        this.vpc = new ec2.Vpc(this, 'Vpc', {
            maxAzs: 2,
            natGateways: 1,
            subnetConfiguration: [
                {
                    name: 'Public',
                    subnetType: ec2.SubnetType.PUBLIC,
                    cidrMask: 24
                },
                {
                    name: 'Private',
                    subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidrMask: 24
                }
            ]
        });

        // S3
        this.bucket = new s3.Bucket(this, 'DocumentsBucket', {
            bucketName: `documents-uploads-${this.account}-${this.region}`,
            versioned: true,
            encryption: s3.BucketEncryption.S3_MANAGED,
            blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
            removalPolicy: removal,
            autoDeleteObjects: removal === cdk.RemovalPolicy.DESTROY,
            lifecycleRules: [
                {
                    id: 'transition-to-ia',
                    transitions: [
                        {
                            storageClass: s3.StorageClass.INFREQUENT_ACCESS,
                            transitionAfter: cdk.Duration.days(30)
                        },
                        {
                            storageClass: s3.StorageClass.GLACIER,
                            transitionAfter: cdk.Duration.days(90)
                        }
                    ]
                }
            ],
            cors: [
                {
                    allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.PUT, s3.HttpMethods.POST],
                    allowedOrigins: props.allowedOrigins,
                    allowedHeaders: ['*'],
                    maxAge: 3000
                }
            ]
        });

        // DynamoDB
        this.table = new dynamodb.Table(this, 'DocumentsTable', {
            tableName: 'documents-documents',
            partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
            billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
            pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
            encryption: dynamodb.TableEncryption.AWS_MANAGED,
            removalPolicy: removal
        });

        // GSI1
        this.table.addGlobalSecondaryIndex({
            indexName: 'GSI1',
            partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
            sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
            projectionType: dynamodb.ProjectionType.ALL
        });

        // SQS processing
        const processingDlq = new sqs.Queue(this, 'ProcessingDlq', {
            queueName: 'documents-dlq',
            retentionPeriod: cdk.Duration.days(14)
        });

        this.processingQueue = new sqs.Queue(this, 'ProcessingQueue', {
            queueName: 'documents-processing',
            visibilityTimeout: cdk.Duration.seconds(120),
            retentionPeriod: cdk.Duration.days(4),
            deadLetterQueue: { queue: processingDlq, maxReceiveCount: 3 }
        });

        // SQS thumbnails
        const thumbnailDlq = new sqs.Queue(this, 'ThumbnailDlq', {
            queueName: 'documents-thumbnail-dlq',
            retentionPeriod: cdk.Duration.days(14)
        });

        this.thumbnailQueue = new sqs.Queue(this, 'ThumbnailQueue', {
            queueName: 'documents-thumbnail-jobs',
            visibilityTimeout: cdk.Duration.seconds(120),
            retentionPeriod: cdk.Duration.days(4),
            deadLetterQueue: { queue: thumbnailDlq, maxReceiveCount: 3 }
        });

        // SQS notifications
        this.apiNotificationsQueue = new sqs.Queue(this, 'ApiNotificationsQueue', {
            queueName: 'documents-api-notifications',
            retentionPeriod: cdk.Duration.minutes(5)
        });

        // SNS
        this.notificationsTopic = new sns.Topic(this, 'NotificationsTopic', {
            topicName: 'documents-notifications'
        });

        this.notificationsTopic.addSubscription(
            new snsSubscriptions.SqsSubscription(this.apiNotificationsQueue, {
                rawMessageDelivery: false
            })
        );

        // ECR
        const serviceNames = ['api', 'worker', 'frontend'] as const;
        this.repositories = {} as typeof this.repositories;

        for (const service of serviceNames) {
            const repo = new ecr.Repository(this, `${service}Repo`, {
                repositoryName: `documents/${service}`,
                removalPolicy: removal,
                emptyOnDelete: removal === cdk.RemovalPolicy.DESTROY,
                lifecycleRules: [
                    {
                        description: 'Keep last 10 images',
                        maxImageCount: 10
                    }
                ]
            });

            this.repositories[service] = repo;
        }

        // CloudWatch
        this.logGroups = {
            api: new logs.LogGroup(this, 'ApiLogGroup', {
                logGroupName: '/documents/api',
                retention: logs.RetentionDays.ONE_MONTH,
                removalPolicy: cdk.RemovalPolicy.DESTROY
            }),
            worker: new logs.LogGroup(this, 'WorkerLogGroup', {
                logGroupName: '/documents/worker',
                retention: logs.RetentionDays.ONE_MONTH,
                removalPolicy: cdk.RemovalPolicy.DESTROY
            }),
            ai: new logs.LogGroup(this, 'AiLogGroup', {
                logGroupName: '/documents/ai',
                retention: logs.RetentionDays.ONE_MONTH,
                removalPolicy: cdk.RemovalPolicy.DESTROY
            })
        };

        // Alarms
        const alertsTopic = new sns.Topic(this, 'AlertsTopic', {
            topicName: 'documents-alerts'
        });

        const appServices = ['api', 'worker', 'ai'] as const;

        for (const svc of appServices) {
            const lg = this.logGroups[svc];
            const svcTitle = svc.charAt(0).toUpperCase() + svc.slice(1);

            // ERROR log count
            const errorFilter = new logs.MetricFilter(this, `${svcTitle}ErrorFilter`, {
                logGroup: lg,
                filterName: `${svc}-errors`,
                filterPattern: logs.FilterPattern.literal('{ $.level = "ERROR" }'),
                metricNamespace: 'Documents',
                metricName: `${svcTitle}ErrorCount`,
                metricValue: '1',
                defaultValue: 0
            });

            new cloudwatch.Alarm(this, `${svcTitle}ErrorAlarm`, {
                alarmName: `documents-${svc}-errors`,
                alarmDescription: `More than 10 ERROR logs in 5 minutes from the ${svc} service`,
                metric: errorFilter.metric({
                    period: cdk.Duration.minutes(5),
                    statistic: 'Sum'
                }),
                threshold: 10,
                evaluationPeriods: 1,
                comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING
            }).addAlarmAction(new cloudwatchActions.SnsAction(alertsTopic));

            // FATAL log count
            const fatalFilter = new logs.MetricFilter(this, `${svcTitle}FatalFilter`, {
                logGroup: lg,
                filterName: `${svc}-fatals`,
                filterPattern: logs.FilterPattern.literal('{ $.level = "FATAL" }'),
                metricNamespace: 'Documents',
                metricName: `${svcTitle}FatalCount`,
                metricValue: '1',
                defaultValue: 0
            });

            new cloudwatch.Alarm(this, `${svcTitle}FatalAlarm`, {
                alarmName: `documents-${svc}-fatal`,
                alarmDescription: `Any FATAL log from the ${svc} service`,
                metric: fatalFilter.metric({
                    period: cdk.Duration.minutes(1),
                    statistic: 'Sum'
                }),
                threshold: 0,
                evaluationPeriods: 1,
                comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING
            }).addAlarmAction(new cloudwatchActions.SnsAction(alertsTopic));
        }

        // DLQ depth alarms
        const dlqs: [string, sqs.Queue][] = [
            ['processing', processingDlq],
            ['thumbnail', thumbnailDlq]
        ];

        for (const [name, dlq] of dlqs) {
            new cloudwatch.Alarm(this, `${name}DlqAlarm`, {
                alarmName: `documents-${name}-dlq-depth`,
                alarmDescription: `Messages visible in the ${name} DLQ — jobs are failing repeatedly`,
                metric: dlq.metricApproximateNumberOfMessagesVisible({
                    period: cdk.Duration.minutes(5),
                    statistic: 'Maximum'
                }),
                threshold: 0,
                evaluationPeriods: 1,
                comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING
            }).addAlarmAction(new cloudwatchActions.SnsAction(alertsTopic));
        }

        // IAM ECS task role
        this.taskRole = new iam.Role(this, 'TaskRole', {
            roleName: 'documents-ecs-task-role',
            assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com')
        });

        this.bucket.grantReadWrite(this.taskRole);
        this.table.grantReadWriteData(this.taskRole);

        for (const queue of [
            this.processingQueue,
            this.thumbnailQueue,
            this.apiNotificationsQueue
        ]) {
            queue.grantSendMessages(this.taskRole);
            queue.grantConsumeMessages(this.taskRole);
        }

        this.notificationsTopic.grantPublish(this.taskRole);

        // CloudWatch Logs — needed by the ECS execution role (awslogs driver)
        this.taskRole.addToPolicy(
            new iam.PolicyStatement({
                actions: [
                    'logs:CreateLogGroup',
                    'logs:CreateLogStream',
                    'logs:PutLogEvents',
                    'logs:DescribeLogStreams'
                ],
                resources: Object.values(this.logGroups).map((lg) => `${lg.logGroupArn}:*`)
            })
        );

        this.taskRole.addToPolicy(
            new iam.PolicyStatement({
                actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
                resources: [
                    `arn:aws:bedrock:${this.region}::foundation-model/anthropic.claude-3-haiku-20240307-v1:0`
                ]
            })
        );

        this.taskRole.addToPolicy(
            new iam.PolicyStatement({
                actions: ['secretsmanager:GetSecretValue'],
                resources: [
                    `arn:aws:secretsmanager:${this.region}:${this.account}:secret:documents/*`
                ]
            })
        );

        new cdk.CfnOutput(this, 'BucketName', { value: this.bucket.bucketName });
        new cdk.CfnOutput(this, 'TableName', { value: this.table.tableName });
        new cdk.CfnOutput(this, 'ProcessingQueueUrl', { value: this.processingQueue.queueUrl });
        new cdk.CfnOutput(this, 'ThumbnailQueueUrl', { value: this.thumbnailQueue.queueUrl });
        new cdk.CfnOutput(this, 'SnsTopicArn', { value: this.notificationsTopic.topicArn });
        new cdk.CfnOutput(this, 'AlertsTopicArn', { value: alertsTopic.topicArn });

        for (const [svc, repo] of Object.entries(this.repositories)) {
            new cdk.CfnOutput(this, `${svc}RepoUri`, { value: repo.repositoryUri });
        }

        for (const [svc, lg] of Object.entries(this.logGroups)) {
            new cdk.CfnOutput(this, `${svc}LogGroup`, { value: lg.logGroupName });
        }
    }
}
