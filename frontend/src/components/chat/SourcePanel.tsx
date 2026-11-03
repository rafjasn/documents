'use client';

import Link from 'next/link';
import { ExternalLink, FileText, ListTree, X } from 'lucide-react';
import PagePreview from './PagePreview';
import { Citation } from '@/types';

function where(citation: Citation): string {
    if (citation.chunkId === null) return 'Extracted fields';
    if (citation.pages.length > 1) {
        return `Pages ${citation.pages[0]}–${citation.pages[citation.pages.length - 1]}`;
    }
    return citation.page ? `Page ${citation.page}` : 'Passage';
}

interface SourcePanelProps {
    /** The citations of one answer. */
    citations: Citation[];
    activeId: number | null;
    onSelect: (citationId: number) => void;
    onClose?: () => void;
    /** Link each source to its document page (off when the chat is on that page). */
    linkDocuments?: boolean;
}

/** The sources of an answer: the selected one with its page and highlighted passage,
 * then the list of everything the answer cited. */
export default function SourcePanel({
    citations,
    activeId,
    onSelect,
    onClose,
    linkDocuments = true
}: SourcePanelProps) {
    const active = citations.find((c) => c.citationId === activeId) ?? null;

    return (
        <div className="flex h-full min-h-0 flex-col" data-testid="source-panel">
            <div className="flex items-center justify-between pb-3">
                <h2 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                    <ListTree size={16} />
                    Sources
                </h2>
                {onClose && (
                    <button
                        type="button"
                        onClick={onClose}
                        aria-label="Close sources"
                        className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800 dark:hover:text-gray-200"
                    >
                        <X size={16} />
                    </button>
                )}
            </div>
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-1">
                {active ? (
                    <div className="space-y-3" data-testid="active-source">
                        <div className="flex items-start justify-between gap-2">
                            <div className="min-w-0">
                                <p className="truncate text-sm font-semibold text-gray-800 dark:text-white/90">
                                    [{active.citationId}] {active.title}
                                </p>
                                <p className="text-xs text-gray-500 dark:text-gray-400">
                                    {where(active)}
                                </p>
                            </div>
                            {linkDocuments && (
                                <Link
                                    href={`/documents/${active.docId}`}
                                    className="inline-flex shrink-0 items-center gap-1 text-xs text-brand-600 hover:underline dark:text-brand-400"
                                >
                                    Open <ExternalLink size={12} />
                                </Link>
                            )}
                        </div>
                        {active.page !== null && (
                            <PagePreview
                                key={`${active.docId}-${active.chunkId}-${active.page}`}
                                docId={active.docId}
                                page={active.page}
                                bbox={active.bbox}
                            />
                        )}
                        <blockquote
                            data-testid="source-snippet"
                            className="rounded-lg border-l-4 border-amber-400 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-gray-700 dark:bg-amber-500/10 dark:text-gray-300"
                        >
                            {active.chunkId === null && (
                                <span className="mb-1 block font-semibold text-gray-500 dark:text-gray-400">
                                    Fields extracted from the whole document
                                </span>
                            )}
                            {active.snippet}
                        </blockquote>
                    </div>
                ) : (
                    <p className="rounded-lg bg-gray-50 px-3 py-4 text-center text-sm text-gray-500 dark:bg-white/[0.03] dark:text-gray-400">
                        {citations.length
                            ? 'Select a source to see the cited page.'
                            : 'Citations from answers appear here.'}
                    </p>
                )}
                {citations.length > 0 && (
                    <ul className="space-y-1">
                        {citations.map((c) => (
                            <li key={c.citationId}>
                                <button
                                    type="button"
                                    onClick={() => onSelect(c.citationId)}
                                    className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition-colors ${
                                        c.citationId === activeId
                                            ? 'bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300'
                                            : 'text-gray-600 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-white/[0.04]'
                                    }`}
                                >
                                    <span className="font-semibold">[{c.citationId}]</span>
                                    <FileText size={13} className="shrink-0 text-gray-400" />
                                    <span className="min-w-0 flex-1 truncate">{c.title}</span>
                                    <span className="shrink-0 text-gray-400">{where(c)}</span>
                                </button>
                            </li>
                        ))}
                    </ul>
                )}
            </div>
        </div>
    );
}
