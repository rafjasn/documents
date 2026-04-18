export enum DocumentStatus {
    PENDING = 'PENDING',
    PROCESSING = 'PROCESSING',
    EXTRACTING = 'EXTRACTING',
    CLASSIFYING = 'CLASSIFYING',
    SUMMARIZING = 'SUMMARIZING',
    COMPLETED = 'COMPLETED',
    FAILED = 'FAILED'
}

export interface DocumentRecord {
    id: string;
    userId: string;
    fileName: string;
    originalName: string;
    mimeType: string;
    fileSize: number;
    s3Key: string;
    status: DocumentStatus;
    category?: string;
    extractedText?: string;
    summary?: string;
    thumbnailKey?: string;
    metadata?: DocumentMetadata;
    errorMessage?: string;
    retryCount: number;
    createdAt: string;
    updatedAt: string;
    processingStartedAt?: string;
    processingCompletedAt?: string;
}

export interface DocumentMetadata {
    pageCount?: number;
    dates?: string[];
    amounts?: { value: number; currency: string }[];
    names?: string[];
    locations?: string[];
    tags?: string[];
    keywords?: string[];
    language?: string;
    confidence: number;
    category?: string;
}

export interface ProcessingJobMessage {
    documentId: string;
    userId: string;
    s3Key: string;
    mimeType: string;
}

export interface ThumbnailJobMessage {
    documentId: string;
    userId: string;
    s3Key: string;
    mimeType: string;
}

export interface ChatMessage {
    id: string;
    documentId: string;
    userId: string;
    role: 'user' | 'assistant';
    content: string;
    createdAt: string;
}

export interface WsDocumentStatusEvent {
    documentId: string;
    status: DocumentStatus;
    document?: Partial<DocumentRecord>;
    timestamp: string;
}

export interface WsChatResponseEvent {
    documentId: string;
    sessionId: string;
    chunk: string;
    done: boolean;
}

export interface PaginatedResult<T> {
    items: T[];
    total: number;
    lastKey?: string;
}

export const AWS_RESOURCES = {
    S3_BUCKET: 'documents-uploads',
    DYNAMODB_TABLE: 'documents-documents',
    SQS_PROCESSING_QUEUE: 'documents-processing',
    SQS_PROCESSING_DLQ: 'documents-dlq',
    SQS_THUMBNAIL_QUEUE: 'documents-thumbnail-jobs',
    SQS_THUMBNAIL_DLQ: 'documents-thumbnail-dlq',
    SNS_NOTIFICATIONS: 'documents-notifications'
} as const;

export const ALLOWED_MIME_TYPES = [
    'application/pdf',
    'image/png',
    'image/jpeg',
    'image/webp',
    'text/plain',
    'text/csv',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
] as const;

export const MAX_FILE_SIZE_MB = 50;

export {
    type AwsClientsConfig,
    createDynamoDBClient,
    createS3Client,
    createSQSClient,
    createSNSClient
} from './aws-clients.factory';

export * from './auth/auth.module';
export * from './auth/jwt.strategy';
export * from './auth/jwt-auth.guard';
export * from './auth/auth-provider.interface';
export * from './auth/providers/auth-provider.factory';

export { CloudWatchLogger } from './logging/cloudwatch.logger';
