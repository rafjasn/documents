import * as path from 'path';
import { Injectable, Logger, NotFoundException, BadRequestException, Inject } from '@nestjs/common';
import type { ConfigType } from '@nestjs/config';
import {
    DynamoDBDocumentClient,
    PutCommand,
    GetCommand,
    QueryCommand,
    UpdateCommand
} from '@aws-sdk/lib-dynamodb';
import {
    S3Client,
    PutObjectCommand,
    GetObjectCommand,
    DeleteObjectCommand
} from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { createPresignedPost } from '@aws-sdk/s3-presigned-post';
import { v4 as uuid } from 'uuid';
import { awsConfig, appConfig } from '../../config/index';
import { DocumentRecord, DocumentStatus, PaginatedResult } from '@documents/shared';
import { DocumentEntity } from './document.entity';
import { ListDocumentsDto } from './document.dto';

@Injectable()
export class DocumentsService {
    private readonly logger = new Logger(DocumentsService.name);

    constructor(
        @Inject('DYNAMODB_CLIENT')
        private readonly dynamodb: DynamoDBDocumentClient,
        @Inject('S3_CLIENT')
        private readonly s3: S3Client,
        @Inject(awsConfig.KEY)
        private readonly aws: ConfigType<typeof awsConfig>,
        @Inject(appConfig.KEY)
        private readonly app: ConfigType<typeof appConfig>
    ) {}

    async uploadDocument(
        userId: string,
        file: Express.Multer.File,
        displayName?: string
    ): Promise<DocumentRecord> {
        if (!this.app.allowedMimeTypes.includes(file.mimetype)) {
            throw new BadRequestException(
                `File type ${file.mimetype} is not supported. Allowed: ${this.app.allowedMimeTypes.join(', ')}`
            );
        }

        const maxBytes = this.app.maxFileSizeMb * 1024 * 1024;

        if (file.size > maxBytes) {
            throw new BadRequestException(
                `File too large. Maximum size: ${this.app.maxFileSizeMb}MB`
            );
        }

        const documentId = uuid();
        const now = new Date().toISOString();
        const safeName = path.basename(file.originalname).replace(/[^a-zA-Z0-9._-]/g, '_');
        const s3Key = `uploads/${userId}/${documentId}/${safeName}`;

        this.logger.log(`Uploading ${s3Key} to S3 (${file.size} bytes)`);

        await this.s3.send(
            new PutObjectCommand({
                Bucket: this.aws.s3.bucket,
                Key: s3Key,
                Body: file.buffer,
                ContentType: file.mimetype,
                Metadata: {
                    documentId,
                    userId,
                    originalName: file.originalname
                }
            })
        );

        const record: DocumentRecord = {
            id: documentId,
            userId,
            fileName: displayName || safeName,
            originalName: safeName,
            mimeType: file.mimetype,
            fileSize: file.size,
            s3Key,
            status: DocumentStatus.PENDING,
            retryCount: 0,
            createdAt: now,
            updatedAt: now
        };

        const entity = DocumentEntity.fromRecord(record);

        await this.dynamodb.send(
            new PutCommand({
                TableName: this.aws.dynamodb.tableName,
                Item: entity
            })
        );

        this.logger.log(`Document ${documentId} created for user ${userId}`);
        return record;
    }

    async getDocument(userId: string, documentId: string): Promise<DocumentRecord> {
        const result = await this.dynamodb.send(
            new GetCommand({
                TableName: this.aws.dynamodb.tableName,
                Key: {
                    PK: `USER#${userId}`,
                    SK: `DOC#${documentId}`
                }
            })
        );

        if (!result.Item) {
            throw new NotFoundException(`Document ${documentId} not found`);
        }

        return DocumentEntity.toRecord(result.Item);
    }

    async listDocuments(
        userId: string,
        filters: ListDocumentsDto
    ): Promise<PaginatedResult<DocumentRecord>> {
        const params: any = {
            TableName: this.aws.dynamodb.tableName,
            KeyConditionExpression: 'PK = :pk AND begins_with(SK, :skPrefix)',
            ExpressionAttributeValues: {
                ':pk': `USER#${userId}`,
                ':skPrefix': 'DOC#'
            },
            Limit: filters.limit || 20,
            ScanIndexForward: false
        };

        if (filters.status) {
            params.FilterExpression = '#status = :status';
            params.ExpressionAttributeNames = { '#status': 'status' };
            params.ExpressionAttributeValues[':status'] = filters.status;
        }

        if (filters.lastKey) {
            params.ExclusiveStartKey = JSON.parse(
                Buffer.from(filters.lastKey, 'base64').toString()
            );
        }

        const result = await this.dynamodb.send(new QueryCommand(params));

        const items = (result.Items || []).map(DocumentEntity.toRecord);
        const lastKey = result.LastEvaluatedKey
            ? Buffer.from(JSON.stringify(result.LastEvaluatedKey)).toString('base64')
            : undefined;

        return { items, total: result.ScannedCount || 0, lastKey };
    }

