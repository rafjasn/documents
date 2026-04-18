import { Injectable, Logger, OnModuleDestroy } from '@nestjs/common';
import { createWorker, Worker } from 'tesseract.js';

@Injectable()
export class ExtractionService implements OnModuleDestroy {
    private readonly logger = new Logger(ExtractionService.name);
    private tesseractWorker: Worker | null = null;
    private tesseractReady: Promise<Worker> | null = null;

    async extractText(buffer: any, mimeType: string): Promise<string> {
        switch (mimeType) {
            case 'application/pdf':
                return this.extractFromPdf(buffer);
            case 'text/plain':
            case 'text/csv':
                return buffer.toString('utf-8');
            case 'image/png':
            case 'image/jpeg':
            case 'image/webp':
                return this.extractFromImage(buffer);
            case 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
                return this.extractFromDocx(buffer);
            default:
                this.logger.warn(`Unsupported mime type for extraction: ${mimeType}`);
                return '';
        }
    }

    async onModuleDestroy() {
        if (this.tesseractWorker) {
            await this.tesseractWorker.terminate();
            this.tesseractWorker = null;
        }
    }

    private async extractFromPdf(buffer: Buffer): Promise<string> {
        try {
            const { PDFParse } = (await import('pdf-parse')) as any;
            const parser = new PDFParse({ data: buffer });
            const result = await parser.getText();
            await parser.destroy();
            return result.text || '';
        } catch (error) {
            this.logger.error(`PDF extraction failed: ${error}`);
            throw new Error(`PDF text extraction failed: ${(error as Error).message}`, {
                cause: error
            });
        }
    }

    private async extractFromImage(buffer: Buffer): Promise<string> {
        try {
            const worker = await this.getOcrWorker();
            const { data } = await worker.recognize(buffer);
            this.logger.log(`OCR complete — confidence: ${data.confidence.toFixed(1)}%`);
            return data.text || '';
        } catch (error) {
            this.logger.error(`OCR failed: ${error}`);
            return '';
        }
    }

    private async extractFromDocx(buffer: Buffer): Promise<string> {
        try {
            const mammoth = await import('mammoth');
            const result = await mammoth.extractRawText({ buffer });
            return result.value;
        } catch (error) {
            this.logger.error(`DOCX extraction failed: ${error}`);
            return '';
        }
    }

    private getOcrWorker(): Promise<Worker> {
        if (this.tesseractReady) return this.tesseractReady;

        this.tesseractReady = (async () => {
            this.logger.log('Initialising Tesseract OCR worker...');
            const worker = await createWorker('eng', 1, {
                logger: () => {}
            });
            this.tesseractWorker = worker;
            this.logger.log('Tesseract OCR worker ready');
            return worker;
        })();

        return this.tesseractReady;
    }
}
