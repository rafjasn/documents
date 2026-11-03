'use client';

import { Fragment, ReactNode } from 'react';
import { Citation } from '@/types';

// [2], [1][3] and [1, 3]: the markers ai-service turns into citations.
const INLINE = /\*\*([^*]+)\*\*|\[(\d+(?:\s*[,;]\s*\d+)*)\]/g;
const BULLET = /^\s*(?:[-*•]|\d+[.)])\s+/;

interface AnswerTextProps {
    text: string;
    citations: Citation[];
    activeId?: number | null;
    onCite?: (citationId: number) => void;
}

/** An answer with its citation markers as buttons, and light markdown (paragraphs,
 * bullet lists, bold), which is all the answer prompt produces. */
export default function AnswerText({ text, citations, activeId, onCite }: AnswerTextProps) {
    const byId = new Map(citations.map((c) => [c.citationId, c]));

    const marker = (n: number, key: string) => {
        const citation = byId.get(n);
        const base =
            'mx-0.5 inline-flex h-[1.15rem] min-w-[1.15rem] items-center justify-center rounded px-1 align-[0.1em] text-[0.7rem] font-semibold leading-none';
        if (!citation || !onCite) {
            return (
                <span
                    key={key}
                    className={`${base} bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-300`}
                >
                    {n}
                </span>
            );
        }
        const active = activeId === n;
        return (
            <button
                key={key}
                type="button"
                data-testid="citation-marker"
                onClick={() => onCite(n)}
                title={`${citation.title}${citation.page ? `, page ${citation.page}` : ''}`}
                className={`${base} transition-colors ${
                    active
                        ? 'bg-brand-500 text-white'
                        : 'bg-brand-100 text-brand-700 hover:bg-brand-200 dark:bg-brand-500/25 dark:text-brand-200 dark:hover:bg-brand-500/40'
                }`}
            >
                {n}
            </button>
        );
    };

    const inline = (line: string, prefix: string): ReactNode[] => {
        const parts: ReactNode[] = [];
        let last = 0;
        for (const match of line.matchAll(INLINE)) {
            const at = match.index ?? 0;
            if (at > last) parts.push(line.slice(last, at));
            if (match[1] !== undefined) {
                parts.push(
                    <strong key={`${prefix}-${at}`} className="font-semibold">
                        {match[1]}
                    </strong>
                );
            } else {
                match[2]
                    .split(/\s*[,;]\s*/)
                    .forEach((n, i) => parts.push(marker(Number(n), `${prefix}-${at}-${i}`)));
            }
            last = at + match[0].length;
        }
        if (last < line.length) parts.push(line.slice(last));
        return parts;
    };

    // Blocks: paragraphs separated by blank lines, and runs of bullet lines.
    const blocks: { list: boolean; lines: string[] }[] = [];
    for (const line of text.split('\n')) {
        if (!line.trim()) {
            blocks.push({ list: false, lines: [] });
            continue;
        }
        const list = BULLET.test(line);
        const current = blocks[blocks.length - 1];
        if (current && current.list === list && (list || current.lines.length > 0)) {
            current.lines.push(list ? line.replace(BULLET, '') : line);
        } else {
            blocks.push({ list, lines: [list ? line.replace(BULLET, '') : line] });
        }
    }

    return (
        <div className="space-y-2">
            {blocks
                .filter((block) => block.lines.length > 0)
                .map((block, b) =>
                    block.list ? (
                        <ul key={b} className="list-disc space-y-1 pl-5">
                            {block.lines.map((line, i) => (
                                <li key={i}>{inline(line, `${b}-${i}`)}</li>
                            ))}
                        </ul>
                    ) : (
                        <p key={b}>
                            {block.lines.map((line, i) => (
                                <Fragment key={i}>
                                    {i > 0 && <br />}
                                    {inline(line, `${b}-${i}`)}
                                </Fragment>
                            ))}
                        </p>
                    )
                )}
        </div>
    );
}
