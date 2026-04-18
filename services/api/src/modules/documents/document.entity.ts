import { DocumentMetadata, DocumentRecord, DocumentStatus } from '@documents/shared';

export class DocumentEntity implements DocumentRecord {
    id!: string;
    userId!: string;
    fileName!: string;
    originalName!: string;
    mimeType!: string;
    fileSize!: number;
    s3Key!: string;
    status!: DocumentStatus;
    category?: string;
    extractedText?: string;
    summary?: string;
    thumbnailKey?: string;
    metadata?: DocumentMetadata;
    errorMessage?: string;
    retryCount!: number;
    createdAt!: string;
    updatedAt!: string;
    processingStartedAt?: string;
    processingCompletedAt?: string;

    // DynamoDB keys
    PK!: string;
    SK!: string;
    GSI1PK!: string;
    GSI1SK!: string;

    static fromRecord(record: DocumentRecord): DocumentEntity {
        const entity = new DocumentEntity();
        Object.assign(entity, record);
        entity.PK = `USER#${record.userId}`;
        entity.SK = `DOC#${record.id}`;
        entity.GSI1PK = `DOC#${record.id}`;
        entity.GSI1SK = `STATUS#${record.status}#${record.createdAt}`;
        return entity;
    }

    static toRecord(item: Record<string, any>): DocumentRecord {
        return {
            id: item.id,
            userId: item.userId,
            fileName: item.fileName,
            originalName: item.originalName,
            mimeType: item.mimeType,
            fileSize: item.fileSize,
            s3Key: item.s3Key,
            status: item.status,
            category: item.category,
            extractedText: item.extractedText,
            summary: item.summary,
            thumbnailKey: item.thumbnailKey,
            metadata: item.metadata ? item.metadata : undefined,
            errorMessage: item.errorMessage,
            retryCount: item.retryCount ?? 0,
            createdAt: item.createdAt,
            updatedAt: item.updatedAt,
            processingStartedAt: item.processingStartedAt,
            processingCompletedAt: item.processingCompletedAt
        };
    }
}
