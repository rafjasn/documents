import { Injectable, Inject, NotFoundException } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import {
    DynamoDBDocumentClient,
    PutCommand,
    QueryCommand,
    GetCommand
} from '@aws-sdk/lib-dynamodb';
import { v4 as uuid } from 'uuid';
import { ChatMessage } from '@documents/shared';
import { ModelProviderService } from './model-provider.service';

@Injectable()
export class ChatService {
    private readonly tableName: string;

    constructor(
        @Inject('DYNAMODB_CLIENT')
        private readonly dynamodb: DynamoDBDocumentClient,
        private readonly model: ModelProviderService,
        config: ConfigService
    ) {
        this.tableName = config.get<string>('aws.dynamodb.tableName')!;
    }

    async getHistory(documentId: string): Promise<ChatMessage[]> {
        const result = await this.dynamodb.send(
            new QueryCommand({
                TableName: this.tableName,
                KeyConditionExpression: 'PK = :pk AND begins_with(SK, :skPrefix)',
                ExpressionAttributeValues: {
                    ':pk': `CHAT#${documentId}`,
                    ':skPrefix': 'MSG#'
                },
                ScanIndexForward: true
            })
        );

        return (result.Items || []) as ChatMessage[];
    }

    async chat(documentId: string, userId: string, userMessage: string): Promise<string> {
        const extractedText = await this.fetchExtractedText(userId, documentId);
        const history = await this.getHistory(documentId);

        await this.saveMessage(documentId, userId, 'user', userMessage);

        const { systemPrompt, userPrompt } = this.buildPrompt(extractedText, history, userMessage);
        const response = await this.model.complete({ systemPrompt, userPrompt });

        await this.saveMessage(documentId, userId, 'assistant', response.text);

        return response.text;
    }

    async *streamChat(
        documentId: string,
        userId: string,
        userMessage: string
    ): AsyncGenerator<string> {
        const extractedText = await this.fetchExtractedText(userId, documentId);
        const history = await this.getHistory(documentId);
        await this.saveMessage(documentId, userId, 'user', userMessage);

        const { systemPrompt, userPrompt } = this.buildPrompt(extractedText, history, userMessage);

        let fullResponse = '';

        for await (const chunk of this.model.stream({ systemPrompt, userPrompt })) {
            fullResponse += chunk;
            yield chunk;
        }

        await this.saveMessage(documentId, userId, 'assistant', fullResponse);
    }

    private async fetchExtractedText(userId: string, documentId: string): Promise<string> {
        const result = await this.dynamodb.send(
            new GetCommand({
                TableName: this.tableName,
                Key: { PK: `USER#${userId}`, SK: `DOC#${documentId}` },
                ProjectionExpression: 'extractedText'
            })
        );

        if (!result.Item) {
            throw new NotFoundException(`Document ${documentId} not found`);
        }

        return (result.Item.extractedText as string) || '';
    }

    private buildPrompt(
        extractedText: string,
        history: ChatMessage[],
        userMessage: string
    ): { systemPrompt: string; userPrompt: string } {
        const historyText = history
            .map((m) => `${m.role === 'user' ? 'User' : 'Assistant'}: ${m.content}`)
            .join('\n');

        return {
            systemPrompt: `You are a helpful assistant answering questions about a document. Here is the document content:\n\n${extractedText.slice(0, 8000)}`,
            userPrompt: historyText ? `${historyText}\nUser: ${userMessage}` : userMessage
        };
    }

    private async saveMessage(
        documentId: string,
        userId: string,
        role: 'user' | 'assistant',
        content: string
    ): Promise<void> {
        const now = new Date().toISOString();
        const id = uuid();
        const message: ChatMessage = { id, documentId, userId, role, content, createdAt: now };

        await this.dynamodb.send(
            new PutCommand({
                TableName: this.tableName,
                Item: {
                    PK: `CHAT#${documentId}`,
                    SK: `MSG#${now}#${id}`,
                    ...message
                }
            })
        );
    }
}
