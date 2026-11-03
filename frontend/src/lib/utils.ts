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
        SUMMARIZING: 'Summarizing',
        INDEXING: 'Indexing'
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

const LANGUAGES: Record<string, string> = { en: 'English', pl: 'Polish' };

export function languageName(code?: string): string {
    if (!code) return 'Unknown';
    return LANGUAGES[code] ?? code.toUpperCase();
}

/** An amount with its ISO currency code, e.g. "PLN 5,166.00" or "£1,665.40". */
export function formatMoney(amount?: number, currency?: string): string {
    if (amount === undefined || amount === null) return '–';
    if (!currency) {
        return amount.toLocaleString('en-GB', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }
    try {
        return new Intl.NumberFormat('en-GB', { style: 'currency', currency }).format(amount);
    } catch {
        return `${amount.toFixed(2)} ${currency}`;
    }
}

/** An ISO date (YYYY-MM-DD) as "12 Mar 2026". */
export function formatDay(iso?: string): string {
    if (!iso) return '–';
    const day = new Date(`${iso.slice(0, 10)}T00:00:00`);
    if (Number.isNaN(day.getTime())) return iso;
    return day.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
}

export function formatCost(usd?: number | null): string {
    if (usd === undefined || usd === null) return '';
    return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

export const CATEGORY_LABELS: Record<string, string> = {
    INVOICE: 'Invoices',
    RECEIPT: 'Receipts',
    CONTRACT: 'Contracts',
    OTHER: 'Other'
};

export const CATEGORY_NAMES: Record<string, string> = {
    INVOICE: 'Invoice',
    RECEIPT: 'Receipt',
    CONTRACT: 'Contract',
    OTHER: 'Other'
};