    async presignUpload(
        userId: string,
        fileName: string,
        mimeType: string,
        fileSize: number,
        displayName?: string
    ): Promise<{ documentId: string; uploadUrl: string; fields: Record<string, string> }> {
        if (!this.app.allowedMimeTypes.includes(mimeType)) {
            throw new BadRequestException(
                `File type ${mimeType} is not supported. Allowed: ${this.app.allowedMimeTypes.join(', ')}`
            );
        }
        const maxBytes = this.app.maxFileSizeMb * 1024 * 1024;

        if (fileSize > maxBytes) {
            throw new BadRequestException(
                `File too large. Maximum size: ${this.app.maxFileSizeMb}MB`
            );
        }

        const documentId = uuid();
        const now = new Date().toISOString();
        const safeName = path.basename(fileName).replace(/[^a-zA-Z0-9._-]/g, '_');
        const s3Key = `uploads/${userId}/${documentId}/${safeName}`;

        const record: DocumentRecord = {
            id: documentId,
            userId,
            fileName: displayName || safeName,
            originalName: safeName,
            mimeType,
            fileSize,
            s3Key,
            status: DocumentStatus.PENDING,
            retryCount: 0,
            createdAt: now,
            updatedAt: now
        };

        await this.dynamodb.send(
            new PutCommand({
                TableName: this.aws.dynamodb.tableName,
                Item: DocumentEntity.fromRecord(record)
            })
        );

        const signingClient = this.createSigningS3Client();

        const { url, fields } = await createPresignedPost(signingClient, {
            Bucket: this.aws.s3.bucket,
            Key: s3Key,
            Conditions: [
                ['content-length-range', 1, maxBytes],
                ['eq', '$Content-Type', mimeType]
            ],
            Fields: {
                'Content-Type': mimeType,
                'x-amz-meta-documentid': documentId,
                'x-amz-meta-userid': userId,
                'x-amz-meta-originalname': safeName
            },
            Expires: 300
        });

        this.logger.log(`Presigned upload URL created for document ${documentId} (${userId})`);
        return { documentId, uploadUrl: url, fields };
    }

    async getThumbnailUrl(userId: string, documentId: string): Promise<string | undefined> {
        const doc = await this.getDocument(userId, documentId);
        if (!doc.thumbnailKey) return undefined;
        return this.signPublicUrl(doc.thumbnailKey);
    }

    async getDownloadUrl(userId: string, documentId: string): Promise<string> {
        const doc = await this.getDocument(userId, documentId);
        return this.signPublicUrl(doc.s3Key);
    }

    private async signPublicUrl(key: string): Promise<string> {
        const signingClient = this.createSigningS3Client();
        return getSignedUrl(
            signingClient,
            new GetObjectCommand({ Bucket: this.aws.s3.bucket, Key: key }),
            { expiresIn: 3600 }
        );
    }

    private createSigningS3Client(): S3Client {
        return new S3Client({
            region: this.aws.region,
            ...(this.aws.s3.publicEndpoint
                ? {
                      endpoint: this.aws.s3.publicEndpoint,
                      forcePathStyle: true
                  }
                : {}),
            ...(this.aws.credentials ? { credentials: this.aws.credentials } : {})
        });
    }

    async updateDocumentStatus(
        userId: string,
        documentId: string,
        status: DocumentStatus,
        extra: Partial<DocumentRecord> = {}
    ): Promise<DocumentRecord> {
        const now = new Date().toISOString();
        const updateExpressions: string[] = [
            '#status = :status',
            'updatedAt = :now',
            'GSI1SK = :gsi1sk'
        ];
        const expressionValues: Record<string, any> = {
            ':status': status,
            ':now': now,
            ':gsi1sk': `STATUS#${status}#${now}`
        };
        const expressionNames: Record<string, string> = {
            '#status': 'status'
        };

        if (extra.category) {
            updateExpressions.push('category = :category');
            expressionValues[':category'] = extra.category;
        }

        if (extra.extractedText !== undefined) {
            updateExpressions.push('extractedText = :extractedText');
            expressionValues[':extractedText'] = extra.extractedText;
        }

        if (extra.metadata) {
            updateExpressions.push('#metadata = :metadata');
            expressionValues[':metadata'] = extra.metadata;
            expressionNames['#metadata'] = 'metadata';
        }

        if (extra.errorMessage) {
            updateExpressions.push('errorMessage = :errorMessage');
            expressionValues[':errorMessage'] = extra.errorMessage;
        }

        if (status === DocumentStatus.PROCESSING) {
            updateExpressions.push('processingStartedAt = :processingStartedAt');
            expressionValues[':processingStartedAt'] = now;
        }

        if (status === DocumentStatus.COMPLETED || status === DocumentStatus.FAILED) {
            updateExpressions.push('processingCompletedAt = :processingCompletedAt');
            expressionValues[':processingCompletedAt'] = now;
        }

        const result = await this.dynamodb.send(
            new UpdateCommand({
                TableName: this.aws.dynamodb.tableName,
                Key: {
                    PK: `USER#${userId}`,
                    SK: `DOC#${documentId}`
                },
                UpdateExpression: `SET ${updateExpressions.join(', ')}`,
                ExpressionAttributeValues: expressionValues,
                ExpressionAttributeNames: expressionNames,
                ReturnValues: 'ALL_NEW'
            })
        );

        return DocumentEntity.toRecord(result.Attributes!);
    }

    async deleteDocument(userId: string, documentId: string): Promise<void> {
        const doc = await this.getDocument(userId, documentId);

        await this.s3.send(
            new DeleteObjectCommand({
                Bucket: this.aws.s3.bucket,
                Key: doc.s3Key
            })
        );

        await this.dynamodb.send(
            new UpdateCommand({
                TableName: this.aws.dynamodb.tableName,
                Key: {
                    PK: `USER#${userId}`,
                    SK: `DOC#${documentId}`
                },
                UpdateExpression: 'SET #status = :status, updatedAt = :now',
                ExpressionAttributeNames: { '#status': 'status' },
                ExpressionAttributeValues: {
                    ':status': 'DELETED',
                    ':now': new Date().toISOString()
                }
            })
        );

        this.logger.log(`Document ${documentId} deleted for user ${userId}`);
    }
}
