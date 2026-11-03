import {
    Body,
    Controller,
    Delete,
    Get,
    HttpCode,
    HttpStatus,
    Param,
    ParseUUIDPipe,
    Post,
    Query,
    Req,
    Res,
    UseGuards
} from '@nestjs/common';
import { ApiBearerAuth, ApiOperation, ApiTags } from '@nestjs/swagger';
import type { Request, Response } from 'express';
import type { Conversation, ConversationMessage } from '@documents/shared';
import { JwtAuthGuard } from '../auth/jwt-auth.guard';
import { CurrentUser } from '@common/decorators/current-user.decorator';
import { DocumentsService } from '../documents/documents.service';
import { ConversationsService } from './conversations.service';
import { CreateConversationDto, ListConversationsDto, StreamMessageDto } from './conversation.dto';

@ApiTags('ai')
@ApiBearerAuth()
@UseGuards(JwtAuthGuard)
@Controller('ai/conversations')
export class ConversationsController {
    constructor(
        private readonly conversations: ConversationsService,
        private readonly documents: DocumentsService
    ) {}

    @Post()
    @ApiOperation({ summary: 'Start a conversation, optionally scoped to some documents' })
    async create(
        @CurrentUser('userId') userId: string,
        @Body() dto: CreateConversationDto
    ): Promise<Conversation> {
        // Scoped documents must be the user's own.
        for (const documentId of dto.scope?.documentIds ?? []) {
            await this.documents.getDocument(userId, documentId);
        }
        return this.conversations.create(userId, dto.scope ?? {}, dto.title);
    }

    @Get()
    @ApiOperation({ summary: "The user's conversations, most recent first" })
    list(
        @CurrentUser('userId') userId: string,
        @Query() query: ListConversationsDto
    ): Promise<Conversation[]> {
        return this.conversations.list(userId, query.documentId);
    }

    @Get(':id/messages')
    @ApiOperation({ summary: 'Messages of a conversation, oldest first' })
    messages(
        @CurrentUser('userId') userId: string,
        @Param('id', ParseUUIDPipe) id: string
    ): Promise<ConversationMessage[]> {
        return this.conversations.messages(userId, id);
    }

    @Delete(':id')
    @HttpCode(HttpStatus.NO_CONTENT)
    @ApiOperation({ summary: 'Delete a conversation and its messages' })
    remove(
        @CurrentUser('userId') userId: string,
        @Param('id', ParseUUIDPipe) id: string
    ): Promise<void> {
        return this.conversations.delete(userId, id);
    }

    @Post(':id/stream')
    @ApiOperation({ summary: 'Ask a question; the answer streams as server-sent events' })
    async stream(
        @CurrentUser('userId') userId: string,
        @Param('id', ParseUUIDPipe) id: string,
        @Body() dto: StreamMessageDto,
        @Req() request: Request,
        @Res() response: Response
    ): Promise<void> {
        const conversation = await this.conversations.get(userId, id);

        response.setHeader('Content-Type', 'text/event-stream');
        response.setHeader('Cache-Control', 'no-cache');
        response.setHeader('Connection', 'keep-alive');
        response.setHeader('X-Accel-Buffering', 'no');
        response.flushHeaders();

        const authorization = request.headers.authorization ?? '';
        for await (const event of this.conversations.ask(
            userId,
            conversation,
            dto.message,
            authorization
        )) {
            response.write(event);
        }
        response.end();
    }
}
