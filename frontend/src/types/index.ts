export type DocumentStatus = 'PENDING' | 'PROCESSING' | 'INDEXING' | 'COMPLETED' | 'FAILED';

export type DocumentStatusExpanded =
    | 'PENDING'
    | 'PROCESSING'
    | 'COMPLETED'
    | 'FAILED'
    | 'EXTRACTING'
    | 'CLASSIFYING'
    | 'SUMMARIZING'
    | 'INDEXING';

export interface DocumentMetadata {
    pageCount?: number;
    dates?: string[];
    amounts?: { value: number; currency: string }[];
    names?: string[];
    locations?: string[];
    tags?: string[];
    summary?: string;
    keywords?: string[];
    language?: string;
    confidence: number;
}

export interface Document {
    id: string;
    fileName: string;
    originalName: string;
    mimeType: string;
    fileSize: number;
    status: DocumentStatus;
    category?: string;
    summary?: string;
    metadata?: DocumentMetadata;
    /** Detected language of the content, e.g. en or pl. */
    language?: string;
    extracted?: ExtractedFields;
    index?: DocumentIndex;
    /** Text addressed to an AI system, removed at ingest before any model read it. */
    quarantined?: string[];
    errorMessage?: string;
    createdAt: string;
    updatedAt: string;
    processingCompletedAt?: string;
    downloadUrl?: string;
    thumbnailUrl?: string;
}

// Extracted fields: mirror ExtractedFields in packages/shared (the JSON Schema in
// packages/shared/contracts/extracted-fields.schema.json is the reference).

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

export interface PaginatedResponse<T> {
    items: T[];
    total: number;
    lastKey?: string;
}

export interface AuthResponse {
    accessToken: string;
    refreshToken?: string;
    userId: string;
    email: string;
}

export interface User {
    userId: string;
    email: string;
    accessToken: string;
}

// Chat: mirrors the conversation types and ChatStreamEvent in packages/shared.

export interface ChatScope {
    documentIds?: string[];
    categories?: ('INVOICE' | 'RECEIPT' | 'CONTRACT' | 'OTHER')[];
    languages?: string[];
    dateFrom?: string;
    dateTo?: string;
}

export interface Conversation {
    id: string;
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

export type ChatIntent = 'smalltalk' | 'retrieve' | 'aggregate' | 'mixed';

export interface ChatUsage {
    inputTokens: number;
    outputTokens: number;
    costUsd: number | null;
    llmCalls: number;
}

export interface ConversationMessage {
    id: string;
    role: 'user' | 'assistant';
    content: string;
    citations?: Citation[];
    intent?: ChatIntent;
    usage?: ChatUsage;
    traceId?: string | null;
    createdAt: string;
}

export type ChatStreamEvent =
    | { type: 'step'; node: string; label: string }
    | { type: 'token'; text: string }
    | { type: 'reset' }
    | { type: 'citations'; citations: Citation[] }
    | { type: 'done'; intent: ChatIntent; usage: ChatUsage; traceId: string | null }
    | { type: 'error'; message: string };
