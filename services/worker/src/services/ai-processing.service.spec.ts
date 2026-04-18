import { Test, TestingModule } from '@nestjs/testing';
import { ConfigService } from '@nestjs/config';
import { GetCommand, UpdateCommand } from '@aws-sdk/lib-dynamodb';
import { AiProcessingService } from './ai-processing.service';
import { ModelProviderService } from './model-provider.service';
import { DocumentStatus } from '@documents/shared';

const TABLE = 'test-table';
const TOPIC_ARN = 'arn:aws:sns:us-east-1:123456789012:test-topic';
const EXTRACTED_TEXT = 'Sample document text for testing';

const VALID_CLASSIFICATION = JSON.stringify({
    language: 'en',
    category: 'INVOICE',
    keywords: ['invoice', 'payment', 'due'],
    tags: ['finance'],
    people: ['John Doe'],
    organizations: ['Acme Corp'],
    locations: ['New York'],
    confidence: 0.95
});

describe('AiProcessingService', () => {
    let service: AiProcessingService;
    let mockDynamoDB: { send: jest.Mock };
    let mockSNS: { send: jest.Mock };
    let mockModel: { complete: jest.Mock };

    beforeEach(async () => {
        mockDynamoDB = { send: jest.fn() };
        mockSNS = { send: jest.fn().mockResolvedValue({}) };
        mockModel = { complete: jest.fn() };

        mockDynamoDB.send.mockImplementation((command: unknown) => {
            if (command instanceof GetCommand) {
                return Promise.resolve({ Item: { extractedText: EXTRACTED_TEXT } });
            }
            return Promise.resolve({});
        });

        const module: TestingModule = await Test.createTestingModule({
            providers: [
                AiProcessingService,
                { provide: 'DYNAMODB_CLIENT', useValue: mockDynamoDB },
                { provide: 'SNS_CLIENT', useValue: mockSNS },
                { provide: ModelProviderService, useValue: mockModel },
                {
                    provide: ConfigService,
                    useValue: {
                        get: (key: string) => {
                            if (key === 'aws.dynamodb.tableName') return TABLE;
                            if (key === 'aws.sns.topicArn') return TOPIC_ARN;
                        }
                    }
                }
            ]
        }).compile();

        service = module.get(AiProcessingService);
    });

    afterEach(() => {
        jest.clearAllMocks();
        jest.useRealTimers();
    });

    describe('classify()', () => {
        const job = { documentId: 'doc-1', userId: 'user-1' };

        it('returns the parsed category', async () => {
            mockModel.complete.mockResolvedValue({ text: VALID_CLASSIFICATION, tokensUsed: 100 });

            const result = await service.classify(job);

            expect(result).toBe('INVOICE');
        });

        it('falls back to OTHER when model returns invalid JSON', async () => {
            mockModel.complete.mockResolvedValue({ text: 'not json at all', tokensUsed: 50 });

            const result = await service.classify(job);

            expect(result).toBe('OTHER');
        });

        it('falls back to OTHER when category field is missing from JSON', async () => {
            const noCategory = JSON.stringify({ language: 'en', confidence: 0.5 });
            mockModel.complete.mockResolvedValue({ text: noCategory, tokensUsed: 50 });

            const result = await service.classify(job);

            expect(result).toBe('OTHER');
        });

        it('merges people and organizations into names', async () => {
            mockModel.complete.mockResolvedValue({ text: VALID_CLASSIFICATION, tokensUsed: 100 });

            await service.classify(job);

            const updateCmd = mockDynamoDB.send.mock.calls
                .map(([cmd]) => cmd)
                .find((cmd) => cmd instanceof UpdateCommand);

            const metadata = updateCmd.input.ExpressionAttributeValues[':metadata'];
            expect(metadata.names).toEqual(['John Doe', 'Acme Corp']);
        });

        it('uses category.toLowerCase() as tag when tags array is empty', async () => {
            const noTags = JSON.stringify({ ...JSON.parse(VALID_CLASSIFICATION), tags: [] });
            mockModel.complete.mockResolvedValue({ text: noTags, tokensUsed: 100 });

            await service.classify(job);

            const updateCmd = mockDynamoDB.send.mock.calls
                .map(([cmd]) => cmd)
                .find((cmd) => cmd instanceof UpdateCommand);
            const metadata = updateCmd.input.ExpressionAttributeValues[':metadata'];
            expect(metadata.tags).toEqual(['invoice']);
        });

        it('writes to the correct DynamoDB table and key', async () => {
            mockModel.complete.mockResolvedValue({ text: VALID_CLASSIFICATION, tokensUsed: 100 });

            await service.classify(job);

            const updateCmd = mockDynamoDB.send.mock.calls
                .map(([cmd]) => cmd)
                .find((cmd) => cmd instanceof UpdateCommand);
            expect(updateCmd.input.TableName).toBe(TABLE);
            expect(updateCmd.input.Key).toEqual({ PK: 'USER#user-1', SK: 'DOC#doc-1' });
        });

        it('publishes CLASSIFYING SNS event with category and metadata', async () => {
            mockModel.complete.mockResolvedValue({ text: VALID_CLASSIFICATION, tokensUsed: 100 });

            await service.classify(job);

            expect(mockSNS.send).toHaveBeenCalledTimes(1);
            const event = JSON.parse(mockSNS.send.mock.calls[0][0].input.Message);
            expect(event.status).toBe(DocumentStatus.CLASSIFYING);
            expect(event.documentId).toBe('doc-1');
            expect(event.document.category).toBe('INVOICE');
            expect(event.document.metadata.names).toEqual(['John Doe', 'Acme Corp']);
        });

        it('retries on model failure and succeeds on the second attempt', async () => {
            jest.useFakeTimers();
            mockModel.complete
                .mockRejectedValueOnce(new Error('model unavailable'))
                .mockResolvedValue({ text: VALID_CLASSIFICATION, tokensUsed: 100 });

            const promise = service.classify(job);
            await jest.runAllTimersAsync();
            const result = await promise;

            expect(mockModel.complete).toHaveBeenCalledTimes(2);
            expect(result).toBe('INVOICE');
        });

        it('throws after exhausting all retries', async () => {
            jest.useFakeTimers();
            mockModel.complete.mockRejectedValue(new Error('persistent failure'));

            let caughtError: Error | undefined;
            const promise = service.classify(job).catch((e: Error) => {
                caughtError = e;
            });
            await jest.runAllTimersAsync();
            await promise;

            expect(caughtError?.message).toBe('persistent failure');
            expect(mockModel.complete).toHaveBeenCalledTimes(3);
        });

        it('does not call UpdateCommand when all retries are exhausted', async () => {
            jest.useFakeTimers();
            mockModel.complete.mockRejectedValue(new Error('fail'));

            const promise = service.classify(job).catch(() => {});
            await jest.runAllTimersAsync();
            await promise;

            const updateCalls = mockDynamoDB.send.mock.calls.filter(
                ([cmd]) => cmd instanceof UpdateCommand
            );
            expect(updateCalls).toHaveLength(0);
        });
    });

    describe('summarize()', () => {
        const job = { documentId: 'doc-1', userId: 'user-1', category: 'INVOICE' };

        it('writes COMPLETED status and summary text to DynamoDB', async () => {
            mockModel.complete.mockResolvedValue({ text: 'A concise summary.', tokensUsed: 80 });

            await service.summarize(job);

            const updateCmd = mockDynamoDB.send.mock.calls
                .map(([cmd]) => cmd)
                .find((cmd) => cmd instanceof UpdateCommand);
            const values = updateCmd.input.ExpressionAttributeValues;
            expect(values[':status']).toBe(DocumentStatus.COMPLETED);
            expect(values[':summary']).toBe('A concise summary.');
        });

        it('publishes COMPLETED SNS event with summary', async () => {
            mockModel.complete.mockResolvedValue({ text: 'A concise summary.', tokensUsed: 80 });

            await service.summarize(job);

            const event = JSON.parse(mockSNS.send.mock.calls[0][0].input.Message);
            expect(event.status).toBe(DocumentStatus.COMPLETED);
            expect(event.document.summary).toBe('A concise summary.');
        });

        it('includes GSI1SK in the DynamoDB update for status indexing', async () => {
            mockModel.complete.mockResolvedValue({ text: 'Summary.', tokensUsed: 40 });

            await service.summarize(job);

            const updateCmd = mockDynamoDB.send.mock.calls
                .map(([cmd]) => cmd)
                .find((cmd) => cmd instanceof UpdateCommand);
            const gsi1sk = updateCmd.input.ExpressionAttributeValues[':gsi1sk'] as string;
            expect(gsi1sk).toMatch(/^STATUS#COMPLETED#/);
        });
    });

    describe('markFailed()', () => {
        const job = { documentId: 'doc-1', userId: 'user-1' };

        it('writes FAILED status and error message to DynamoDB', async () => {
            await service.markFailed(job, new Error('something broke'));

            const updateCmd = mockDynamoDB.send.mock.calls
                .map(([cmd]) => cmd)
                .find((cmd) => cmd instanceof UpdateCommand);
            const values = updateCmd.input.ExpressionAttributeValues;
            expect(values[':status']).toBe(DocumentStatus.FAILED);
            expect(values[':error']).toBe('something broke');
        });

        it('publishes FAILED SNS event', async () => {
            await service.markFailed(job, new Error('something broke'));

            const event = JSON.parse(mockSNS.send.mock.calls[0][0].input.Message);
            expect(event.status).toBe(DocumentStatus.FAILED);
            expect(event.documentId).toBe('doc-1');
        });

        it('does not throw when SNS publish fails', async () => {
            mockSNS.send.mockRejectedValue(new Error('SNS unavailable'));

            await expect(service.markFailed(job, new Error('err'))).resolves.not.toThrow();
        });
    });
});
