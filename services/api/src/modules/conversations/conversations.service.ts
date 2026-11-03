import { Inject, Injectable, Logger, NotFoundException } from '@nestjs/common';
import type { ConfigType } from '@nestjs/config';
import {
    BatchWriteCommand,
    DeleteCommand,
    DynamoDBDocumentClient,
    GetCommand,
    PutCommand,
    QueryCommand,
    UpdateCommand
} from '@aws-sdk/lib-dynamodb';
import { randomUUID as uuid } from 'crypto';
import type {
    ChatScope,
    ChatUsage,
    Citation,
    ChatIntent,
    Conversation,
    ConversationMessage
} from '@documents/shared';
import { awsConfig } from '@config/index';
import { AiServiceClient } from './ai-client';

/** Earlier turns sent with each question: enough to resolve "and the due date?". */
export const HISTORY_TURNS = 10;
const TITLE_CHARS = 80;
const BATCH_WRITE_ITEMS = 25; // DynamoDB's limit per BatchWriteItem
const UNAVAILABLE = 'The assistant is unavailable. Please try again.';

/**
 * Conversations and their messages in the documents table:
 * USER#{userId} / CONV#{conversationId} and CONV#{conversationId} / MSG#{timestamp}#{id}.
 * Every read starts from the user's own conversation item, so a conversation ID alone
 * never reaches another user's messages.
 */
@Injectable()
export class ConversationsService {
    private readonly logger = new Logger(ConversationsService.name);
    private lastTimestamp = 0;

    constructor(
        @Inject('DYNAMODB_CLIENT') private readonly dynamodb: DynamoDBDocumentClient,
        @Inject(awsConfig.KEY) private readonly aws: ConfigType<typeof awsConfig>,
        private readonly ai: AiServiceClient
    ) {}

    private get table(): string {
        return this.aws.dynamodb.tableName;
    }

    async create(userId: string, scope: ChatScope = {}, title?: string): Promise<Conversation> {
        const now = new Date().toISOString();
        const conversation: Conversation = {
            id: uuid(),
            userId,
            title: title?.trim() || '',
            scope: plainScope(scope),
            createdAt: now,
            updatedAt: now
        };
        await this.dynamodb.send(
            new PutCommand({
                TableName: this.table,
                Item: { PK: `USER#${userId}`, SK: `CONV#${conversation.id}`, ...conversation }
            })
        );
        return conversation;
    }

    async get(userId: string, conversationId: string): Promise<Conversation> {
        const result = await this.dynamodb.send(
            new GetCommand({
                TableName: this.table,
                Key: { PK: `USER#${userId}`, SK: `CONV#${conversationId}` }
            })
        );
        if (!result.Item) {
            throw new NotFoundException('Conversation not found');
        }
        return toConversation(result.Item);
    }

    /** The user's conversations, most recently active first; optionally only those
     * scoped to exactly one document. */
    async list(userId: string, documentId?: string): Promise<Conversation[]> {
        const items: Record<string, unknown>[] = [];
        let start: Record<string, unknown> | undefined;
        do {
            const page = await this.dynamodb.send(
                new QueryCommand({
                    TableName: this.table,
                    KeyConditionExpression: 'PK = :pk AND begins_with(SK, :conv)',
                    ExpressionAttributeValues: { ':pk': `USER#${userId}`, ':conv': 'CONV#' },
                    ExclusiveStartKey: start
                })
            );
            items.push(...((page.Items ?? []) as Record<string, unknown>[]));
            start = page.LastEvaluatedKey;
        } while (start);
        return items
            .map(toConversation)
            .filter(
                (c) =>
                    !documentId ||
                    (c.scope.documentIds?.length === 1 && c.scope.documentIds[0] === documentId)
            )
            .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
    }

    /** Delete a conversation: its messages first, so a failure part-way can be retried. */
    async delete(userId: string, conversationId: string): Promise<void> {
        await this.get(userId, conversationId);
        let start: Record<string, unknown> | undefined;
        do {
            const page = await this.dynamodb.send(
                new QueryCommand({
                    TableName: this.table,
                    KeyConditionExpression: 'PK = :pk AND begins_with(SK, :msg)',
                    ExpressionAttributeValues: { ':pk': `CONV#${conversationId}`, ':msg': 'MSG#' },
                    ProjectionExpression: 'PK, SK',
                    ExclusiveStartKey: start
                })
            );
            const keys = (page.Items ?? []).map((item) => ({ PK: item.PK, SK: item.SK }));
            for (let i = 0; i < keys.length; i += BATCH_WRITE_ITEMS) {
                await this.deleteAll(keys.slice(i, i + BATCH_WRITE_ITEMS));
            }
            start = page.LastEvaluatedKey;
        } while (start);
        await this.dynamodb.send(
            new DeleteCommand({
                TableName: this.table,
                Key: { PK: `USER#${userId}`, SK: `CONV#${conversationId}` }
            })
        );
    }

