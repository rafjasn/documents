import { Test, TestingModule } from '@nestjs/testing';
import { ConfigService } from '@nestjs/config';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { GetCommand, UpdateCommand } from '@aws-sdk/lib-dynamodb';
import { ThumbnailService } from './thumbnail.service';
import { DocumentStatus } from '@documents/shared';

jest.mock('sharp', () => {
    const instance = {
        resize: jest.fn().mockReturnThis(),
        png: jest.fn().mockReturnThis(),
        toBuffer: jest.fn().mockResolvedValue(Buffer.from('fake-thumbnail-png'))
    };
    const fn = Object.assign(jest.fn().mockReturnValue(instance), { __instance: instance });
    return { default: fn, __esModule: true };
});

const mockSharp = (jest.requireMock('sharp') as any).default as jest.Mock;

const sharpInstance = (mockSharp as any).__instance as {
    resize: jest.Mock;
    png: jest.Mock;
    toBuffer: jest.Mock;
};

async function* bodyStream(data: Buffer): AsyncIterable<Uint8Array> {
    yield data;
}

const USER_ID = 'user-1';
const DOC_ID = 'doc-1';
const S3_KEY = 'uploads/user-1/doc-1/file.pdf';
const THUMB_KEY = `thumbnails/${USER_ID}/${DOC_ID}.png`;

describe('ThumbnailService', () => {
    let service: ThumbnailService;
    let mockS3: { send: jest.Mock };
    let mockDynamoDB: { send: jest.Mock };
    let mockSNS: { send: jest.Mock };

    beforeEach(async () => {
        mockS3 = {
            send: jest.fn().mockImplementation((cmd: unknown) => {
                if (cmd instanceof GetObjectCommand) {
                    return Promise.resolve({ Body: bodyStream(Buffer.from('raw file')) });
                }
                return Promise.resolve({}); // PutObjectCommand
            })
        };

        mockDynamoDB = {
            send: jest.fn().mockImplementation((cmd: unknown) => {
                if (cmd instanceof GetCommand) {
                    return Promise.resolve({ Item: { status: DocumentStatus.COMPLETED } });
                }
                return Promise.resolve({}); // UpdateCommand
            })
        };

        mockSNS = { send: jest.fn().mockResolvedValue({}) };

        const module: TestingModule = await Test.createTestingModule({
            providers: [
                ThumbnailService,
                { provide: 'S3_CLIENT', useValue: mockS3 },
                { provide: 'DYNAMODB_CLIENT', useValue: mockDynamoDB },
                { provide: 'SNS_CLIENT', useValue: mockSNS },
                {
                    provide: ConfigService,
                    useValue: {
                        get: (key: string) => {
                            if (key === 'aws.s3.bucket') return 'test-bucket';
                            if (key === 'aws.dynamodb.tableName') return 'test-table';
                            if (key === 'aws.sns.topicArn')
                                return 'arn:aws:sns:us-east-1:000:topic';
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

        service = module.get(ThumbnailService);
        jest.clearAllMocks();
        mockSharp.mockReturnValue(sharpInstance);
    });

    afterEach(() => jest.clearAllMocks());

    describe('S3 upload', () => {
        it('uploads the thumbnail to thumbnails/<userId>/<docId>.png', async () => {
            await service.process(USER_ID, DOC_ID, S3_KEY, 'application/pdf');

            const putCall = mockS3.send.mock.calls
                .map(([cmd]) => cmd)
                .find((cmd) => !(cmd instanceof GetObjectCommand));

            expect(putCall?.input).toMatchObject({
                Bucket: 'test-bucket',
                Key: THUMB_KEY,
                ContentType: 'image/png'
            });
        });

        it('uses the placeholder pipeline for non-image mimeTypes', async () => {
            await service.process(USER_ID, DOC_ID, S3_KEY, 'application/pdf');

            expect(mockSharp).toHaveBeenCalledTimes(1);
            const arg = mockSharp.mock.calls[0][0] as Buffer;
            expect(arg.toString()).toContain('<svg');
        });

        it('uses the image resize pipeline for image/* mimeTypes', async () => {
            await service.process(USER_ID, DOC_ID, S3_KEY, 'image/png');

            expect(mockSharp).toHaveBeenCalledWith(expect.any(Buffer));
            expect(sharpInstance.resize).toHaveBeenCalledWith(200, 260, expect.any(Object));
        });
    });

    describe('DynamoDB update', () => {
        it('writes thumbnailKey to the correct table and item key', async () => {
            await service.process(USER_ID, DOC_ID, S3_KEY, 'application/pdf');

            const updateCmd = mockDynamoDB.send.mock.calls
                .map(([cmd]) => cmd)
                .find((cmd) => cmd instanceof UpdateCommand);

            expect(updateCmd?.input).toMatchObject({
                TableName: 'test-table',
                Key: { PK: `USER#${USER_ID}`, SK: `DOC#${DOC_ID}` }
            });
            expect(updateCmd?.input.ExpressionAttributeValues?.[':key']).toBe(THUMB_KEY);
        });
    });

    describe('SNS event', () => {
        it('publishes an event with the thumbnailKey and the current document status', async () => {
            await service.process(USER_ID, DOC_ID, S3_KEY, 'application/pdf');

            expect(mockSNS.send).toHaveBeenCalledTimes(1);
            const event = JSON.parse(mockSNS.send.mock.calls[0][0].input.Message);

            expect(event.documentId).toBe(DOC_ID);
            expect(event.status).toBe(DocumentStatus.COMPLETED); // read back from DDB mock
            expect(event.document.thumbnailKey).toBe(THUMB_KEY);
        });

        it('falls back to PROCESSING status when DDB item has no status field', async () => {
            mockDynamoDB.send.mockImplementation((cmd: unknown) => {
                if (cmd instanceof GetCommand) return Promise.resolve({ Item: {} });
                return Promise.resolve({});
            });

            await service.process(USER_ID, DOC_ID, S3_KEY, 'application/pdf');

            const event = JSON.parse(mockSNS.send.mock.calls[0][0].input.Message);
            expect(event.status).toBe(DocumentStatus.PROCESSING);
        });

        it('does not throw when SNS publish fails', async () => {
            mockSNS.send.mockRejectedValue(new Error('SNS unavailable'));

            await expect(
                service.process(USER_ID, DOC_ID, S3_KEY, 'application/pdf')
            ).resolves.not.toThrow();
        });
    });

    describe('placeholder label', () => {
        it.each([
            ['application/pdf', 'PDF'],
            ['application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'DOCX'],
            ['text/plain', 'TXT'],
            ['text/csv', 'CSV'],
            ['application/octet-stream', 'FILE']
        ])('uses correct label for %s', async (mimeType, expectedLabel) => {
            await service.process(USER_ID, DOC_ID, S3_KEY, mimeType);

            const svgArg = (mockSharp.mock.calls[0][0] as Buffer).toString();
            expect(svgArg).toContain(expectedLabel);
        });
    });
});
