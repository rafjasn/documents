'use client';

import { useMemo, useState } from 'react';
import { Calendar, FileText, Filter, Globe, Library, Search } from 'lucide-react';
import { ChatScope, Document } from '@/types';
import { CATEGORY_LABELS, CATEGORY_NAMES, languageName } from '@/lib/utils';

type Category = NonNullable<ChatScope['categories']>[number];
const CATEGORIES: Category[] = ['INVOICE', 'RECEIPT', 'CONTRACT', 'OTHER'];
const LANGUAGES = ['en', 'pl'];

export function isWholeLibrary(scope: ChatScope): boolean {
    return !(
        scope.documentIds?.length ||
        scope.categories?.length ||
        scope.languages?.length ||
        scope.dateFrom ||
        scope.dateTo
    );
}

/** The scope in a few words each: "Contracts", "Polish", "from 2026-01-01". */
export function describeScope(scope: ChatScope, titles: Map<string, string> = new Map()): string[] {
    if (isWholeLibrary(scope)) return ['Whole library'];
    const parts: string[] = [];
    const ids = scope.documentIds ?? [];
    if (ids.length === 1) parts.push(titles.get(ids[0]) ?? '1 document');
    else if (ids.length > 1) parts.push(`${ids.length} documents`);
    for (const category of scope.categories ?? [])
        parts.push(CATEGORY_LABELS[category] ?? category);
    for (const language of scope.languages ?? []) parts.push(languageName(language));
    if (scope.dateFrom && scope.dateTo) parts.push(`${scope.dateFrom} – ${scope.dateTo}`);
    else if (scope.dateFrom) parts.push(`from ${scope.dateFrom}`);
    else if (scope.dateTo) parts.push(`until ${scope.dateTo}`);
    return parts;
}

export function ScopeChips({ scope, titles }: { scope: ChatScope; titles?: Map<string, string> }) {
    const whole = isWholeLibrary(scope);
    return (
        <div className="flex flex-wrap items-center gap-1.5" data-testid="scope-chips">
            {whole ? (
                <Library size={14} className="text-gray-400" />
            ) : (
                <Filter size={14} className="text-gray-400" />
            )}
            {describeScope(scope, titles).map((part) => (
                <span
                    key={part}
                    className="max-w-[16rem] truncate rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-medium text-gray-700 dark:bg-gray-800 dark:text-gray-300"
                >
                    {part}
                </span>
            ))}
        </div>
    );
}

function toggle<T>(values: T[] | undefined, value: T): T[] {
    const current = values ?? [];
    return current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
}

