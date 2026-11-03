import { readdirSync, readFileSync } from 'fs';
import * as path from 'path';
import { NotFoundException } from '@nestjs/common';
import type { ChatRequest } from '@documents/shared';
import { readEvents, type StreamedEvent } from './ai-client';
import { ConversationsService, HISTORY_TURNS } from './conversations.service';

const CONTRACTS = path.resolve(__dirname, '../../../../../packages/shared/contracts');
const EVENT_FIXTURES = path.join(CONTRACTS, 'fixtures', 'chat-stream-event');
const requestSchema = JSON.parse(
    readFileSync(path.join(CONTRACTS, 'chat-request.schema.json'), 'utf8')
);

function fixture(name: string): string {
    const payload = JSON.parse(readFileSync(path.join(EVENT_FIXTURES, `${name}.json`), 'utf8'));
    return `event: ${payload.type}\ndata: ${JSON.stringify(payload)}\n\n`;
}

type Item = Record<string, string>;
type Input = {
    Item: Item;
    Key: Item;
    ExpressionAttributeValues: Item;
    ScanIndexForward?: boolean;
    Limit?: number;
    RequestItems: Record<string, { DeleteRequest: { Key: Item } }[]>;
};

/** Just enough of DynamoDB for the service: put, get, query by prefix, update. */
class FakeTable {
    items = new Map<string, Item>();

    async send(command: { constructor: { name: string }; input: Input }): Promise<unknown> {
        const input = command.input;
        switch (command.constructor.name) {
            case 'PutCommand':
                this.items.set(`${input.Item.PK}|${input.Item.SK}`, { ...input.Item });
                return {};
            case 'GetCommand':
                return { Item: this.items.get(`${input.Key.PK}|${input.Key.SK}`) };
            case 'UpdateCommand': {
                const item = this.items.get(`${input.Key.PK}|${input.Key.SK}`)!;
                item.updatedAt = input.ExpressionAttributeValues[':now'];
                item.title = input.ExpressionAttributeValues[':title'];
                return {};
            }
            case 'DeleteCommand':
                this.items.delete(`${input.Key.PK}|${input.Key.SK}`);
                return {};
            case 'BatchWriteCommand':
                for (const requests of Object.values(input.RequestItems)) {
                    for (const { DeleteRequest } of requests) {
                        this.items.delete(`${DeleteRequest.Key.PK}|${DeleteRequest.Key.SK}`);
                    }
                }
                return {};
            case 'QueryCommand': {
                const [pk, prefix] = Object.values(input.ExpressionAttributeValues);
                let found = [...this.items.values()]
                    .filter((item) => item.PK === pk && item.SK.startsWith(prefix))
                    .sort((a, b) => a.SK.localeCompare(b.SK));
                if (input.ScanIndexForward === false) found = found.reverse();
                if (input.Limit) found = found.slice(0, input.Limit);
                return { Items: found };
            }
        }
        throw new Error(`unexpected ${command.constructor.name}`);
    }
}

class FakeAi {
    requests: { authorization: string; request: ChatRequest }[] = [];

    constructor(private readonly events: string[] | Error) {}

    async *stream(authorization: string, request: ChatRequest): AsyncGenerator<StreamedEvent> {
        this.requests.push({ authorization, request });
        if (this.events instanceof Error) throw this.events;
        yield* readEvents(
            (async function* (events: string[]) {
                for (const event of events) yield new TextEncoder().encode(event);
            })(this.events)
        );
    }
}

function service(events: string[] | Error) {
    const table = new FakeTable();
    const ai = new FakeAi(events);
    const conversations = new ConversationsService(
        table as never,
        { dynamodb: { tableName: 'documents' } } as never,
        ai as never
    );
    return { table, ai, conversations };
}

async function collect(stream: AsyncGenerator<string>): Promise<string[]> {
    const out: string[] = [];
    for await (const chunk of stream) out.push(chunk);
    return out;
}

const ANSWER = [
    fixture('step'),
    'event: token\ndata: {"type":"token","text":"Wrong."}\n\n',
    fixture('reset'),
    'event: token\ndata: {"type":"token","text":"The total is "}\n\n',
    'event: token\ndata: {"type":"token","text":"3 564,05 PLN [1]."}\n\n',
    fixture('citations'),
    fixture('done')
];

