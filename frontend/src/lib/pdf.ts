import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist';
import { api } from './api';
import { Document } from '@/types';

// pdf.js is loaded only when a page preview is first shown.
let library: Promise<typeof import('pdfjs-dist')> | null = null;

function pdfjs() {
    library ??= import('pdfjs-dist').then((lib) => {
        lib.GlobalWorkerOptions.workerSrc = new URL(
            'pdfjs-dist/build/pdf.worker.min.mjs',
            import.meta.url
        ).toString();
        return lib;
    });
    return library;
}

export interface DocumentFile {
    document: Document;
    url: string;
}

// Download URLs are signed for an hour; reuse them for most of it.
const URL_SECONDS = 50 * 60;
const files = new Map<string, { file: Promise<DocumentFile>; expires: number }>();
const pdfs = new Map<string, Promise<PDFDocumentProxy>>();

/** A document and a signed URL for its file, fetched once per document. */
export function documentFile(docId: string): Promise<DocumentFile> {
    const cached = files.get(docId);
    if (cached && cached.expires > Date.now()) return cached.file;
    const file = Promise.all([api.getDocument(docId), api.getDownloadUrl(docId)]).then(
        ([document, { downloadUrl }]) => ({ document, url: downloadUrl })
    );
    file.catch(() => files.delete(docId));
    files.set(docId, { file, expires: Date.now() + URL_SECONDS * 1000 });
    return file;
}

/** A document's PDF, downloaded once per page load. */
export function openPdf(docId: string, url: string): Promise<PDFDocumentProxy> {
    let pdf = pdfs.get(docId);
    if (!pdf) {
        pdf = pdfjs().then((lib) => lib.getDocument({ url }).promise);
        pdf.catch(() => pdfs.delete(docId));
        pdfs.set(docId, pdf);
    }
    return pdf;
}

/** Render a page to the canvas, `width` CSS pixels wide at the screen's pixel density. */
export async function renderPage(
    pdf: PDFDocumentProxy,
    pageNumber: number,
    canvas: HTMLCanvasElement,
    width: number
): Promise<RenderTask> {
    const page = await pdf.getPage(pageNumber);
    const scale = (width / page.getViewport({ scale: 1 }).width) * (window.devicePixelRatio || 1);
    const viewport = page.getViewport({ scale });
    canvas.width = Math.floor(viewport.width);
    canvas.height = Math.floor(viewport.height);
    return page.render({ canvas, viewport });
}
