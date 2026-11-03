import { NotFoundException } from '@nestjs/common';

// Only the controller's own logic: the guard and the documents service are stand-ins.
jest.mock('../auth/jwt-auth.guard', () => ({ JwtAuthGuard: class {} }));
jest.mock('../documents/documents.service', () => ({ DocumentsService: class {} }));

import { ConversationsController } from './conversations.controller';

/** Documents of user-a only, as DocumentsService.getDocument behaves. */
const documents = {
    getDocument: jest.fn(async (userId: string, documentId: string) => {
        if (userId !== 'user-a' || documentId !== 'doc-of-user-a') {
            throw new NotFoundException('Document not found');
        }
        return { id: documentId };
    })
};

describe('ConversationsController (tenant isolation)', () => {
    it("refuses a conversation scoped to another user's document", async () => {
        const conversations = { create: jest.fn() };
        const controller = new ConversationsController(conversations as never, documents as never);

        await expect(
            controller.create('user-a', {
                scope: { documentIds: ['doc-of-user-a', 'doc-of-user-b'] }
            } as never)
        ).rejects.toThrow(NotFoundException);
        expect(conversations.create).not.toHaveBeenCalled();
    });

    it("creates one scoped to the user's own documents", async () => {
        const conversations = { create: jest.fn().mockResolvedValue({ id: 'conv-1' }) };
        const controller = new ConversationsController(conversations as never, documents as never);

        await controller.create('user-a', { scope: { documentIds: ['doc-of-user-a'] } } as never);

        expect(conversations.create).toHaveBeenCalledWith(
            'user-a',
            { documentIds: ['doc-of-user-a'] },
            undefined
        );
    });
});