function Toggle({
    on,
    onClick,
    children
}: {
    on: boolean;
    onClick: () => void;
    children: React.ReactNode;
}) {
    return (
        <button
            type="button"
            onClick={onClick}
            aria-pressed={on}
            className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                on
                    ? 'border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300'
                    : 'border-gray-200 text-gray-600 hover:border-gray-300 dark:border-gray-700 dark:text-gray-300'
            }`}
        >
            {children}
        </button>
    );
}

interface ScopeEditorProps {
    scope: ChatScope;
    onChange: (scope: ChatScope) => void;
    /** The user's processed documents, to choose from; null while they load. */
    documents: Document[] | null;
}

/** What a new conversation may search: the whole library, or filtered by type,
 * language, document date or chosen documents. */
export function ScopeEditor({ scope, onChange, documents }: ScopeEditorProps) {
    const [query, setQuery] = useState('');
    const set = (change: Partial<ChatScope>) => onChange({ ...scope, ...change });
    const chosen = new Set(scope.documentIds ?? []);
    const matching = useMemo(() => {
        const q = query.trim().toLowerCase();
        return (documents ?? [])
            .filter((d) => !q || d.fileName.toLowerCase().includes(q))
            .sort((a, b) => a.fileName.localeCompare(b.fileName));
    }, [documents, query]);

    const label =
        'mb-1.5 flex items-center gap-1.5 text-xs font-medium text-gray-500 dark:text-gray-400';
    const dateInput =
        'rounded-lg border border-gray-300 bg-white px-2.5 py-1.5 text-xs text-gray-700 outline-none focus:border-brand-500 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300 dark:[color-scheme:dark]';

    return (
        <div
            className="space-y-4 rounded-2xl border border-gray-200 p-4 dark:border-gray-800"
            data-testid="scope-editor"
        >
            <div className="flex items-center justify-between gap-2">
                <p className="text-sm font-semibold text-gray-800 dark:text-white/90">
                    What should this chat search?
                </p>
                {!isWholeLibrary(scope) && (
                    <button
                        type="button"
                        onClick={() => onChange({})}
                        className="text-xs text-brand-600 hover:underline dark:text-brand-400"
                    >
                        Whole library
                    </button>
                )}
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
                <div>
                    <p className={label}>
                        <FileText size={13} /> Type
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                        {CATEGORIES.map((category) => (
                            <Toggle
                                key={category}
                                on={scope.categories?.includes(category) ?? false}
                                onClick={() =>
                                    set({ categories: toggle(scope.categories, category) })
                                }
                            >
                                {CATEGORY_LABELS[category]}
                            </Toggle>
                        ))}
                    </div>
                </div>
                <div>
                    <p className={label}>
                        <Globe size={13} /> Document language
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                        {LANGUAGES.map((language) => (
                            <Toggle
                                key={language}
                                on={scope.languages?.includes(language) ?? false}
                                onClick={() =>
                                    set({ languages: toggle(scope.languages, language) })
                                }
                            >
                                {languageName(language)}
                            </Toggle>
                        ))}
                    </div>
                </div>
            </div>
            <div>
                <p className={label}>
                    <Calendar size={13} /> Document date (issue date, receipt date or contract
                    start)
                </p>
                <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500">
                    <input
                        type="date"
                        aria-label="From"
                        value={scope.dateFrom ?? ''}
                        onChange={(e) => set({ dateFrom: e.target.value || undefined })}
                        className={dateInput}
                    />
                    <span>to</span>
                    <input
                        type="date"
                        aria-label="To"
                        value={scope.dateTo ?? ''}
                        onChange={(e) => set({ dateTo: e.target.value || undefined })}
                        className={dateInput}
                    />
                </div>
            </div>
            <div>
                <p className={label}>
                    <Library size={13} /> Documents {chosen.size > 0 && `(${chosen.size} chosen)`}
                </p>
                <div className="relative mb-2">
                    <Search
                        size={14}
                        className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400"
                    />
                    <input
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        placeholder="Find a document…"
                        data-testid="scope-document-search"
                        className="w-full rounded-lg border border-gray-300 bg-white py-1.5 pl-8 pr-3 text-xs text-gray-700 outline-none focus:border-brand-500 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300"
                    />
                </div>
                <ul className="max-h-44 space-y-0.5 overflow-y-auto rounded-lg border border-gray-100 p-1 dark:border-gray-800">
                    {matching.length === 0 && (
                        <li className="px-2 py-2 text-xs text-gray-400">
                            {documents === null
                                ? 'Loading documents…'
                                : 'No processed documents match.'}
                        </li>
                    )}
                    {matching.map((d) => (
                        <li key={d.id}>
                            <label className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-xs text-gray-700 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-white/[0.04]">
                                <input
                                    type="checkbox"
                                    checked={chosen.has(d.id)}
                                    onChange={() =>
                                        set({ documentIds: toggle(scope.documentIds, d.id) })
                                    }
                                    className="h-3.5 w-3.5 rounded border-gray-300 text-brand-500"
                                />
                                <span className="min-w-0 flex-1 truncate">{d.fileName}</span>
                                {d.category && (
                                    <span className="shrink-0 text-gray-400">
                                        {CATEGORY_NAMES[d.category] ?? d.category}
                                    </span>
                                )}
                                {d.language && (
                                    <span className="shrink-0 uppercase text-gray-400">
                                        {d.language}
                                    </span>
                                )}
                            </label>
                        </li>
                    ))}
                </ul>
            </div>
        </div>
    );
}
