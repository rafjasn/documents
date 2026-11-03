export enum DocumentStatus {
    PENDING = 'PENDING',
    PROCESSING = 'PROCESSING',
    EXTRACTING = 'EXTRACTING',
    CLASSIFYING = 'CLASSIFYING',
    SUMMARIZING = 'SUMMARIZING',
    INDEXING = 'INDEXING',
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
    summary?: string;
    thumbnailKey?: string;
    metadata?: DocumentMetadata;
    /** Detected language of the content, e.g. en or pl. */
    language?: string;
    /** Typed fields of an invoice, receipt or contract. */
    extracted?: ExtractedFields;
    /** Where the document's chunks are indexed. */
    index?: DocumentIndex;
    /** Text addressed to an AI system, cut at ingest before any model read it. */
    quarantined?: string[];
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

// Extracted fields: the JSON Schema in packages/shared/contracts/extracted-fields.schema.json
// is the reference. Optional fields are absent when the document doesn't state them.

export interface Party {
    name: string;
    taxId?: string;
}

export interface Money {
    amount: number;
    currency?: string;
}

export interface InvoiceLine {
    description: string;
    quantity?: number;
    unitPrice?: number;
    netAmount?: number;
    taxRate?: number;
}

export interface InvoiceFields {
    kind: 'invoice';
    vendor: Party;
    buyer?: Party;
    invoiceNumber?: string;
    issueDate?: string;
    dueDate?: string;
    currency?: string;
    netTotal?: number;
    taxTotal?: number;
    grossTotal?: number;
    paidDate?: string;
    reverseCharge: boolean;
    lineItems: InvoiceLine[];
}

export interface ReceiptItem {
    description: string;
    quantity?: number;
    unitPrice?: number;
    amount?: number;
}

export interface ReceiptFields {
    kind: 'receipt';
    merchant: Party;
    receiptNumber?: string;
    date?: string;
    currency?: string;
    total?: number;
    taxTotal?: number;
    paymentMethod?: 'cash' | 'card' | 'transfer' | 'other';
    items: ReceiptItem[];
}

export interface ContractFields {
    kind: 'contract';
    parties: Party[];
    contractNumber?: string;
    effectiveDate?: string;
    endDate?: string;
    autoRenewal?: boolean;
    noticePeriodDays?: number;
    paymentTermsDays?: number;
    valueCap?: Money;
    governingLaw?: string;
}

export type ExtractedFields = InvoiceFields | ReceiptFields | ContractFields;

export interface DocumentIndex {
    collection: string;
    version: number;
    chunkCount: number;
    embeddingModel: string;
    indexedAt: string;
}

export interface WsDocumentStatusEvent {
    documentId: string;
    status: DocumentStatus;
    document?: Partial<DocumentRecord>;
    timestamp: string;
}

// Chat: conversations stored by the API, and the stream ai-service sends through it.
// The JSON Schemas in packages/shared/contracts are the reference for ChatRequest and
// ChatStreamEvent.

export type ChatRole = 'user' | 'assistant';
export type ChatIntent = 'smalltalk' | 'retrieve' | 'aggregate' | 'mixed';

export interface ChatScope {
    documentIds?: string[];
    categories?: ('INVOICE' | 'RECEIPT' | 'CONTRACT' | 'OTHER')[];
    languages?: string[];
    dateFrom?: string;
    dateTo?: string;
}

export interface Conversation {
    id: string;
    userId: string;
    title: string;
    scope: ChatScope;
    createdAt: string;
    updatedAt: string;
}

export interface Citation {
    citationId: number;
    docId: string;
    title: string;
    page: number | null;
    pages: number[];
    bbox: [number, number, number, number] | null;
    chunkId: number | null;
    snippet: string;
}

export interface ChatUsage {
    inputTokens: number;
    outputTokens: number;
    costUsd: number | null;
    llmCalls: number;
}

export interface ConversationMessage {
    id: string;
    conversationId: string;
    role: ChatRole;
    content: string;
    citations?: Citation[];
    usage?: ChatUsage;
    intent?: ChatIntent;
    traceId?: string | null;
    createdAt: string;
}

export interface ChatRequest {
    conversationId: string;
    message: string;
    history: { role: ChatRole; content: string }[];
    scope: ChatScope;
}

export type ChatStreamEvent =
    | { type: 'step'; node: string; label: string }
    | { type: 'token'; text: string }
    | { type: 'reset' }
    | { type: 'citations'; citations: Citation[] }
    | { type: 'done'; intent: ChatIntent; usage: ChatUsage; traceId: string | null }
    | { type: 'error'; message: string };

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
