// Extracts text from every corpus document exactly as the NestJS worker did
// (services/worker/src/services/extraction.service.ts, removed in Stage 07): pdf-parse for PDFs (text layer
// only, so scans yield nothing), mammoth raw text for DOCX (images dropped) and
// Tesseract.js with English data for images. The V0 baseline answers from this text.
//
// Run from the repository root after `npm ci`: node services/ai/evals/legacy-extract.mjs
import { mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import mammoth from 'mammoth';
import { PDFParse } from 'pdf-parse';
import { createWorker } from 'tesseract.js';

const corpus = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'corpus');
const output = path.join(corpus, 'extracted', 'legacy');
const manifest = (await readFile(path.join(corpus, 'manifest.jsonl'), 'utf8'))
    .split('\n')
    .filter(Boolean)
    .map((line) => JSON.parse(line));

await mkdir(output, { recursive: true });
let ocr;
const summary = { written: 0, skipped: 0, empty: [] };

for (const doc of manifest) {
    const target = path.join(output, `${doc.doc_id}.txt`);
    if (await stat(target).catch(() => null)) {
        summary.skipped++;
        continue;
    }
    const buffer = await readFile(path.join(corpus, 'data', doc.path));
    let text = '';
    if (doc.format === 'pdf' || doc.format === 'scanned_pdf') {
        const parser = new PDFParse({ data: buffer });
        text = (await parser.getText()).text || '';
        await parser.destroy();
    } else if (doc.format === 'docx') {
        text = (await mammoth.extractRawText({ buffer })).value;
    } else {
        ocr ??= await createWorker('eng', 1, {
            cachePath: path.join(corpus, 'raw', 'tessdata'),
            logger: () => {}
        });
        text = (await ocr.recognize(buffer)).data.text || '';
    }
    await writeFile(target, text);
    summary.written++;
    if (!text.trim()) summary.empty.push(doc.doc_id);
}

await ocr?.terminate();
console.log(
    `written=${summary.written} skipped=${summary.skipped} empty=${summary.empty.length}` +
        (summary.empty.length ? ` (${summary.empty.join(', ')})` : '')
);
