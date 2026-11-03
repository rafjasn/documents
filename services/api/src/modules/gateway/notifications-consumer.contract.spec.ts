import { readdirSync, readFileSync } from 'fs';
import * as path from 'path';
import { NotificationsConsumerService } from './notifications-consumer.service';

// The real gateway verifies tokens with jwks-rsa, an ESM-only package Jest can't load;
// these tests replace it with a spy anyway.
jest.mock('./document.gateway', () => ({ DocumentGateway: class {} }));

// Consumer side of the WsDocumentStatusEvent contract. The Python ingestion worker
// publishes these events to SNS; its tests check that every event shape it emits has a
// fixture here and that the fixtures match the JSON Schema.
const CONTRACTS = path.resolve(__dirname, '../../../../../packages/shared/contracts');
const FIXTURES = path.join(CONTRACTS, 'fixtures', 'ws-document-status-event');
const schema = JSON.parse(
    readFileSync(path.join(CONTRACTS, 'ws-document-status-event.schema.json'), 'utf8')
);
const fixtures = readdirSync(FIXTURES).filter((name) => name.endsWith('.json'));

/** DocumentStatus values, read from source: importing @documents/shared in Jest loads
 * its auth module, which depends on an ESM-only package. */
function documentStatuses(): string[] {
    const source = readFileSync(path.resolve(CONTRACTS, '..', 'src', 'index.ts'), 'utf8');
    const body = /export enum DocumentStatus \{([^}]*)\}/.exec(source)?.[1] ?? '';
    return [...body.matchAll(/=\s*'([A-Z_]+)'/g)].map((match) => match[1]);
}

/** What SNS delivers to the API's queue for an event published with its attributes. */
function snsDelivery(event: unknown, userId: string) {
    return {
        MessageId: 'message-1',
        ReceiptHandle: 'receipt-1',
        Body: JSON.stringify({
            Type: 'Notification',
            TopicArn: 'arn:aws:sns:us-east-1:000000000000:documents-notifications',
            Message: JSON.stringify(event),
            MessageAttributes: {
                eventType: { Type: 'String', Value: 'DOCUMENT_STATUS' },
                userId: { Type: 'String', Value: userId }
            }
        })
    };
}

describe('WsDocumentStatusEvent contract (API consumer)', () => {
    it('has fixtures to test against', () => {
        expect(fixtures.length).toBeGreaterThanOrEqual(4);
    });

    it('uses the same statuses as the schema', () => {
        expect(documentStatuses()).toEqual(schema.properties.status.enum);
    });

    it.each(fixtures)('pushes %s to the owner unchanged', async (fixture) => {
        const event = JSON.parse(readFileSync(path.join(FIXTURES, fixture), 'utf8'));
        for (const field of schema.required as string[]) {
            expect(event).toHaveProperty(field);
        }
        const gateway = { notifyStatusChange: jest.fn() };
        const sqs = { send: jest.fn().mockResolvedValue({}) };
        const config = { get: jest.fn() };
        const consumer = new NotificationsConsumerService(
            sqs as never,
            {} as never,
            gateway as never,
            config as never
        ) as unknown as { handleMessage(message: unknown): Promise<void> };

        await consumer.handleMessage(snsDelivery(event, 'user-1'));

        expect(gateway.notifyStatusChange).toHaveBeenCalledWith(
            'user-1',
            event.documentId,
            event.status,
            event.document
        );
        expect(sqs.send).toHaveBeenCalledTimes(1); // the message is deleted
    });
});
