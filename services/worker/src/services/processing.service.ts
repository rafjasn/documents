import { Injectable, Logger, Inject, OnModuleInit, OnModuleDestroy } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import {
    SQSClient,
    ReceiveMessageCommand,
    DeleteMessageCommand,
    Message
} from '@aws-sdk/client-sqs';
import { S3Client, GetObjectCommand } from '@aws-sdk/client-s3';
import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';
import { DynamoDBDocumentClient, UpdateCommand } from '@aws-sdk/lib-dynamodb';
import {
    DocumentStatus,
    DocumentMetadata,
    ProcessingJobMessage,
    WsDocumentStatusEvent
} from '@documents/shared';
import { ExtractionService } from './extraction.service';
import { SqsPublisherService } from './sqs-publisher.service';
import { AiProcessingService } from './ai-processing.service';

@Injectable()
export class ProcessingService implements OnModuleInit, OnModuleDestroy {
    private readonly logger = new Logger(ProcessingService.name);
    private isPolling = false;
    private pollTimer: NodeJS.Timeout | null = null;
    private isShuttingDown = false;
    private consecutiveFailures = 0;
    private readonly maxConsecutiveFailures = 5;
    private readonly circuitBreakerCooldownMs = 30_000;
    private readonly queueUrl: string;
    private readonly snsTopicArn: string;
    private readonly tableName: string;
    private readonly bucketName: string;
    private readonly pollingIntervalMs: number;
    private readonly maxMessages: number;

    constructor(
        @Inject('SQS_CLIENT') private readonly sqs: SQSClient,
        @Inject('S3_CLIENT') private readonly s3: S3Client,
        @Inject('SNS_CLIENT') private readonly sns: SNSClient,
        @Inject('DYNAMODB_CLIENT')
        private readonly dynamodb: DynamoDBDocumentClient,
        private readonly extractionService: ExtractionService,
        private readonly sqsPublisher: SqsPublisherService,
        private readonly aiProcessing: AiProcessingService,
        config: ConfigService
    ) {
        this.queueUrl = config.get<string>('aws.sqs.queueUrl')!;
        this.snsTopicArn = config.get<string>('aws.sns.topicArn')!;
        this.tableName = config.get<string>('aws.dynamodb.tableName')!;
        this.bucketName = config.get<string>('aws.s3.bucket')!;
        this.pollingIntervalMs = config.get<number>('aws.sqs.pollingInterval', 5000);
        this.maxMessages = config.get<number>('aws.sqs.maxMessages', 5);
    }

    onModuleInit() {
        this.startPolling();
    }

    onModuleDestroy() {
        this.stopPolling();
    }

    private startPolling() {
        if (this.isPolling) return;
        this.isPolling = true;
        this.logger.log('Starting SQS polling...');
        this.poll();
    }

    private stopPolling() {
        this.isShuttingDown = true;
        this.isPolling = false;
        if (this.pollTimer) {
            clearTimeout(this.pollTimer);
            this.pollTimer = null;
        }
        this.logger.log('SQS polling stopped');
    }

    private async poll() {
        if (!this.isPolling || this.isShuttingDown) return;

        try {
            const result = await this.sqs.send(
                new ReceiveMessageCommand({
                    QueueUrl: this.queueUrl,
                    MaxNumberOfMessages: this.maxMessages,
                    WaitTimeSeconds: 10,
                    VisibilityTimeout: 120,
                    MessageSystemAttributeNames: ['ApproximateReceiveCount']
                })
            );

            const messages = result.Messages || [];
            if (messages.length > 0) {
                this.logger.log(`Received ${messages.length} message(s)`);
                await Promise.allSettled(messages.map((msg) => this.handleMessage(msg)));
            }

            this.consecutiveFailures = 0;
        } catch (error) {
            this.consecutiveFailures++;
            this.logger.error(
                `Poll error (${this.consecutiveFailures}/${this.maxConsecutiveFailures}): ${error}`
            );

            if (this.consecutiveFailures >= this.maxConsecutiveFailures) {
                this.logger.warn(
                    `Circuit breaker open — cooling down ${this.circuitBreakerCooldownMs}ms`
                );
                await this.sleep(this.circuitBreakerCooldownMs);
                this.consecutiveFailures = 0;
            }
        }

        this.pollTimer = setTimeout(() => this.poll(), this.pollingIntervalMs);
    }

    private async handleMessage(message: Message): Promise<void> {
        const body = JSON.parse(message.Body || '{}');

        const records = body.Records || [];

        for (const record of records) {
            if (!record.s3) continue;

            const s3Key = decodeURIComponent(record.s3.object.key.replace(/\+/g, ' '));

            const parts = s3Key.split('/');
            if (parts.length < 4) {
                this.logger.warn(`Unexpected S3 key format: ${s3Key}`);

                continue;
            }

            const job: ProcessingJobMessage = {
                userId: parts[1],
                documentId: parts[2],
                s3Key,
                mimeType: record.s3.object.contentType || 'application/octet-stream'
            };

            await this.processDocument(job);
        }

        if (message.ReceiptHandle) {
            await this.sqs.send(
                new DeleteMessageCommand({
                    QueueUrl: this.queueUrl,
                    ReceiptHandle: message.ReceiptHandle
                })
            );
        }
    }

