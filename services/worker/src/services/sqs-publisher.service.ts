import { Injectable, Inject, Logger } from '@nestjs/common';
import { SQSClient, SendMessageCommand } from '@aws-sdk/client-sqs';
import { ThumbnailJobMessage } from '@documents/shared';

@Injectable()
export class SqsPublisherService {
    private readonly logger = new Logger(SqsPublisherService.name);
    private readonly thumbnailQueueUrl = process.env.SQS_THUMBNAIL_QUEUE_URL;

    constructor(@Inject('SQS_CLIENT') private readonly sqs: SQSClient) {}

    async publishThumbnailJob(job: ThumbnailJobMessage): Promise<void> {
        await this.sqs.send(
            new SendMessageCommand({
                QueueUrl: this.thumbnailQueueUrl,
                MessageBody: JSON.stringify(job)
            })
        );

        this.logger.log(`Thumbnail job enqueued for doc ${job.documentId}`);
    }
}
