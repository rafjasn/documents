'use client';

import { FileText, X, CheckCircle2, AlertCircle, Loader2, Upload } from 'lucide-react';
import Link from 'next/link';
import { formatFileSize } from '@/lib/utils';

export interface UploadItem {
    id: string;
    file: File;
    displayName: string;
    status: 'queued' | 'uploading' | 'done' | 'error';
    progress: number;
    documentId?: string;
    error?: string;
}

interface DocumentListProps {
    items: UploadItem[];
    uploading: boolean;
    onRemoveItem: (id: string) => void;
    onUpdateItemName: (id: string, name: string) => void;
    onUploadAll: () => void;
}

export default function DocumentList({
    items,
    uploading,
    onRemoveItem,
    onUpdateItemName,
    onUploadAll
}: DocumentListProps) {
    if (items.length === 0) return null;

    const completedCount = items.filter((i) => i.status === 'done').length;
    const allDone = items.every((i) => i.status === 'done' || i.status === 'error');

    return (
        <div className="mt-4">
            <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03] mb-4">
                {items.map((item, index) => (
                    <div
                        key={item.id}
                        className={`flex items-center gap-4 px-4 py-3 ${
                            index < items.length - 1
                                ? 'border-b border-gray-100 dark:border-gray-800'
                                : ''
                        }`}
                    >
                        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gray-100 dark:bg-gray-800">
                            <FileText size={18} className="text-gray-500 dark:text-gray-400" />
                        </div>

                        <div className="min-w-0 flex-1">
                            {item.status === 'queued' ? (
                                <input
                                    type="text"
                                    value={item.displayName}
                                    onChange={(e) => onUpdateItemName(item.id, e.target.value)}
                                    className="w-full rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-800 focus:border-brand-500 focus:outline-none dark:border-gray-700 dark:bg-gray-900 dark:text-white/90"
                                />
                            ) : (
                                <p className="truncate text-sm font-medium text-gray-800 dark:text-white/90">
                                    {item.displayName}
                                </p>
                            )}
                            <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                                {formatFileSize(item.file.size)} ·{' '}
                                {item.file.type.split('/').pop()?.toUpperCase()}
                            </p>

                            {item.status === 'uploading' && (
                                <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
                                    <div
                                        className="h-full rounded-full bg-brand-500 transition-all duration-500"
                                        style={{ width: `${item.progress}%` }}
                                    />
                                </div>
                            )}

                            {item.error && (
                                <p className="mt-1 text-xs text-error-500">{item.error}</p>
                            )}
                        </div>

                        <div className="shrink-0">
                            {item.status === 'queued' && (
                                <button
                                    onClick={() => onRemoveItem(item.id)}
                                    className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300"
                                >
                                    <X size={16} />
                                </button>
                            )}
                            {item.status === 'uploading' && (
                                <Loader2 size={18} className="animate-spin text-brand-500" />
                            )}
                            {item.status === 'done' && (
                                <CheckCircle2 size={18} className="text-success-500" />
                            )}
                            {item.status === 'error' && (
                                <AlertCircle size={18} className="text-error-500" />
                            )}
                        </div>
                    </div>
                ))}
            </div>

            <div className="flex items-center justify-between">
                <p className="text-sm text-gray-500 dark:text-gray-400">
                    {completedCount}/{items.length} uploaded
                </p>
                <div className="flex gap-3">
                    {allDone ? (
                        <Link
                            href="/dashboard"
                            className="inline-flex items-center gap-2 rounded-lg bg-brand-500 px-4 py-2.5 text-sm font-medium text-white hover:bg-brand-600"
                        >
                            View documents
                            <CheckCircle2 size={16} />
                        </Link>
                    ) : (
                        <button
                            onClick={onUploadAll}
                            disabled={uploading || items.every((i) => i.status !== 'queued')}
                            className="inline-flex items-center gap-2 rounded-lg bg-brand-500 px-4 py-2.5 text-sm font-medium text-white hover:bg-brand-600 disabled:opacity-50"
                        >
                            {uploading ? (
                                <>
                                    <Loader2 size={16} className="animate-spin" />
                                    Uploading...
                                </>
                            ) : (
                                <>
                                    <Upload size={16} />
                                    Upload all
                                </>
                            )}
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
}
