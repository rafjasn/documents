import { Injectable, Logger, Inject, OnModuleInit, OnModuleDestroy } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import {
    SQSClient,
    ReceiveMessageCommand,
    DeleteMessageCommand,
    CreateQueueCommand,
    GetQueueAttributesCommand
} from '@aws-sdk/client-sqs';
import { SNSClient, SubscribeCommand, ListSubscriptionsByTopicCommand } from '@aws-sdk/client-sns';
import { WsDocumentStatusEvent, DocumentStatus } from '@documents/shared';
import { DocumentGateway } from './document.gateway';

@Injectable()
export class NotificationsConsumerService implements OnModuleInit, OnModuleDestroy {
    private readonly logger = new Logger(NotificationsConsumerService.name);
    private pollTimer: NodeJS.Timeout | null = null;
    private isPolling = false;
    private isShuttingDown = false;

    private readonly snsTopicArn: string;
    private readonly endpoint?: string;
    private readonly queueUrl: string;
    private readonly pollingIntervalMs: number;

    constructor(
        @Inject('SQS_CLIENT') private readonly sqs: SQSClient,
        @Inject('SNS_CLIENT') private readonly sns: SNSClient,
        private readonly gateway: DocumentGateway,
        config: ConfigService
    ) {
        this.snsTopicArn = config.get<string>('aws.sns.topicArn')!;
        this.endpoint = config.get<string>('aws.endpoint');
        this.pollingIntervalMs = 5000;
        this.queueUrl = config.get<string>('aws.sqs.apiNotificationsQueueUrl')!;
    }

    async onModuleInit() {
        if (this.endpoint) {
            await this.ensureQueueAndSubscription();
        } else {
            this.logger.log('Using pre-provisioned notifications queue');
        }
        this.isPolling = true;
        this.logger.log('Starting notifications consumer...');
        this.poll();
    }

    onModuleDestroy() {
        this.isShuttingDown = true;
        this.isPolling = false;

        if (this.pollTimer) {
            clearTimeout(this.pollTimer);
        }

        this.logger.log('Notifications consumer stopped');
    }

    private async ensureQueueAndSubscription(): Promise<void> {
        try {
            await this.sqs.send(
                new CreateQueueCommand({ QueueName: 'documents-api-notifications' })
            );

            this.logger.log('Notifications SQS queue ready');

            const attrs = await this.sqs.send(
                new GetQueueAttributesCommand({
                    QueueUrl: this.queueUrl,
                    AttributeNames: ['QueueArn']
                })
            );

            const queueArn = attrs.Attributes?.QueueArn;

            if (!queueArn) {
                this.logger.warn('Could not get queue ARN — skipping SNS subscription');
                return;
            }

            const subs = await this.sns.send(
                new ListSubscriptionsByTopicCommand({ TopicArn: this.snsTopicArn })
            );

            const alreadySubscribed = (subs.Subscriptions || []).some(
                (s) => s.Endpoint === queueArn && s.Protocol === 'sqs'
            );

            if (!alreadySubscribed) {
                await this.sns.send(
                    new SubscribeCommand({
                        TopicArn: this.snsTopicArn,
                        Protocol: 'sqs',
                        Endpoint: queueArn
                    })
                );
                this.logger.log('Subscribed API notifications queue to SNS topic');
            } else {
                this.logger.log('SNS subscription already exists');
            }
        } catch (error) {
            this.logger.error(`Failed to set up notifications queue: ${error}`);
        }
    }

    private async poll(): Promise<void> {
        if (!this.isPolling || this.isShuttingDown) {
            return;
        }

        try {
            const result = await this.sqs.send(
                new ReceiveMessageCommand({
                    QueueUrl: this.queueUrl,
                    MaxNumberOfMessages: 10,
                    WaitTimeSeconds: 5,
                    VisibilityTimeout: 30
                })
            );

            const messages = result.Messages || [];

            for (const message of messages) {
                await this.handleMessage(message);
            }
        } catch (error) {
            this.logger.error(`Notifications poll error: ${error}`);
        }

        this.pollTimer = setTimeout(() => this.poll(), this.pollingIntervalMs);
    }

    private async handleMessage(message: any): Promise<void> {
        try {
            const body = JSON.parse(message.Body || '{}');

            let event: WsDocumentStatusEvent;
            let userId: string | undefined;

            if (body.Type === 'Notification') {
                event = JSON.parse(body.Message) as WsDocumentStatusEvent;
                userId = body.MessageAttributes?.userId?.Value;
            } else {
                event = body as WsDocumentStatusEvent;
                userId = body.userId;
            }

            if (!userId || !event.documentId || !event.status) {
                this.logger.warn(`Skipping malformed notification: ${message.Body}`);
            } else {
                this.gateway.notifyStatusChange(
                    userId,
                    event.documentId,
                    event.status as DocumentStatus,
                    event.document
                );
                this.logger.log(
                    `Pushed ${event.status} for doc ${event.documentId} to user ${userId}`
                );
            }
        } catch (error) {
            this.logger.error(`Failed to handle notification: ${error}`);
        } finally {
            if (message.ReceiptHandle) {
                await this.sqs
                    .send(
                        new DeleteMessageCommand({
                            QueueUrl: this.queueUrl,
                            ReceiptHandle: message.ReceiptHandle
                        })
                    )
                    .catch(() => {});
            }
        }
    }
}
