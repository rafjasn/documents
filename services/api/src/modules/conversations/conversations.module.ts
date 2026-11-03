import { Module } from '@nestjs/common';
import { DocumentsModule } from '../documents/documents.module';
import { AiServiceClient } from './ai-client';
import { ConversationsController } from './conversations.controller';
import { ConversationsService } from './conversations.service';

@Module({
    imports: [DocumentsModule],
    controllers: [ConversationsController],
    providers: [ConversationsService, AiServiceClient]
})
export class ConversationsModule {}