    private async deleteAll(keys: Record<string, unknown>[]): Promise<void> {
        let requests = keys.map((Key) => ({ DeleteRequest: { Key } }));
        for (let attempt = 0; requests.length > 0; attempt++) {
            if (attempt === 5) throw new Error('Could not delete the conversation messages');
            if (attempt > 0) await new Promise((done) => setTimeout(done, 100 * 2 ** attempt));
            const result = await this.dynamodb.send(
                new BatchWriteCommand({ RequestItems: { [this.table]: requests } })
            );
            requests = (result.UnprocessedItems?.[this.table] ?? []) as typeof requests;
        }
    }

    async messages(userId: string, conversationId: string): Promise<ConversationMessage[]> {
        await this.get(userId, conversationId);
        return this.messagesOf(conversationId);
    }

    private async messagesOf(
        conversationId: string,
        limit?: number
    ): Promise<ConversationMessage[]> {
        const result = await this.dynamodb.send(
            new QueryCommand({
                TableName: this.table,
                KeyConditionExpression: 'PK = :pk AND begins_with(SK, :msg)',
                ExpressionAttributeValues: { ':pk': `CONV#${conversationId}`, ':msg': 'MSG#' },
                ScanIndexForward: limit === undefined,
                Limit: limit
            })
        );
        const items = (result.Items ?? []).map(toMessage);
        return limit === undefined ? items : items.reverse();
    }

    private async save(
        conversationId: string,
        message: Omit<ConversationMessage, 'id' | 'conversationId' | 'createdAt'>
    ): Promise<void> {
        const id = uuid();
        const createdAt = this.nextTimestamp();
        await this.dynamodb.send(
            new PutCommand({
                TableName: this.table,
                Item: {
                    PK: `CONV#${conversationId}`,
                    SK: `MSG#${createdAt}#${id}`,
                    id,
                    conversationId,
                    createdAt,
                    ...message
                }
            })
        );
    }

    /** Message keys sort by time; two messages saved in the same millisecond still
     * keep their order. */
    private nextTimestamp(): string {
        this.lastTimestamp = Math.max(Date.now(), this.lastTimestamp + 1);
        return new Date(this.lastTimestamp).toISOString();
    }

    private async touch(userId: string, conversation: Conversation, question: string) {
        const title = conversation.title || question.slice(0, TITLE_CHARS);
        await this.dynamodb.send(
            new UpdateCommand({
                TableName: this.table,
                Key: { PK: `USER#${userId}`, SK: `CONV#${conversation.id}` },
                UpdateExpression: 'SET updatedAt = :now, title = :title',
                ExpressionAttributeValues: { ':now': new Date().toISOString(), ':title': title }
            })
        );
    }

    /**
     * Ask ai-service and pass its events through unchanged. The user's question is
     * stored first; the answer, assembled from the token events, is stored with its
     * citations when the stream ends with `done`.
     */
    async *ask(
        userId: string,
        conversation: Conversation,
        question: string,
        authorization: string
    ): AsyncGenerator<string> {
        const history = (await this.messagesOf(conversation.id, HISTORY_TURNS)).map((m) => ({
            role: m.role,
            content: m.content
        }));
        await this.save(conversation.id, { role: 'user', content: question });
        await this.touch(userId, conversation, question);

        let answer = '';
        let citations: Citation[] = [];
        let finished: { intent: ChatIntent; usage: ChatUsage; traceId: string | null } | null =
            null;
        try {
            for await (const { raw, event } of this.ai.stream(authorization, {
                conversationId: conversation.id,
                message: question,
                history,
                scope: conversation.scope
            })) {
                yield raw;
                if (event.type === 'token') answer += event.text;
                else if (event.type === 'reset') answer = '';
                else if (event.type === 'citations') citations = event.citations;
                else if (event.type === 'done') finished = event;
                else if (event.type === 'error') finished = null;
            }
        } catch (error) {
            this.logger.error(`AI service stream failed: ${(error as Error).message}`);
            yield `event: error\ndata: ${JSON.stringify({ type: 'error', message: UNAVAILABLE })}\n\n`;
            return;
        }
        if (finished) {
            await this.save(conversation.id, {
                role: 'assistant',
                content: answer,
                citations,
                usage: finished.usage,
                intent: finished.intent,
                traceId: finished.traceId
            });
        }
    }
}

/** The scope as a plain object of its set fields: validation hands over a class
 * instance, which the DynamoDB marshaller rejects. */
export function plainScope(scope: ChatScope): ChatScope {
    const plain: ChatScope = {};
    if (scope.documentIds?.length) plain.documentIds = [...scope.documentIds];
    if (scope.categories?.length) plain.categories = [...scope.categories];
    if (scope.languages?.length) plain.languages = [...scope.languages];
    if (scope.dateFrom) plain.dateFrom = scope.dateFrom;
    if (scope.dateTo) plain.dateTo = scope.dateTo;
    return plain;
}

function toConversation(item: Record<string, unknown>): Conversation {
    return {
        id: String(item.id),
        userId: String(item.userId),
        title: String(item.title ?? ''),
        scope: (item.scope as ChatScope) ?? {},
        createdAt: String(item.createdAt),
        updatedAt: String(item.updatedAt)
    };
}

function toMessage(item: Record<string, unknown>): ConversationMessage {
    const message = { ...item };
    delete message.PK;
    delete message.SK;
    return message as unknown as ConversationMessage;
}