describe('ConversationsService', () => {
    it('passes events through unchanged and stores the question and the cited answer', async () => {
        const { conversations, ai } = service(ANSWER);
        const conversation = await conversations.create('user-a', { documentIds: ['doc-1'] });

        const sent = await collect(
            conversations.ask('user-a', conversation, 'What is the total?', 'Bearer token-a')
        );

        expect(sent).toEqual(ANSWER);
        const [question, answer] = await conversations.messages('user-a', conversation.id);
        expect(question).toMatchObject({ role: 'user', content: 'What is the total?' });
        expect(answer).toMatchObject({
            role: 'assistant',
            content: 'The total is 3 564,05 PLN [1].',
            intent: 'aggregate',
            traceId: '2f6c1e7a9b3d4c58a1e0f7b6d5c4a392'
        });
        expect(answer.citations).toHaveLength(2);
        expect(answer.usage?.llmCalls).toBe(3);
        // The user's own token goes to ai-service, with the conversation's scope.
        expect(ai.requests[0].authorization).toBe('Bearer token-a');
        expect(ai.requests[0].request.scope).toEqual({ documentIds: ['doc-1'] });
        const [listed] = await conversations.list('user-a');
        expect(listed.title).toBe('What is the total?');
    });

    it('sends recent history, oldest first, in the request shape ai-service expects', async () => {
        const { conversations, ai } = service([fixture('done')]);
        const conversation = await conversations.create('user-a');
        for (let i = 0; i < 6; i++) {
            await collect(conversations.ask('user-a', conversation, `question ${i}`, 'Bearer t'));
        }

        const { request } = ai.requests[5];
        // Five earlier questions; their answers were empty `done`-only streams.
        expect(request.history.map((turn) => turn.content).filter(Boolean)).toEqual([
            'question 0',
            'question 1',
            'question 2',
            'question 3',
            'question 4'
        ]);
        expect(request.history.length).toBeLessThanOrEqual(HISTORY_TURNS);
        expect(Object.keys(request).sort()).toEqual([...requestSchema.required].sort());
        for (const key of Object.keys(request)) {
            expect(requestSchema.properties).toHaveProperty(key);
        }
    });

    it('stores no answer when the stream ends in an error', async () => {
        const { conversations } = service([fixture('step'), fixture('error')]);
        const conversation = await conversations.create('user-a');

        const sent = await collect(conversations.ask('user-a', conversation, 'q', 'Bearer t'));

        expect(sent).toEqual([fixture('step'), fixture('error')]);
        const messages = await conversations.messages('user-a', conversation.id);
        expect(messages.map((m) => m.role)).toEqual(['user']);
    });

    it('turns an unreachable ai-service into an error event', async () => {
        const { conversations } = service(new Error('connect ECONNREFUSED'));
        const conversation = await conversations.create('user-a');

        const [event] = await collect(conversations.ask('user-a', conversation, 'q', 'Bearer t'));

        expect(event).toBe(
            'event: error\ndata: {"type":"error","message":"The assistant is unavailable. Please try again."}\n\n'
        );
    });

    it("never returns another user's conversation", async () => {
        const { conversations } = service([]);
        const conversation = await conversations.create('user-a');

        await expect(conversations.get('user-b', conversation.id)).rejects.toThrow(
            NotFoundException
        );
        await expect(conversations.messages('user-b', conversation.id)).rejects.toThrow(
            NotFoundException
        );
        expect(await conversations.list('user-b')).toEqual([]);
    });

    it('deletes a conversation with its messages, and only for its owner', async () => {
        const { conversations, table } = service(ANSWER);
        const conversation = await conversations.create('user-a');
        await collect(conversations.ask('user-a', conversation, 'What is the total?', 'Bearer t'));
        const other = await conversations.create('user-a');

        await expect(conversations.delete('user-b', conversation.id)).rejects.toThrow(
            NotFoundException
        );
        await conversations.delete('user-a', conversation.id);

        expect((await conversations.list('user-a')).map((c) => c.id)).toEqual([other.id]);
        expect([...table.items.keys()].some((key) => key.includes(conversation.id))).toBe(false);
    });

    it('stores the scope as a plain object, as DynamoDB requires', async () => {
        class ScopeDto {
            documentIds = ['doc-1'];
            categories = [];
        }
        const { conversations, table } = service([]);

        await conversations.create('user-a', new ScopeDto() as never);

        const [item] = table.items.values();
        expect(Object.getPrototypeOf(item.scope)).toBe(Object.prototype);
        expect(item.scope).toEqual({ documentIds: ['doc-1'] });
    });

    it('lists the conversations scoped to one document', async () => {
        const { conversations } = service([]);
        await conversations.create('user-a', { documentIds: ['doc-1'] });
        await conversations.create('user-a', { documentIds: ['doc-1', 'doc-2'] });
        await conversations.create('user-a');

        const found = await conversations.list('user-a', 'doc-1');

        expect(found.map((c) => c.scope.documentIds)).toEqual([['doc-1']]);
    });
});

describe('ChatStreamEvent contract (API proxy)', () => {
    const fixtures = readdirSync(EVENT_FIXTURES).filter((name) => name.endsWith('.json'));

    it('reads every fixture, even split across network chunks', async () => {
        const wire = fixtures.map((name) => fixture(name.replace('.json', ''))).join('');
        const bytes = new TextEncoder().encode(wire);
        const chunks = (async function* () {
            for (let i = 0; i < bytes.length; i += 7) yield bytes.slice(i, i + 7);
        })();

        const events: StreamedEvent[] = [];
        for await (const event of readEvents(chunks)) events.push(event);

        expect(events.map((e) => e.raw).join('')).toBe(wire);
        expect(events.map((e) => e.event.type)).toEqual(
            fixtures.map(
                (name) => JSON.parse(readFileSync(path.join(EVENT_FIXTURES, name), 'utf8')).type
            )
        );
    });
});
