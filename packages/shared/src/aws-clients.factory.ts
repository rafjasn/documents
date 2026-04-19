import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { S3Client } from '@aws-sdk/client-s3';
import { SQSClient } from '@aws-sdk/client-sqs';
import { SNSClient } from '@aws-sdk/client-sns';

export interface AwsClientsConfig {
    region: string;
    endpoint?: string;
    credentials?: { accessKeyId: string; secretAccessKey: string };
}

export function createDynamoDBClient(config: AwsClientsConfig) {
    const client = new DynamoDBClient({
        region: config.region,
        ...(config.endpoint ? { endpoint: config.endpoint } : {}),
        ...(config.credentials ? { credentials: config.credentials } : {})
    });
    return DynamoDBDocumentClient.from(client, {
        marshallOptions: { removeUndefinedValues: true }
    });
}

export function createS3Client(config: AwsClientsConfig) {
    return new S3Client({
        region: config.region,
        ...(config.endpoint ? { endpoint: config.endpoint, forcePathStyle: true } : {}),
        ...(config.credentials ? { credentials: config.credentials } : {})
    });
}

export function createSQSClient(config: AwsClientsConfig) {
    return new SQSClient({
        region: config.region,
        ...(config.endpoint ? { endpoint: config.endpoint } : {}),
        ...(config.credentials ? { credentials: config.credentials } : {})
    });
}

export function createSNSClient(config: AwsClientsConfig) {
    return new SNSClient({
        region: config.region,
        ...(config.endpoint ? { endpoint: config.endpoint } : {}),
        ...(config.credentials ? { credentials: config.credentials } : {})
    });
}