    async processDocument(job: ProcessingJobMessage): Promise<void> {
        const { documentId, userId, s3Key, mimeType } = job;
        this.logger.log(`Processing ${documentId}`);

        try {
            await this.updateStatus(userId, documentId, DocumentStatus.PROCESSING);

            await this.publishStatusEvent(userId, documentId, DocumentStatus.PROCESSING);

            const { buffer, mimeType: actualMimeType } = await this.fetchFromS3(s3Key);

            await this.updateStatus(userId, documentId, DocumentStatus.EXTRACTING);

            await this.publishStatusEvent(userId, documentId, DocumentStatus.EXTRACTING);

            const extractedText = await this.extractionService.extractText(buffer, actualMimeType);

            await this.updateStatus(userId, documentId, DocumentStatus.CLASSIFYING, {
                extractedText: extractedText.slice(0, 10_000)
            });

            await this.publishStatusEvent(userId, documentId, DocumentStatus.CLASSIFYING);

            await this.sqsPublisher.publishThumbnailJob({
                documentId,
                userId,
                s3Key,
                mimeType: actualMimeType
            });

            const category = await this.aiProcessing.classify({ documentId, userId });

            await this.updateStatus(userId, documentId, DocumentStatus.SUMMARIZING);
            await this.publishStatusEvent(userId, documentId, DocumentStatus.SUMMARIZING);
            await this.aiProcessing.summarize({ documentId, userId, category });

            this.logger.log(
                `Document ${documentId} processed (category=${category}). Thumbnail job enqueued.`
            );
        } catch (error) {
            const errorMessage = error instanceof Error ? error.message : 'Unknown error';
            this.logger.error(`Failed ${documentId}: ${errorMessage}`);

            await this.updateStatus(userId, documentId, DocumentStatus.FAILED, {
                errorMessage
            });

            await this.publishStatusEvent(userId, documentId, DocumentStatus.FAILED);

            throw error;
        }
    }

    private async fetchFromS3(s3Key: string): Promise<{ buffer: Buffer; mimeType: string }> {
        const response = await this.s3.send(
            new GetObjectCommand({
                Bucket: this.bucketName,
                Key: s3Key
            })
        );

        const bytes = await response.Body?.transformToByteArray();
        if (!bytes || bytes.length === 0) {
            throw new Error(`Empty file body from S3: ${s3Key}`);
        }

        return {
            buffer: Buffer.from(bytes),
            mimeType: response.ContentType || 'application/octet-stream'
        };
    }

    private async updateStatus(
        userId: string,
        documentId: string,
        status: DocumentStatus,
        extra: {
            category?: string;
            extractedText?: string;
            metadata?: DocumentMetadata;
            errorMessage?: string;
        } = {}
    ): Promise<void> {
        const now = new Date().toISOString();

        const updateParts: string[] = ['#status = :status', 'updatedAt = :now', 'GSI1SK = :gsi1sk'];
        const values: Record<string, any> = {
            ':status': status,
            ':now': now,
            ':gsi1sk': `STATUS#${status}#${now}`
        };

        const names: Record<string, string> = {
            '#status': 'status'
        };

        if (extra.category) {
            updateParts.push('category = :category');
            values[':category'] = extra.category;
        }

        if (extra.extractedText !== undefined) {
            updateParts.push('extractedText = :extractedText');
            values[':extractedText'] = extra.extractedText;
        }

        if (extra.metadata) {
            updateParts.push('#metadata = :metadata');
            values[':metadata'] = extra.metadata;
            names['#metadata'] = 'metadata';
        }

        if (extra.errorMessage) {
            updateParts.push('errorMessage = :errorMessage');
            values[':errorMessage'] = extra.errorMessage;
        }

        if (status === DocumentStatus.PROCESSING) {
            updateParts.push('processingStartedAt = :processingStartedAt');
            values[':processingStartedAt'] = now;
        }

        if (status === DocumentStatus.FAILED) {
            updateParts.push('processingCompletedAt = :processingCompletedAt');
            values[':processingCompletedAt'] = now;
        }

        await this.dynamodb.send(
            new UpdateCommand({
                TableName: this.tableName,
                Key: {
                    PK: `USER#${userId}`,
                    SK: `DOC#${documentId}`
                },
                UpdateExpression: `SET ${updateParts.join(', ')}`,
                ExpressionAttributeValues: values,
                ExpressionAttributeNames: names
            })
        );
    }

    private async publishStatusEvent(
        userId: string,
        documentId: string,
        status: DocumentStatus
    ): Promise<void> {
        const event: WsDocumentStatusEvent = {
            documentId,
            status,
            timestamp: new Date().toISOString()
        };

        try {
            await this.sns.send(
                new PublishCommand({
                    TopicArn: this.snsTopicArn,
                    Message: JSON.stringify(event),
                    MessageAttributes: {
                        eventType: {
                            DataType: 'String',
                            StringValue: 'DOCUMENT_STATUS'
                        },
                        userId: {
                            DataType: 'String',
                            StringValue: userId
                        }
                    }
                })
            );
        } catch (error) {
            this.logger.warn(
                `SNS publish failed for ${documentId}: ${error}. ` +
                    `Non-fatal — processing continues.`
            );
        }
    }

    private sleep(ms: number): Promise<void> {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }
}
