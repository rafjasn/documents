'use client';

import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, Loader2 } from 'lucide-react';
import type { RenderTask } from 'pdfjs-dist';
import { DocumentFile, documentFile, openPdf, renderPage } from '@/lib/pdf';

const RENDER_WIDTH = 900; // CSS pixels; the canvas scales down to the panel
const PAD = 0.006; // around the highlighted box, as a share of the page

interface PagePreviewProps {
    docId: string;
    /** The cited page (1-based) and the passage's box on it: normalised, top-left origin. */
    page: number;
    bbox: [number, number, number, number] | null;
}

/** A page of the document with the cited passage highlighted. Render it with a `key`
 * per citation, so a new citation starts on its own page. */
export default function PagePreview({ docId, page, bbox }: PagePreviewProps) {
    const [file, setFile] = useState<DocumentFile | null>(null);
    const [failed, setFailed] = useState(false);
    const [shown, setShown] = useState(page);
    const [pageCount, setPageCount] = useState(0);
    const [rendered, setRendered] = useState(false);
    const canvas = useRef<HTMLCanvasElement>(null);
    const scroller = useRef<HTMLDivElement>(null);
    const highlight = useRef<HTMLDivElement>(null);

    useEffect(() => {
        let current = true;
        documentFile(docId)
            .then((f) => current && setFile(f))
            .catch(() => current && setFailed(true));
        return () => {
            current = false;
        };
    }, [docId]);

    const mimeType = file?.document.mimeType ?? '';
    const kind = !file
        ? null
        : mimeType === 'application/pdf'
          ? 'pdf'
          : mimeType.startsWith('image/')
            ? 'image'
            : 'other';

    const scrollToHighlight = () => {
        const box = highlight.current;
        const view = scroller.current;
        if (box && view) view.scrollTop = Math.max(0, box.offsetTop - view.clientHeight / 3);
    };

    useEffect(() => {
        if (kind !== 'pdf' || !file || !canvas.current) return;
        let cancelled = false;
        let task: RenderTask | null = null;
        setRendered(false);
        openPdf(docId, file.url)
            .then(async (pdf) => {
                if (cancelled || !canvas.current) return;
                setPageCount(pdf.numPages);
                task = await renderPage(
                    pdf,
                    Math.min(shown, pdf.numPages),
                    canvas.current,
                    RENDER_WIDTH
                );
                if (cancelled) {
                    task.cancel();
                    return;
                }
                await task.promise;
                if (!cancelled) {
                    setRendered(true);
                    requestAnimationFrame(scrollToHighlight);
                }
            })
            .catch((error: Error) => {
                if (!cancelled && error?.name !== 'RenderingCancelledException') setFailed(true);
            });
        return () => {
            cancelled = true;
            task?.cancel();
        };
    }, [kind, file, docId, shown]);

    if (failed) {
        return (
            <p className="text-sm text-gray-500 dark:text-gray-400">The page could not be shown.</p>
        );
    }
    if (kind === 'other') {
        return (
            <p className="text-sm text-gray-500 dark:text-gray-400">
                No page preview for this file type; the cited passage is below.
            </p>
        );
    }

    const box =
        bbox && shown === page
            ? {
                  left: `${Math.max(0, bbox[0] - PAD) * 100}%`,
                  top: `${Math.max(0, bbox[1] - PAD) * 100}%`,
                  width: `${(Math.min(1, bbox[2] + PAD) - Math.max(0, bbox[0] - PAD)) * 100}%`,
                  height: `${(Math.min(1, bbox[3] + PAD) - Math.max(0, bbox[1] - PAD)) * 100}%`
              }
            : null;

    return (
        <div
            className="overflow-hidden rounded-xl border border-gray-200 bg-gray-100 dark:border-gray-800 dark:bg-gray-900"
            data-testid="page-preview"
            data-page={shown}
            data-rendered={kind === 'image' ? 'true' : String(rendered)}
        >
            <div ref={scroller} className="relative max-h-[60vh] overflow-auto">
                <div className="relative">
                    {kind === 'image' && file ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                            src={file.url}
                            alt={`${file.document.fileName}`}
                            className="block h-auto w-full"
                            onLoad={() => requestAnimationFrame(scrollToHighlight)}
                        />
                    ) : (
                        <canvas
                            ref={canvas}
                            className={rendered ? 'block h-auto w-full bg-white' : 'hidden'}
                        />
                    )}
                    {box && (kind === 'image' || rendered) && (
                        <div
                            ref={highlight}
                            data-testid="page-highlight"
                            className="pointer-events-none absolute rounded-sm border-2 border-amber-500 bg-amber-300/25 shadow-[0_0_0_4px_rgba(245,158,11,0.18)]"
                            style={box}
                        />
                    )}
                </div>
                {(!file || (kind === 'pdf' && !rendered)) && (
                    <div className="flex min-h-48 items-center justify-center">
                        <Loader2 size={20} className="animate-spin text-gray-400" />
                    </div>
                )}
            </div>
            {kind === 'pdf' && pageCount > 1 && (
                <div className="flex items-center justify-between border-t border-gray-200 bg-white px-2 py-1.5 text-xs text-gray-600 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400">
                    <button
                        type="button"
                        onClick={() => setShown((p) => Math.max(1, p - 1))}
                        disabled={shown <= 1}
                        className="rounded p-1 hover:bg-gray-100 disabled:opacity-30 dark:hover:bg-gray-800"
                        aria-label="Previous page"
                    >
                        <ChevronLeft size={16} />
                    </button>
                    <span>
                        Page {shown} of {pageCount}
                        {shown !== page && (
                            <button
                                type="button"
                                onClick={() => setShown(page)}
                                className="ml-2 text-brand-600 hover:underline dark:text-brand-400"
                            >
                                back to page {page}
                            </button>
                        )}
                    </span>
                    <button
                        type="button"
                        onClick={() => setShown((p) => Math.min(pageCount, p + 1))}
                        disabled={shown >= pageCount}
                        className="rounded p-1 hover:bg-gray-100 disabled:opacity-30 dark:hover:bg-gray-800"
                        aria-label="Next page"
                    >
                        <ChevronRight size={16} />
                    </button>
                </div>
            )}
        </div>
    );
}
