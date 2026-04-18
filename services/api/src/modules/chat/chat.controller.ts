import { Controller, Post, Get, Param, Body, Res, UseGuards } from '@nestjs/common';
import type { Response } from 'express';
import { JwtAuthGuard } from '../auth/jwt-auth.guard';
import { CurrentUser } from '@common/decorators/current-user.decorator';
import { DocumentsService } from '../documents/documents.service';
import { ChatService } from './chat.service';

@Controller('ai/chat')
@UseGuards(JwtAuthGuard)
export class ChatController {
    constructor(
        private readonly chatService: ChatService,
        private readonly documentsService: DocumentsService
    ) {}

    @Post(':documentId/stream')
    async stream(
        @Param('documentId') documentId: string,
        @Body() body: { message: string },
        @CurrentUser('userId') userId: string,
        @Res() res: Response
    ): Promise<void> {
        await this.documentsService.getDocument(userId, documentId);

        res.setHeader('Content-Type', 'text/event-stream');
        res.setHeader('Cache-Control', 'no-cache');
        res.setHeader('Connection', 'keep-alive');
        res.setHeader('X-Accel-Buffering', 'no');

        for await (const chunk of this.chatService.streamChat(documentId, userId, body.message)) {
            res.write(`data: ${JSON.stringify({ chunk })}\n\n`);
        }

        res.write('data: [DONE]\n\n');
        res.end();
    }

    @Get(':documentId/history')
    async history(@Param('documentId') documentId: string, @CurrentUser('userId') userId: string) {
        await this.documentsService.getDocument(userId, documentId);

        return this.chatService.getHistory(documentId);
    }
}
