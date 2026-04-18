import { Injectable, Logger, Inject, OnModuleInit, OnModuleDestroy } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import {
    SQSClient,
    ReceiveMessageCommand,
    DeleteMessageCommand,
    Message
} from '@aws-sdk/client-sqs';
import { ThumbnailJobMessage } from '@documents/shared';
import { ThumbnailService } from './thumbnail.service';

@Injectable()
export class ThumbnailPollService implements OnModuleInit, OnModuleDestroy {
    private readonly logger = new Logger(ThumbnailPollService.name);
    private readonly queueUrl: string;
    private isPolling = false;
    private isShuttingDown = false;
    private pollTimer: NodeJS.Timeout | null = null;

    constructor(
        @Inject('SQS_CLIENT') private readonly sqs: SQSClient,
        private readonly thumbnailService: ThumbnailService,
        config: ConfigService
    ) {
        this.queueUrl =
            config.get<string>('aws.sqs.thumbnailQueueUrl') ||
            process.env.SQS_THUMBNAIL_QUEUE_URL ||
            'http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/documents-thumbnail-jobs';
    }

    onModuleInit() {
        this.isPolling = true;
        this.poll();
    }

    onModuleDestroy() {
        this.isShuttingDown = true;
        this.isPolling = false;

        if (this.pollTimer) {
            clearTimeout(this.pollTimer);
        }
    }

    private async poll() {
        if (!this.isPolling || this.isShuttingDown) return;

        try {
            const result = await this.sqs.send(
                new ReceiveMessageCommand({
                    QueueUrl: this.queueUrl,
                    MaxNumberOfMessages: 5,
                    WaitTimeSeconds: 10,
                    VisibilityTimeout: 120
                })
            );

            const messages = result.Messages || [];

            if (messages.length > 0) {
                await Promise.allSettled(messages.map((m) => this.handleMessage(m)));
            }
        } catch (error) {
            this.logger.error(`Thumbnail poll error: ${error}`);
        }

        if (!this.isShuttingDown) {
            this.pollTimer = setTimeout(() => this.poll(), 5000);
        }
    }

    private async handleMessage(msg: Message): Promise<void> {
        let job: ThumbnailJobMessage;

        try {
            job = JSON.parse(msg.Body!) as ThumbnailJobMessage;
        } catch {
            this.logger.error(`Invalid thumbnail message: ${msg.Body}`);
            await this.deleteMessage(msg);
            return;
        }

        try {
            await this.thumbnailService.process(
                job.userId,
                job.documentId,
                job.s3Key,
                job.mimeType
            );

            await this.deleteMessage(msg);
        } catch (error) {
            this.logger.error(`Thumbnail generation failed for ${job.documentId}: ${error}`);
        }
    }

    private async deleteMessage(msg: Message): Promise<void> {
        await this.sqs.send(
            new DeleteMessageCommand({
                QueueUrl: this.queueUrl,
                ReceiptHandle: msg.ReceiptHandle!
            })
        );
    }
}
