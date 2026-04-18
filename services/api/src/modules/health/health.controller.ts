import { Controller, Get, Inject } from '@nestjs/common';
import { ApiTags, ApiOperation } from '@nestjs/swagger';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { S3Client, HeadBucketCommand } from '@aws-sdk/client-s3';
import { SQSClient, GetQueueAttributesCommand } from '@aws-sdk/client-sqs';
import type { ConfigType } from '@nestjs/config';
import { awsConfig } from '../../config/index';

@ApiTags('health')
@Controller('health')
export class HealthController {
    constructor(
        @Inject('DYNAMODB_CLIENT')
        private readonly dynamodb: DynamoDBDocumentClient,
        @Inject('S3_CLIENT') private readonly s3: S3Client,
        @Inject('SQS_CLIENT') private readonly sqs: SQSClient,
        @Inject(awsConfig.KEY) private readonly aws: ConfigType<typeof awsConfig>
    ) {}

    @Get()
    @ApiOperation({ summary: 'Health check' })
    async check() {
        const checks: Record<string, { status: string; latency?: number; error?: string }> = {};

        // S3
        const s3Start = Date.now();
        try {
            await this.s3.send(new HeadBucketCommand({ Bucket: this.aws.s3.bucket }));
            checks.s3 = { status: 'healthy', latency: Date.now() - s3Start };
        } catch (err) {
            checks.s3 = {
                status: 'unhealthy',
                error: (err as Error).message,
                latency: Date.now() - s3Start
            };
        }

        // SQS
        const sqsStart = Date.now();
        try {
            await this.sqs.send(
                new GetQueueAttributesCommand({
                    QueueUrl: this.aws.sqs.queueUrl,
                    AttributeNames: ['ApproximateNumberOfMessages']
                })
            );
            checks.sqs = { status: 'healthy', latency: Date.now() - sqsStart };
        } catch (err) {
            checks.sqs = {
                status: 'unhealthy',
                error: (err as Error).message,
                latency: Date.now() - sqsStart
            };
        }

        const allHealthy = Object.values(checks).every((c) => c.status === 'healthy');

        return {
            status: allHealthy ? 'healthy' : 'degraded',
            timestamp: new Date().toISOString(),
            services: checks
        };
    }

    @Get('ready')
    @ApiOperation({ summary: 'Readiness probe' })
    ready() {
        return { status: 'ready' };
    }
}
