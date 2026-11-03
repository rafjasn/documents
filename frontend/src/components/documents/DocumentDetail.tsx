'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import Badge from '@/components/ui/badge/Badge';
import ChatThread, { ActiveCitation } from '@/components/chat/ChatThread';
import SourcePanel from '@/components/chat/SourcePanel';
import ExtractedFields from './ExtractedFields';
import { useChat } from '@/hooks/useChat';
import { Conversation, Document } from '@/types';
import {
    CATEGORY_NAMES,
    confidenceColor,
    formatDate,
    formatFileSize,
    languageName,
    mimeTypeIcon,
    statusLabel
} from '@/lib/utils';
import {
    AlertCircle,
    ArrowLeft,
    BarChart3,
    Download,
    ExternalLink,
    FileText,
    Globe,
    Loader2,
    MessageSquare,
    MessageSquarePlus,
    ShieldAlert,
    Tag,
    Trash2
} from 'lucide-react';
import { api } from '@/lib/api';

interface DocumentDetailProps {
    doc: Document;
    deleting: boolean;
    onDownload: () => void;
    onDelete: () => void;
}

const card =
    'overflow-hidden rounded-2xl border border-gray-200 bg-white p-6 dark:border-gray-800 dark:bg-white/[0.03]';
const label =
    'flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400';
const heading = `mb-4 ${label}`;

function Chips({ label, values }: { label: string; values?: string[] }) {
    if (!values?.length) return null;
    return (
        <div>
            <p className="mb-1.5 text-xs text-gray-500 dark:text-gray-400">{label}</p>
            <div className="flex flex-wrap gap-1.5">
                {values.map((value, i) => (
                    <span
                        key={i}
                        className="rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-medium text-gray-700 dark:bg-gray-800 dark:text-gray-300"
                    >
                        {value}
                    </span>
                ))}
            </div>
        </div>
    );
}

/** Chat about this document: a conversation scoped to it, with the cited pages beside. */
function DocumentChat({ doc }: { doc: Document }) {
    const [conversationId, setConversationId] = useState<string | null>(null);
    const [found, setFound] = useState(false); // looked for an earlier conversation
    const [citation, setCitation] = useState<ActiveCitation | null>(null);
    const [drawer, setDrawer] = useState(false);

    useEffect(() => {
        let current = true;
        api.listConversations(doc.id)
            .then(([latest]) => current && latest && setConversationId(latest.id))
            .catch(() => {
                // Without its history, the chat starts a new conversation.
            })
            .finally(() => current && setFound(true));
        return () => {
            current = false;
        };
    }, [doc.id]);

    const create = useCallback(() => api.createConversation({ documentIds: [doc.id] }), [doc.id]);
    const onCreated = useCallback((c: Conversation) => setConversationId(c.id), []);
    const chat = useChat(conversationId, create, onCreated);

    const message =
        chat.messages.find((m) => m.key === citation?.messageKey) ??
        [...chat.messages].reverse().find((m) => m.role === 'assistant' && m.citations.length > 0);
    const panel = (onClose?: () => void) => (
        <SourcePanel
            citations={message?.citations ?? []}
            activeId={citation && message?.key === citation.messageKey ? citation.citationId : null}
            onSelect={(citationId) =>
                message && setCitation({ messageKey: message.key, citationId })
            }
            onClose={onClose}
            linkDocuments={false}
        />
    );

    return (
        <div
            className={`${card} mt-6`}
            data-testid="document-chat"
            data-ready={found && !chat.loading}
        >
            <div className="mb-2 flex items-center justify-between">
                <h2 className={label}>
                    <MessageSquare size={16} />
                    Ask about this document
                </h2>
                <div className="flex items-center gap-3 text-xs">
                    {conversationId && (
                        <Link
                            href={`/chat?c=${conversationId}`}
                            className="inline-flex items-center gap-1 text-brand-600 hover:underline dark:text-brand-400"
                        >
                            Open in Chat <ExternalLink size={12} />
                        </Link>
                    )}
                    {chat.messages.length > 0 && (
                        <button
                            type="button"
                            onClick={() => {
                                setConversationId(null);
                                setCitation(null);
                            }}
                            className="inline-flex items-center gap-1 text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-white"
                        >
                            <MessageSquarePlus size={13} /> New conversation
                        </button>
                    )}
                </div>
            </div>
            <div className="grid gap-6 lg:grid-cols-5">
                <div className="flex h-[36rem] flex-col lg:col-span-3">
                    <ChatThread
                        messages={chat.messages}
                        loading={chat.loading}
                        sending={chat.sending}
                        onSend={chat.send}
                        active={citation}
                        onCite={(next) => {
                            setCitation(next);
                            setDrawer(true);
                        }}
                        placeholder="Ask something about this document…"
                        empty={
                            <p className="py-10 text-center text-sm text-gray-400 dark:text-gray-500">
                                Ask about amounts, dates, parties or clauses. Answers cite the page
                                they come from.
                            </p>
                        }
                    />
                </div>
                <div className="hidden h-[36rem] lg:col-span-2 lg:block">{panel()}</div>
            </div>
            {drawer && citation && (
                <div className="fixed inset-0 z-[100000] lg:hidden">
                    <div
                        className="absolute inset-0 bg-gray-900/40"
                        onClick={() => setDrawer(false)}
                    />
                    <div className="absolute inset-y-0 right-0 w-full max-w-md bg-white p-4 shadow-xl dark:bg-gray-900">
                        {panel(() => setDrawer(false))}
                    </div>
                </div>
            )}
        </div>
    );
}

