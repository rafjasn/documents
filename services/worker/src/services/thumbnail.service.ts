import { Injectable, Logger, Inject } from '@nestjs/common';
import { S3Client, GetObjectCommand, PutObjectCommand } from '@aws-sdk/client-s3';
import { DynamoDBDocumentClient, UpdateCommand, GetCommand } from '@aws-sdk/lib-dynamodb';
import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';
import { ConfigService } from '@nestjs/config';
import { DocumentStatus, WsDocumentStatusEvent } from '@documents/shared';
import sharp from 'sharp';

const THUMB_WIDTH = 200;
const THUMB_HEIGHT = 260;

@Injectable()
export class ThumbnailService {
    private readonly logger = new Logger(ThumbnailService.name);
    private readonly bucket: string;
    private readonly tableName: string;
    private readonly snsTopicArn: string;

    constructor(
        @Inject('S3_CLIENT') private readonly s3: S3Client,
        @Inject('DYNAMODB_CLIENT') private readonly dynamodb: DynamoDBDocumentClient,
        @Inject('SNS_CLIENT') private readonly sns: SNSClient,
        config: ConfigService
    ) {
        this.bucket = config.get<string>('aws.s3.bucket')!;
        this.tableName = config.get<string>('aws.dynamodb.tableName')!;
        this.snsTopicArn = config.get<string>('aws.sns.topicArn')!;
    }

    async process(
        userId: string,
        documentId: string,
        s3Key: string,
        mimeType: string
    ): Promise<void> {
        const buffer = await this.downloadFromS3(s3Key);
        const thumbnail = await this.generate(buffer, mimeType);
        const thumbnailKey = `thumbnails/${userId}/${documentId}.png`;

        await this.s3.send(
            new PutObjectCommand({
                Bucket: this.bucket,
                Key: thumbnailKey,
                Body: thumbnail,
                ContentType: 'image/png'
            })
        );

        const now = new Date().toISOString();

        await this.dynamodb.send(
            new UpdateCommand({
                TableName: this.tableName,
                Key: { PK: `USER#${userId}`, SK: `DOC#${documentId}` },
                UpdateExpression: 'SET thumbnailKey = :key, updatedAt = :now',
                ExpressionAttributeValues: { ':key': thumbnailKey, ':now': now }
            })
        );

        const item = await this.dynamodb.send(
            new GetCommand({
                TableName: this.tableName,
                Key: { PK: `USER#${userId}`, SK: `DOC#${documentId}` },
                ProjectionExpression: '#s',
                ExpressionAttributeNames: { '#s': 'status' }
            })
        );
        const currentStatus = (item.Item?.status as DocumentStatus) ?? DocumentStatus.PROCESSING;

        const event: WsDocumentStatusEvent = {
            documentId,
            status: currentStatus,
            document: { thumbnailKey },
            timestamp: now
        };
        try {
            await this.sns.send(
                new PublishCommand({
                    TopicArn: this.snsTopicArn,
                    Message: JSON.stringify(event),
                    MessageAttributes: {
                        eventType: { DataType: 'String', StringValue: 'DOCUMENT_STATUS' },
                        userId: { DataType: 'String', StringValue: userId }
                    }
                })
            );
        } catch (err) {
            this.logger.warn(`SNS publish failed for thumbnail ${documentId}: ${err}`);
        }

        this.logger.log(`Thumbnail stored at ${thumbnailKey}`);
    }

    private async downloadFromS3(s3Key: string): Promise<Buffer> {
        const response = await this.s3.send(
            new GetObjectCommand({ Bucket: this.bucket, Key: s3Key })
        );
        const chunks: Uint8Array[] = [];
        for await (const chunk of response.Body as AsyncIterable<Uint8Array>) {
            chunks.push(chunk);
        }
        return Buffer.concat(chunks);
    }

    private async generate(buffer: Buffer, mimeType: string): Promise<Buffer> {
        if (mimeType.startsWith('image/')) {
            return sharp(buffer)
                .resize(THUMB_WIDTH, THUMB_HEIGHT, { fit: 'cover', position: 'top' })
                .png()
                .toBuffer();
        }

        return this.generatePlaceholder(mimeType);
    }

    private async generatePlaceholder(mimeType: string): Promise<Buffer> {
        const { bg, label } = this.styleFor(mimeType);
        const svg = `
      <svg width="${THUMB_WIDTH}" height="${THUMB_HEIGHT}" xmlns="http://www.w3.org/2000/svg">
        <rect width="${THUMB_WIDTH}" height="${THUMB_HEIGHT}" fill="${bg}" rx="10"/>
        <!-- Simulated text lines -->
        <rect x="20" y="32" width="120" height="8"  fill="rgba(255,255,255,0.35)" rx="4"/>
        <rect x="20" y="52" width="150" height="6"  fill="rgba(255,255,255,0.20)" rx="3"/>
        <rect x="20" y="66" width="130" height="6"  fill="rgba(255,255,255,0.20)" rx="3"/>
        <rect x="20" y="80" width="140" height="6"  fill="rgba(255,255,255,0.20)" rx="3"/>
        <rect x="20" y="94" width="110" height="6"  fill="rgba(255,255,255,0.20)" rx="3"/>
        <rect x="20" y="116" width="150" height="6" fill="rgba(255,255,255,0.15)" rx="3"/>
        <rect x="20" y="130" width="120" height="6" fill="rgba(255,255,255,0.15)" rx="3"/>
        <rect x="20" y="144" width="135" height="6" fill="rgba(255,255,255,0.15)" rx="3"/>
        <rect x="20" y="158" width="100" height="6" fill="rgba(255,255,255,0.15)" rx="3"/>
        <rect x="20" y="180" width="140" height="6" fill="rgba(255,255,255,0.10)" rx="3"/>
        <rect x="20" y="194" width="115" height="6" fill="rgba(255,255,255,0.10)" rx="3"/>
        <rect x="20" y="208" width="130" height="6" fill="rgba(255,255,255,0.10)" rx="3"/>
        <!-- Type badge -->
        <rect x="55" y="228" width="90" height="22" fill="rgba(0,0,0,0.30)" rx="6"/>
        <text x="100" y="244"
          font-family="'Helvetica Neue',Arial,sans-serif"
          font-size="11" font-weight="700" letter-spacing="1.5"
          fill="white" text-anchor="middle">${label}</text>
      </svg>`;

        return sharp(Buffer.from(svg)).png().toBuffer();
    }

    private styleFor(mimeType: string): { bg: string; label: string } {
        if (mimeType === 'application/pdf') return { bg: '#dc2626', label: 'PDF' };
        if (mimeType.includes('wordprocessingml')) return { bg: '#2563eb', label: 'DOCX' };
        if (mimeType === 'text/plain') return { bg: '#475569', label: 'TXT' };
        if (mimeType === 'text/csv') return { bg: '#16a34a', label: 'CSV' };

        return { bg: '#7c3aed', label: 'FILE' };
    }
}
