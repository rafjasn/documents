import { registerAs } from '@nestjs/config';

const awsRegion = process.env.AWS_REGION || 'us-east-1';
const awsEndpoint =
    process.env.AWS_ENDPOINT ||
    (process.env.NODE_ENV === 'production' ? undefined : 'http://localhost:4566');

function resolveAwsCredentials(endpoint?: string) {
    if (process.env.AWS_ACCESS_KEY_ID && process.env.AWS_SECRET_ACCESS_KEY) {
        return {
            accessKeyId: process.env.AWS_ACCESS_KEY_ID,
            secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY
        };
    }

    if (endpoint?.includes('localhost') || endpoint?.includes('localstack')) {
        return {
            accessKeyId: 'test',
            secretAccessKey: 'test'
        };
    }

    return undefined;
}

export const awsConfig = registerAs('aws', () => ({
    region: awsRegion,
    endpoint: awsEndpoint,
    credentials: resolveAwsCredentials(awsEndpoint),
    s3: {
        bucket: process.env.S3_BUCKET || 'documents-uploads',
        forcePathStyle: Boolean(awsEndpoint) // required for LocalStack
    },
    dynamodb: {
        tableName: process.env.DYNAMODB_TABLE || 'documents-documents'
    },
    sqs: {
        queueUrl:
            process.env.SQS_QUEUE_URL ||
            'http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/documents-processing',
        dlqUrl:
            process.env.SQS_DLQ_URL ||
            'http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/documents-dlq',
        pollingInterval: parseInt(process.env.SQS_POLLING_INTERVAL || '5000', 10),
        maxMessages: parseInt(process.env.SQS_MAX_MESSAGES || '5', 10)
    },
    sns: {
        topicArn:
            process.env.SNS_TOPIC_ARN ||
            'arn:aws:sns:us-east-1:000000000000:documents-notifications'
    }
}));

export const appConfig = registerAs('app', () => ({
    port: parseInt(process.env.PORT || '3001', 10),
    jwtSecret: process.env.JWT_SECRET || 'dev-secret-change-in-production',
    jwtExpiration: process.env.JWT_EXPIRATION || '24h',
    corsOrigin: process.env.CORS_ORIGIN || 'http://localhost:3000',
    maxFileSizeMb: parseInt(process.env.MAX_FILE_SIZE_MB || '50', 10),
    allowedMimeTypes: [
        'application/pdf',
        'image/png',
        'image/jpeg',
        'image/webp',
        'text/plain',
        'text/csv',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    ]
}));
