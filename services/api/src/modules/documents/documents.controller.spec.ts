// Only the controller's own logic: the guard and the documents service are stand-ins.
// @documents/shared loads an ESM-only package Jest can't load; the DTO needs only the enum.
jest.mock('../auth/jwt-auth.guard', () => ({ JwtAuthGuard: class {} }));
jest.mock('./documents.service', () => ({ DocumentsService: class {} }));
jest.mock('@documents/shared', () => ({ DocumentStatus: { COMPLETED: 'COMPLETED' } }));

import { DocumentsController } from './documents.controller';

const record = (id: string, thumbnailKey?: string) => ({
    id,
    fileName: `${id}.pdf`,
    status: 'COMPLETED',
    thumbnailKey
});

describe('DocumentsController', () => {
    it('signs a page of thumbnails without loading each document again', async () => {
        const documents = {
            listDocuments: jest.fn().mockResolvedValue({
                items: [record('doc-1', 'thumbnails/doc-1.png'), record('doc-2')],
                total: 2
            }),
            thumbnailUrlOf: jest.fn(async (doc: { thumbnailKey?: string }) =>
                doc.thumbnailKey ? `https://signed/${doc.thumbnailKey}` : undefined
            ),
            getDocument: jest.fn()
        };
        const controller = new DocumentsController(documents as never);

        const page = await controller.list('user-a', { limit: 100 } as never);

        expect(page.items.map((item) => item.thumbnailUrl)).toEqual([
            'https://signed/thumbnails/doc-1.png',
            undefined
        ]);
        expect(documents.getDocument).not.toHaveBeenCalled();
    });
});
