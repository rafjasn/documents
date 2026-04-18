export type DocumentStatus = 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED';

export type DocumentStatusExpanded =
    | 'PENDING'
    | 'PROCESSING'
    | 'COMPLETED'
    | 'FAILED'
    | 'EXTRACTING'
    | 'CLASSIFYING'
    | 'SUMMARIZING';

export interface DocumentMetadata {
    pageCount?: number;
    dates?: string[];
    amounts?: { value: number; currency: string }[];
    names?: string[];
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
    errorMessage?: string;
    createdAt: string;
    updatedAt: string;
    processingCompletedAt?: string;
    downloadUrl?: string;
    thumbnailUrl?: string;
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
