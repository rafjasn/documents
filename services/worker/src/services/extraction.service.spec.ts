import { Test, TestingModule } from '@nestjs/testing';
import { ExtractionService } from './extraction.service';

describe('ExtractionService', () => {
    let service: ExtractionService;

    beforeEach(async () => {
        const module: TestingModule = await Test.createTestingModule({
            providers: [ExtractionService]
        })
            .setLogger({
                log: () => {},
                error: () => {},
                warn: () => {},
                debug: () => {},
                verbose: () => {}
            })
            .compile();

        service = module.get(ExtractionService);
    });

    afterEach(() => jest.restoreAllMocks());

    describe('extractText()', () => {
        it('decodes text/plain buffer as UTF-8', async () => {
            const result = await service.extractText(Buffer.from('hello world'), 'text/plain');
            expect(result).toBe('hello world');
        });

        it('decodes text/csv buffer as UTF-8', async () => {
            const result = await service.extractText(Buffer.from('name,age\nJohn,30'), 'text/csv');
            expect(result).toBe('name,age\nJohn,30');
        });

        it('returns empty string for unsupported MIME type', async () => {
            const result = await service.extractText(Buffer.from('data'), 'application/zip');
            expect(result).toBe('');
        });

        it('routes application/pdf to the PDF extractor', async () => {
            jest.spyOn(service as any, 'extractFromPdf').mockResolvedValue('pdf text');
            const result = await service.extractText(Buffer.from('fake pdf'), 'application/pdf');
            expect((service as any).extractFromPdf).toHaveBeenCalledWith(expect.any(Buffer));
            expect(result).toBe('pdf text');
        });

        it('routes DOCX MIME type to the DOCX extractor', async () => {
            jest.spyOn(service as any, 'extractFromDocx').mockResolvedValue('docx text');
            const result = await service.extractText(
                Buffer.from('fake docx'),
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            );
            expect((service as any).extractFromDocx).toHaveBeenCalledWith(expect.any(Buffer));
            expect(result).toBe('docx text');
        });

        it.each(['image/png', 'image/jpeg', 'image/webp'])(
            'routes %s to the OCR extractor',
            async (mimeType) => {
                jest.spyOn(service as any, 'extractFromImage').mockResolvedValue('ocr text');
                const result = await service.extractText(Buffer.from('img'), mimeType);
                expect(result).toBe('ocr text');
            }
        );
    });

    describe('OCR worker', () => {
        it('returns empty string when OCR recognition fails', async () => {
            jest.spyOn(service as any, 'getOcrWorker').mockResolvedValue({
                recognize: jest.fn().mockRejectedValue(new Error('ocr crash')),
                terminate: jest.fn()
            });

            const result = await service.extractText(Buffer.from('img'), 'image/png');
            expect(result).toBe('');
        });
    });

    describe('onModuleDestroy()', () => {
        it('terminates the Tesseract worker if it was initialised', async () => {
            const terminate = jest.fn().mockResolvedValue(undefined);
            (service as any).tesseractWorker = { terminate };

            await service.onModuleDestroy();

            expect(terminate).toHaveBeenCalledTimes(1);
        });

        it('does nothing if no worker was ever initialised', async () => {
            await expect(service.onModuleDestroy()).resolves.not.toThrow();
        });
    });
});
