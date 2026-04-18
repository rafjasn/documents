import { Module } from '@nestjs/common';
import { ChatController } from './chat.controller';
import { ChatService } from './chat.service';
import { ModelProviderService } from './model-provider.service';
import { DocumentsModule } from '../documents/documents.module';

@Module({
    imports: [DocumentsModule],
    controllers: [ChatController],
    providers: [ChatService, ModelProviderService]
})
export class ChatModule {}
