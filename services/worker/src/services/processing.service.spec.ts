import { Test, TestingModule } from '@nestjs/testing';
import { ConfigService } from '@nestjs/config';
import { UpdateCommand } from '@aws-sdk/lib-dynamodb';
import { ProcessingService } from './processing.service';
import { ExtractionService } from './extraction.service';
import { AiProcessingService } from './ai-processing.service';
import { SqsPublisherService } from './sqs-publisher.service';
import { DocumentStatus } from '@documents/shared';

const JOB = {
    documentId: 'doc-1',
    userId: 'user-1',
    s3Key: 'uploads/user-1/doc-1/file.pdf',
    mimeType: 'text/plain'
};

function s3Response(content = 'file content', contentType = 'application/pdf') {
    return {
        Body: { transformToByteArray: jest.fn().mockResolvedValue(Buffer.from(content)) },
        ContentType: contentType
    };
}

describe('ProcessingService', () => {
    let service: ProcessingService;
    let mockSQS: { send: jest.Mock };
    let mockSNS: { send: jest.Mock };
    let mockS3: { send: jest.Mock };
    let mockDynamoDB: { send: jest.Mock };
    let mockExtraction: { extractText: jest.Mock };
    let mockAi: { classify: jest.Mock; summarize: jest.Mock };
    let mockSqsPublisher: { publishThumbnailJob: jest.Mock };

    beforeEach(async () => {
        jest.useFakeTimers();

        mockSQS = { send: jest.fn().mockResolvedValue({}) };
        mockSNS = { send: jest.fn().mockResolvedValue({}) };
        mockS3 = { send: jest.fn().mockResolvedValue(s3Response()) };
        mockDynamoDB = { send: jest.fn().mockResolvedValue({}) };
        mockExtraction = { extractText: jest.fn().mockResolvedValue('extracted text') };
        mockAi = {
            classify: jest.fn().mockResolvedValue('invoice'),
            summarize: jest.fn().mockResolvedValue(undefined)
        };
        mockSqsPublisher = { publishThumbnailJob: jest.fn().mockResolvedValue(undefined) };

        const module: TestingModule = await Test.createTestingModule({
            providers: [
                ProcessingService,
                { provide: 'SQS_CLIENT', useValue: mockSQS },
                { provide: 'SNS_CLIENT', useValue: mockSNS },
                { provide: 'S3_CLIENT', useValue: mockS3 },
                { provide: 'DYNAMODB_CLIENT', useValue: mockDynamoDB },
                { provide: ExtractionService, useValue: mockExtraction },
                { provide: AiProcessingService, useValue: mockAi },
                { provide: SqsPublisherService, useValue: mockSqsPublisher },
                {
                    provide: ConfigService,
                    useValue: {
                        get: (key: string, defaultVal?: unknown) => {
                            const cfg: Record<string, unknown> = {
                                'aws.sqs.queueUrl': 'http://sqs/queue',
                                'aws.sns.topicArn': 'arn:aws:sns:us-east-1:000:topic',
                                'aws.dynamodb.tableName': 'test-table',
                                'aws.s3.bucket': 'test-bucket',
                                'aws.sqs.pollingInterval': 60_000, // long enough to not fire during tests
                                'aws.sqs.maxMessages': 5
                            };
                            return cfg[key] ?? defaultVal;
                        }
                    }
                }
            ]
        })
            .setLogger({
                log: () => {},
                error: () => {},
                warn: () => {},
                debug: () => {},
                verbose: () => {}
            })
            .compile();

        service = module.get(ProcessingService);
    });

    afterEach(() => {
        jest.useRealTimers();
        jest.clearAllMocks();
    });

    describe('processDocument()', () => {
        it('fetches from S3 and passes the S3 Content-Type (not job mimeType) to extraction', async () => {
            await service.processDocument(JOB);

            expect(mockS3.send).toHaveBeenCalledTimes(1);
            expect(mockExtraction.extractText).toHaveBeenCalledWith(
                expect.any(Buffer),
                'application/pdf'
            );
        });

        it('classifies then summarizes with the category returned by classify()', async () => {
            await service.processDocument(JOB);

            expect(mockAi.classify).toHaveBeenCalledWith({ documentId: 'doc-1', userId: 'user-1' });
            expect(mockAi.summarize).toHaveBeenCalledWith({
                documentId: 'doc-1',
                userId: 'user-1',
                category: 'invoice' // return value of classify()
            });
        });

        it('enqueues a thumbnail job right after extraction (before AI steps)', async () => {
            const callOrder: string[] = [];
            mockSqsPublisher.publishThumbnailJob.mockImplementation(() => {
                callOrder.push('thumbnail');
                return Promise.resolve();
            });
            mockAi.classify.mockImplementation(() => {
                callOrder.push('classify');
                return Promise.resolve('invoice');
            });

            await service.processDocument(JOB);

            expect(callOrder).toEqual(['thumbnail', 'classify']);
        });

        it('writes DynamoDB status transitions in order', async () => {
            await service.processDocument(JOB);

            const statuses = mockDynamoDB.send.mock.calls
                .map(([cmd]) => cmd)
                .filter((cmd) => cmd instanceof UpdateCommand)
                .map((cmd) => cmd.input.ExpressionAttributeValues?.[':status']);

            expect(statuses).toEqual([
                DocumentStatus.PROCESSING,
                DocumentStatus.EXTRACTING,
                DocumentStatus.CLASSIFYING,
                DocumentStatus.SUMMARIZING
            ]);
        });

        it('publishes an SNS event for each status transition', async () => {
            await service.processDocument(JOB);

            const events = mockSNS.send.mock.calls.map(([cmd]) => JSON.parse(cmd.input.Message));
            const statuses = events.map((e) => e.status);

            expect(statuses).toEqual([
                DocumentStatus.PROCESSING,
                DocumentStatus.EXTRACTING,
                DocumentStatus.CLASSIFYING,
                DocumentStatus.SUMMARIZING
            ]);
        });

        it('marks the document FAILED and rethrows when extraction throws', async () => {
            mockExtraction.extractText.mockRejectedValue(new Error('extraction crashed'));

            await expect(service.processDocument(JOB)).rejects.toThrow('extraction crashed');

            const failedUpdate = mockDynamoDB.send.mock.calls
                .map(([cmd]) => cmd)
                .filter((cmd) => cmd instanceof UpdateCommand)
                .map((cmd) => cmd.input.ExpressionAttributeValues)
                .find((v) => v[':status'] === DocumentStatus.FAILED);

            expect(failedUpdate).toBeDefined();
            expect(failedUpdate?.[':errorMessage']).toBe('extraction crashed');
        });

        it('publishes a FAILED SNS event when processing fails', async () => {
            mockAi.classify.mockRejectedValue(new Error('classify failed'));

            await expect(service.processDocument(JOB)).rejects.toThrow('classify failed');

            const events = mockSNS.send.mock.calls.map(([cmd]) => JSON.parse(cmd.input.Message));
            expect(events.at(-1)?.status).toBe(DocumentStatus.FAILED);
        });

        it('throws when S3 returns an empty body', async () => {
            mockS3.send.mockResolvedValue({ Body: undefined });

            await expect(service.processDocument(JOB)).rejects.toThrow(
                'Empty file body from S3: uploads/user-1/doc-1/file.pdf'
            );
        });

        it('does not call classify or summarize when S3 fetch fails', async () => {
            mockS3.send.mockRejectedValue(new Error('S3 unavailable'));

            await expect(service.processDocument(JOB)).rejects.toThrow('S3 unavailable');

            expect(mockAi.classify).not.toHaveBeenCalled();
            expect(mockAi.summarize).not.toHaveBeenCalled();
        });
    });
});
