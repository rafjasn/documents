import { DocumentStatus, DocumentStatusExpanded } from '@/types';

export function formatFileSize(bytes: number): string {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));

    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

export function formatDate(iso: string): string {
    return new Date(iso).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    });
}

export function relativeTime(iso: string): string {
    const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);

    if (seconds < 60) {
        return 'just now';
    }

    const minutes = Math.floor(seconds / 60);

    if (minutes < 60) {
        return `${minutes}m ago`;
    }

    const hours = Math.floor(minutes / 60);

    if (hours < 24) {
        return `${hours}h ago`;
    }

    const days = Math.floor(hours / 24);

    return `${days}d ago`;
}

export function statusLabel(status: DocumentStatusExpanded): string {
    const labels: Record<DocumentStatusExpanded, string> = {
        PENDING: 'Pending',
        PROCESSING: 'Processing',
        COMPLETED: 'Completed',
        FAILED: 'Failed',
        EXTRACTING: 'Extracting',
        CLASSIFYING: 'Classifying',
        SUMMARIZING: 'Summarizing'
    };

    return labels[status] || status;
}

export function mimeTypeIcon(mimeType: string): string {
    if (mimeType === 'application/pdf') return '📕';
    if (mimeType.startsWith('image/')) return '🖼️';
    if (mimeType === 'text/csv') return '📊';
    if (mimeType.includes('wordprocessing')) return '📘';
    return '📄';
}

export function confidenceColor(confidence: number): string {
    if (confidence >= 0.7) return 'text-emerald-600';
    if (confidence >= 0.4) return 'text-amber-600';
    return 'text-red-500';
}
