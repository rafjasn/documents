export enum DocumentStatus {
    PENDING = 'PENDING',
    PROCESSING = 'PROCESSING',
    EXTRACTING = 'EXTRACTING',
    CLASSIFYING = 'CLASSIFYING',
    SUMMARIZING = 'SUMMARIZING',
    COMPLETED = 'COMPLETED',
    FAILED = 'FAILED'
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

export interface WsDocumentStatusEvent {
    documentId: string;
    status: DocumentStatus;
    document?: Partial<DocumentRecord>;
    timestamp: string;
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