export default function DocumentDetail({
    doc,
    deleting,
    onDownload,
    onDelete
}: DocumentDetailProps) {
    const meta = doc.metadata;
    const isProcessing = doc.status !== 'COMPLETED' && doc.status !== 'FAILED';

    const statusColor =
        doc.status === 'COMPLETED'
            ? 'success'
            : doc.status === 'PENDING'
              ? 'warning'
              : doc.status === 'FAILED'
                ? 'error'
                : 'info';

    return (
        <div className="mx-auto max-w-6xl">
            <Link
                href="/dashboard"
                className="mb-6 inline-flex items-center gap-1.5 text-sm text-gray-500 transition-colors hover:text-gray-800 dark:text-gray-400 dark:hover:text-white"
            >
                <ArrowLeft size={14} />
                Back to documents
            </Link>

            <div className={`${card} mb-6`}>
                <div className="flex items-start justify-between gap-4">
                    <div className="flex min-w-0 items-start gap-4">
                        <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-xl bg-gray-100 dark:bg-gray-800">
                            {doc.thumbnailUrl ? (
                                // eslint-disable-next-line @next/next/no-img-element
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
                            <h1 className="truncate text-xl font-bold text-gray-800 dark:text-white/90">
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
                                        {CATEGORY_NAMES[doc.category] ?? doc.category}
                                    </span>
                                )}
                                {doc.language && (
                                    <span
                                        data-testid="document-language"
                                        title="Detected language of the content"
                                        className="inline-flex items-center gap-1 rounded-full bg-blue-light-50 px-2.5 py-0.5 text-xs font-medium text-blue-light-700 dark:bg-blue-light-500/15 dark:text-blue-light-400"
                                    >
                                        <Globe size={12} />
                                        {languageName(doc.language)}
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
                            aria-label="Delete document"
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
                <div className="mb-6 flex items-center gap-4 overflow-hidden rounded-2xl border border-indigo-200 bg-indigo-50 p-6 dark:border-indigo-800 dark:bg-indigo-900/20">
                    <div className="rounded-full bg-indigo-100 p-3 dark:bg-indigo-900/40">
                        <Loader2
                            size={24}
                            className="animate-spin text-indigo-600 dark:text-indigo-400"
                        />
                    </div>
                    <div>
                        <h3 className="font-semibold text-indigo-900 dark:text-indigo-300">
                            {doc.status === 'PENDING'
                                ? 'Queued for processing'
                                : `${statusLabel(doc.status)}…`}
                        </h3>
                        <p className="mt-0.5 text-sm text-indigo-700/70 dark:text-indigo-400/70">
                            {doc.status === 'PENDING'
                                ? 'Your document is in the queue and will be processed shortly.'
                                : 'Reading the pages, extracting fields and indexing the text for search.'}
                        </p>
                    </div>
                </div>
            )}

            {doc.quarantined && doc.quarantined.length > 0 && (
                <div
                    data-testid="quarantine-warning"
                    className="mb-6 flex items-start gap-4 overflow-hidden rounded-2xl border border-amber-200 bg-amber-50 p-6 dark:border-amber-800 dark:bg-amber-900/20"
                >
                    <ShieldAlert
                        size={20}
                        className="mt-0.5 shrink-0 text-amber-600 dark:text-amber-400"
                    />
                    <div className="min-w-0">
                        <h3 className="font-semibold text-amber-900 dark:text-amber-300">
                            Text addressed to an AI assistant was removed
                        </h3>
                        <p className="mt-1 text-sm text-amber-800/80 dark:text-amber-400/80">
                            This document contains instructions meant for an AI system, a common way
                            to plant false answers. They were cut before extraction, search and chat
                            read the document; check its contents with the sender.
                        </p>
                        <ul className="mt-3 space-y-1.5">
                            {doc.quarantined.map((excerpt, i) => (
                                <li
                                    key={i}
                                    className="truncate rounded-lg bg-white/70 px-3 py-1.5 font-mono text-xs text-amber-900 dark:bg-gray-900/50 dark:text-amber-200"
                                    title={excerpt}
                                >
                                    {excerpt}
                                </li>
                            ))}
                        </ul>
                    </div>
                </div>
            )}

            {doc.status === 'FAILED' && doc.errorMessage && (
                <div className="mb-6 flex items-start gap-4 overflow-hidden rounded-2xl border border-red-200 bg-red-50 p-6 dark:border-red-800 dark:bg-red-900/20">
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

            {doc.status === 'COMPLETED' && (
                <div className="grid gap-6 lg:grid-cols-3">
                    <div className={`${card} lg:col-span-2`}>
                        <h2 className={heading}>
                            <FileText size={16} />
                            Extracted fields
                        </h2>
                        {doc.extracted ? (
                            <ExtractedFields fields={doc.extracted} />
                        ) : (
                            <div className="space-y-4">
                                <p className="text-sm text-gray-500 dark:text-gray-400">
                                    Typed fields are extracted from invoices, receipts and
                                    contracts. This document was read for search; its entities are
                                    below.
                                </p>
                                <Chips label="Names" values={meta?.names} />
                                <Chips label="Locations" values={meta?.locations} />
                            </div>
                        )}
                    </div>

                    <div className="space-y-6">
                        <div className={card}>
                            <h2 className={heading}>
                                <BarChart3 size={16} />
                                Classification
                            </h2>
                            <dl className="space-y-4 text-sm">
                                <div>
                                    <dt className="text-xs text-gray-500 dark:text-gray-400">
                                        Category
                                    </dt>
                                    <dd className="mt-0.5 text-lg font-semibold text-gray-800 dark:text-white/90">
                                        {doc.category
                                            ? (CATEGORY_NAMES[doc.category] ?? doc.category)
                                            : '–'}
                                    </dd>
                                </div>
                                {meta && (
                                    <div>
                                        <dt className="text-xs text-gray-500 dark:text-gray-400">
                                            Confidence
                                        </dt>
                                        <dd className="mt-1 flex items-center gap-3">
                                            <div className="h-2 flex-1 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
                                                <div
                                                    className="h-full rounded-full bg-brand-500"
                                                    style={{
                                                        width: `${(meta.confidence || 0) * 100}%`
                                                    }}
                                                />
                                            </div>
                                            <span
                                                className={`text-sm font-semibold ${confidenceColor(meta.confidence)}`}
                                            >
                                                {Math.round((meta.confidence || 0) * 100)}%
                                            </span>
                                        </dd>
                                    </div>
                                )}
                                <div className="grid grid-cols-2 gap-3">
                                    <div>
                                        <dt className="text-xs text-gray-500 dark:text-gray-400">
                                            Language
                                        </dt>
                                        <dd className="mt-0.5 font-medium text-gray-800 dark:text-white/90">
                                            {languageName(doc.language)}
                                        </dd>
                                    </div>
                                    {meta?.pageCount !== undefined && (
                                        <div>
                                            <dt className="text-xs text-gray-500 dark:text-gray-400">
                                                Pages
                                            </dt>
                                            <dd className="mt-0.5 font-medium text-gray-800 dark:text-white/90">
                                                {meta.pageCount}
                                            </dd>
                                        </div>
                                    )}
                                </div>
                                {doc.index && (
                                    <div>
                                        <dt className="text-xs text-gray-500 dark:text-gray-400">
                                            Search index
                                        </dt>
                                        <dd className="mt-0.5 text-gray-700 dark:text-gray-300">
                                            {doc.index.chunkCount} passages in{' '}
                                            {doc.index.collection}
                                            <span className="block text-xs text-gray-500 dark:text-gray-400">
                                                {doc.index.embeddingModel}
                                            </span>
                                        </dd>
                                    </div>
                                )}
                            </dl>
                        </div>

                        {meta?.keywords && meta.keywords.length > 0 && (
                            <div className={card}>
                                <h2 className={heading}>
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
                </div>
            )}

            {doc.summary && (
                <div className={`${card} mt-6`}>
                    <h2 className={heading}>
                        <FileText size={16} />
                        Summary
                    </h2>
                    <p className="text-sm leading-relaxed text-gray-700 dark:text-gray-300">
                        {doc.summary}
                    </p>
                </div>
            )}

            {doc.status === 'COMPLETED' && <DocumentChat doc={doc} />}
        </div>
    );
}
