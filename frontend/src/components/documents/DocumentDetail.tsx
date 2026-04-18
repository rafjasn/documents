'use client';

import { useState, useEffect, useRef } from 'react';
import Link from 'next/link';
import Badge from '@/components/ui/badge/Badge';
import { Document } from '@/types';
import {
    formatFileSize,
    formatDate,
    statusLabel,
    mimeTypeIcon,
    confidenceColor
} from '@/lib/utils';
import {
    ArrowLeft,
    Download,
    Trash2,
    Loader2,
    AlertCircle,
    FileText,
    Tag,
    Globe,
    BarChart3,
    MessageSquare,
    Send
} from 'lucide-react';
import { api } from '@/lib/api';

interface ChatMsg {
    role: 'user' | 'assistant';
    content: string;
}

interface DocumentDetailProps {
    doc: Document;
    deleting: boolean;
    onDownload: () => void;
    onDelete: () => void;
}

export default function DocumentDetail({
    doc,
    deleting,
    onDownload,
    onDelete
}: DocumentDetailProps) {
    const [chatMessages, setChatMessages] = useState<ChatMsg[]>([]);
    const [chatInput, setChatInput] = useState('');
    const [chatLoading, setChatLoading] = useState(false);
    const chatBottomRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (!doc || doc.status !== 'COMPLETED') {
            return;
        }

        api.getChatHistory(doc.id)
            .then((msgs) =>
                setChatMessages(
                    msgs.map((m) => ({ role: m.role as 'user' | 'assistant', content: m.content }))
                )
            )
            .catch(() => {});
    }, [doc.id, doc.status]);

    useEffect(() => {
        chatBottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [chatMessages]);

    const handleChatSend = async () => {
        if (!chatInput.trim()) return;
        const message = chatInput.trim();
        setChatInput('');
        setChatMessages((prev) => [...prev, { role: 'user', content: message }]);
        setChatLoading(true);
        setChatMessages((prev) => [...prev, { role: 'assistant', content: '' }]);

        try {
            const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost';
            const res = await api.fetchWithAuth(`${apiUrl}/api/ai/chat/${doc.id}/stream`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message })
            });

            if (!res.ok) {
                throw new Error(`Chat failed: ${res.status}`);
            }

            const reader = res.body!.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();

                if (done) {
                    break;
                }

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() ?? '';

                for (const line of lines) {
                    if (!line.startsWith('data: ')) {
                        continue;
                    }

                    const payload = line.slice(6).trim();

                    if (payload === '[DONE]') {
                        break;
                    }

                    try {
                        const { chunk } = JSON.parse(payload) as { chunk: string };
                        setChatMessages((prev) => {
                            const next = [...prev];
                            next[next.length - 1] = {
                                role: 'assistant',
                                content: next[next.length - 1].content + chunk
                            };
                            return next;
                        });
                    } catch {
                        // skip malformed SSE lines
                    }
                }
            }
        } catch {
            setChatMessages((prev) => {
                const next = [...prev];
                next[next.length - 1] = {
                    role: 'assistant',
                    content: 'Sorry, something went wrong. Please try again.'
                };
                return next;
            });
        } finally {
            setChatLoading(false);
        }
    };

    const meta = doc.metadata;
    const isProcessing = doc.status === 'PROCESSING' || doc.status === 'PENDING';

    const statusColor =
        doc.status === 'COMPLETED'
            ? 'success'
            : doc.status === 'PENDING'
              ? 'warning'
              : doc.status === 'FAILED'
                ? 'error'
                : 'info';

    return (
        <div className="mx-auto max-w-4xl">
            <Link
                href="/dashboard"
                className="mb-6 inline-flex items-center gap-1.5 text-sm text-gray-500 transition-colors hover:text-gray-800 dark:text-gray-400 dark:hover:text-white"
            >
                <ArrowLeft size={14} />
                Back to documents
            </Link>

            <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03] mb-6 p-6">
                <div className="flex items-start justify-between gap-4">
                    <div className="flex items-start gap-4 min-w-0">
                        <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-xl bg-gray-100 dark:bg-gray-800">
                            {doc.thumbnailUrl ? (
                                <img
                                    width={56}
                                    height={56}
                                    src={doc.thumbnailUrl}
                                    className="h-14 w-14 rounded-xl object-cover"
                                    alt={doc.fileName}
                                />
                            ) : (
                                <span className="text-2xl">{mimeTypeIcon(doc.mimeType)}</span>
                            )}
                        </div>
                        <div className="min-w-0">
                            <h1 className="text-xl font-bold text-gray-800 dark:text-white/90 truncate">
                                {doc.fileName}
                            </h1>
                            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
                                {doc.originalName} · {formatFileSize(doc.fileSize)} ·{' '}
                                {doc.mimeType.split('/').pop()?.toUpperCase()}
                            </p>
                            <div className="mt-3 flex flex-wrap items-center gap-2">
                                <Badge size="sm" color={statusColor}>
                                    {statusLabel(doc.status)}
                                </Badge>
                                {doc.category && (
                                    <span className="inline-flex items-center rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-medium text-gray-700 dark:bg-gray-800 dark:text-gray-300">
                                        {doc.category}
                                    </span>
                                )}
                            </div>
                        </div>
                    </div>

                    <div className="flex shrink-0 items-center gap-2">
                        <button
                            onClick={onDownload}
                            className="inline-flex items-center gap-2 rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-white/[0.05]"
                        >
                            <Download size={16} />
                            Download
                        </button>
                        <button
                            onClick={onDelete}
                            disabled={deleting}
                            className="inline-flex items-center justify-center rounded-lg border border-gray-300 bg-white p-2 text-gray-400 shadow-sm hover:border-red-300 hover:bg-red-50 hover:text-red-600 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-800 dark:hover:bg-red-900/20 dark:hover:text-red-400"
                        >
                            {deleting ? (
                                <Loader2 size={16} className="animate-spin" />
                            ) : (
                                <Trash2 size={16} />
                            )}
                        </button>
                    </div>
                </div>

                <div className="mt-6 flex flex-wrap gap-6 border-t border-gray-100 pt-4 text-xs text-gray-500 dark:border-gray-800 dark:text-gray-400">
                    <span>Created: {formatDate(doc.createdAt)}</span>
                    <span>Updated: {formatDate(doc.updatedAt)}</span>
                    {doc.processingCompletedAt && (
                        <span>Processed: {formatDate(doc.processingCompletedAt)}</span>
                    )}
                </div>
            </div>

            {isProcessing && (
                <div className="overflow-hidden rounded-2xl border border-indigo-200 bg-indigo-50 dark:border-indigo-800 dark:bg-indigo-900/20 mb-6 flex items-center gap-4 p-6">
                    <div className="rounded-full bg-indigo-100 dark:bg-indigo-900/40 p-3">
                        <Loader2
                            size={24}
                            className="animate-spin text-indigo-600 dark:text-indigo-400"
                        />
                    </div>
                    <div>
                        <h3 className="font-semibold text-indigo-900 dark:text-indigo-300">
                            {doc.status === 'PENDING'
                                ? 'Queued for processing'
                                : 'Processing your document...'}
                        </h3>
                        <p className="mt-0.5 text-sm text-indigo-700/70 dark:text-indigo-400/70">
                            {doc.status === 'PENDING'
                                ? 'Your document is in the queue and will be processed shortly.'
                                : 'Extracting text, classifying content, and extracting metadata.'}
                        </p>
                    </div>
                </div>
            )}

            {doc.status === 'FAILED' && doc.errorMessage && (
                <div className="overflow-hidden rounded-2xl border border-red-200 bg-red-50 dark:border-red-800 dark:bg-red-900/20 mb-6 flex items-start gap-4 p-6">
                    <AlertCircle
                        size={20}
                        className="mt-0.5 shrink-0 text-red-500 dark:text-red-400"
                    />
                    <div>
                        <h3 className="font-semibold text-red-900 dark:text-red-300">
                            Processing failed
                        </h3>
                        <p className="mt-1 text-sm text-red-700/80 dark:text-red-400/80">
                            {doc.errorMessage}
                        </p>
                    </div>
                </div>
            )}

            {doc.status === 'COMPLETED' && meta && (
                <div className="grid gap-6 lg:grid-cols-2">
                    <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03] p-6">
                        <h2 className="mb-4 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                            <BarChart3 size={16} />
                            Classification
                        </h2>
                        <div className="space-y-4">
                            <div>
                                <p className="text-xs text-gray-500 dark:text-gray-400">Category</p>
                                <p className="mt-0.5 text-lg font-semibold text-gray-800 dark:text-white/90">
                                    {doc.category}
                                </p>
                            </div>
                            <div>
                                <p className="text-xs text-gray-500 dark:text-gray-400">
                                    Confidence
                                </p>
                                <div className="mt-1 flex items-center gap-3">
                                    <div className="h-2 flex-1 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
                                        <div
                                            className="h-full rounded-full bg-brand-500 transition-all"
                                            style={{ width: `${(meta.confidence || 0) * 100}%` }}
                                        />
                                    </div>
                                    <span
                                        className={`text-sm font-semibold ${confidenceColor(meta.confidence)}`}
                                    >
                                        {Math.round((meta.confidence || 0) * 100)}%
                                    </span>
                                </div>
                            </div>
                            {meta.language && (
                                <div className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                                    <Globe size={14} className="text-gray-400" />
                                    {meta.language === 'en' ? 'English' : meta.language}
                                </div>
                            )}
                        </div>
                    </div>

                    <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03] p-6">
                        <h2 className="mb-4 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                            <FileText size={16} />
                            Extracted Data
                        </h2>
                        <div className="space-y-4">
                            {(Object.entries(meta) as [string, unknown][])
                                .filter(
                                    ([key, val]) =>
                                        key !== 'keywords' &&
                                        key !== 'confidence' &&
                                        key !== 'pageCount' &&
                                        key !== 'language' &&
                                        Array.isArray(val) &&
                                        (val as unknown[]).length > 0
                                )
                                .map(([key, val]) => {
                                    const items = val as (
                                        | { value: number; currency: string }
                                        | string
                                    )[];
                                    const label = key.charAt(0).toUpperCase() + key.slice(1);
                                    return (
                                        <div key={key}>
                                            <p className="mb-1.5 text-xs text-gray-500 dark:text-gray-400 capitalize">
                                                {label}
                                            </p>
                                            <div className="flex flex-wrap gap-1.5">
                                                {items.map((item, i) => {
                                                    const isAmount =
                                                        typeof item === 'object' &&
                                                        item !== null &&
                                                        'value' in item;
                                                    const text = isAmount
                                                        ? `${item.currency === 'USD' ? '$' : item.currency === 'EUR' ? '€' : '£'}${item.value.toLocaleString()}`
                                                        : String(item);
                                                    return (
                                                        <span
                                                            key={i}
                                                            className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
                                                                isAmount
                                                                    ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/20 dark:text-emerald-400'
                                                                    : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
                                                            }`}
                                                        >
                                                            {text}
                                                        </span>
                                                    );
                                                })}
                                            </div>
                                        </div>
                                    );
                                })}
                            {!Object.entries(meta).some(
                                ([key, val]) =>
                                    key !== 'keywords' &&
                                    key !== 'confidence' &&
                                    key !== 'pageCount' &&
                                    key !== 'language' &&
                                    Array.isArray(val) &&
                                    (val as unknown[]).length > 0
                            ) && (
                                <p className="text-sm text-gray-400 dark:text-gray-500">
                                    No structured data extracted.
                                </p>
                            )}
                        </div>
                    </div>

                    {meta.keywords && meta.keywords.length > 0 && (
                        <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03] p-6">
                            <h2 className="mb-4 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                                <Tag size={16} />
                                Keywords
                            </h2>
                            <div className="flex flex-wrap gap-2">
                                {meta.keywords.map((kw, i) => (
                                    <span
                                        key={i}
                                        className="rounded-lg bg-brand-50 px-3 py-1.5 text-xs font-medium text-brand-700 dark:bg-brand-500/10 dark:text-brand-400"
                                    >
                                        {kw}
                                    </span>
                                ))}
                            </div>
                        </div>
                    )}
                </div>
            )}

            {doc.summary && (
                <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03] mt-6 p-6">
                    <h2 className="mb-4 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                        <FileText size={16} />
                        AI Summary
                    </h2>
                    <p className="text-sm leading-relaxed text-gray-700 dark:text-gray-300">
                        {doc.summary}
                    </p>
                </div>
            )}

            {doc.status === 'COMPLETED' && (
                <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03] mt-6 p-6">
                    <h2 className="mb-4 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                        <MessageSquare size={16} />
                        Chat
                    </h2>

                    <div className="mb-4 max-h-96 space-y-3 overflow-y-auto">
                        {chatMessages.length === 0 ? (
                            <p className="text-center text-sm text-gray-400 dark:text-gray-500">
                                Ask any question about this document.
                            </p>
                        ) : (
                            chatMessages.map((msg, i) => (
                                <div
                                    key={i}
                                    className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
                                >
                                    <div
                                        className={`max-w-[80%] rounded-xl px-4 py-2.5 text-sm leading-relaxed ${
                                            msg.role === 'user'
                                                ? 'bg-brand-500 text-white'
                                                : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
                                        }`}
                                    >
                                        {msg.content}
                                    </div>
                                </div>
                            ))
                        )}
                        {chatLoading && (
                            <div className="flex justify-start">
                                <div className="flex items-center gap-1.5 rounded-xl bg-gray-100 px-4 py-2.5 dark:bg-gray-800">
                                    <Loader2 size={14} className="animate-spin text-gray-400" />
                                    <span className="text-sm text-gray-500 dark:text-gray-400">
                                        Thinking...
                                    </span>
                                </div>
                            </div>
                        )}
                        <div ref={chatBottomRef} />
                    </div>

                    <div className="flex gap-2">
                        <input
                            type="text"
                            value={chatInput}
                            onChange={(e) => setChatInput(e.target.value)}
                            onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && handleChatSend()}
                            placeholder="Ask something about this document..."
                            disabled={chatLoading}
                            className="flex-1 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-800 placeholder-gray-400 outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-900 dark:text-white/90 dark:placeholder-gray-500"
                        />
                        <button
                            onClick={handleChatSend}
                            disabled={chatLoading || !chatInput.trim()}
                            className="inline-flex items-center justify-center rounded-lg bg-brand-500 px-4 py-2 text-white hover:bg-brand-600 disabled:opacity-50"
                        >
                            {chatLoading ? (
                                <Loader2 size={16} className="animate-spin" />
                            ) : (
                                <Send size={16} />
                            )}
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}
