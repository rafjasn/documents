import { INestApplication } from '@nestjs/common';
import { DynamoDBDocumentClient, DeleteCommand, QueryCommand } from '@aws-sdk/lib-dynamodb';
import { createDynamoDBClient } from '@documents/shared';
import { createE2eApp } from './helpers/app';
import { bearerToken } from './helpers/jwt';
// eslint-disable-next-line @typescript-eslint/no-require-imports
const request = require('supertest') as typeof import('supertest');

const LOCALSTACK = 'http://localhost:4566';
const TABLE = 'documents-documents';

const AWS_OPTS = {
    region: 'us-east-1',
    endpoint: LOCALSTACK,
    credentials: { accessKeyId: 'test', secretAccessKey: 'test' }
};

const NONEXISTENT_UUID = '00000000-0000-4000-8000-000000000000';

async function deleteAllDocsForUser(
    dynamodb: DynamoDBDocumentClient,
    userId: string
): Promise<void> {
    const result = await dynamodb.send(
        new QueryCommand({
            TableName: TABLE,
            KeyConditionExpression: 'PK = :pk AND begins_with(SK, :prefix)',
            ExpressionAttributeValues: {
                ':pk': `USER#${userId}`,
                ':prefix': 'DOC#'
            }
        })
    );

    for (const item of result.Items ?? []) {
        await dynamodb
            .send(new DeleteCommand({ TableName: TABLE, Key: { PK: item.PK, SK: item.SK } }))
            .catch(() => {});
    }
}

describe('Documents API (e2e, requires LocalStack)', () => {
    let app: INestApplication;
    let dynamodb: DynamoDBDocumentClient;

    const USER_A = { sub: 'user-a', email: 'usera@example.com' };
    const USER_B = { sub: 'user-b', email: 'userb@example.com' };
    const tokenA = bearerToken(USER_A);
    const tokenB = bearerToken(USER_B);

    beforeAll(async () => {
        dynamodb = createDynamoDBClient(AWS_OPTS);
        ({ app } = await createE2eApp());
    });

    afterAll(async () => {
        await deleteAllDocsForUser(dynamodb, USER_A.sub);
        await deleteAllDocsForUser(dynamodb, USER_B.sub);
        await app.close();
    });

    afterEach(async () => {
        await deleteAllDocsForUser(dynamodb, USER_A.sub);
        await deleteAllDocsForUser(dynamodb, USER_B.sub);
    });

    describe('auth guard', () => {
        it('rejects requests to /api/documents without a token', async () => {
            await request(app.getHttpServer()).get('/api/documents').expect(401);
        });

        it('rejects requests to /api/documents/presign without a token', async () => {
            await request(app.getHttpServer()).post('/api/documents/presign').expect(401);
        });
    });

    describe('POST /api/documents/presign', () => {
        it('returns 201 with documentId, S3 uploadUrl and form fields', async () => {
            const res = await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenA)
                .send({ fileName: 'test.pdf', mimeType: 'application/pdf', fileSize: 1024 })
                .expect(201);

            expect(res.body).toMatchObject({
                documentId: expect.any(String),
                uploadUrl: expect.any(String),
                fields: expect.objectContaining({
                    key: expect.stringContaining('test.pdf'),
                    'Content-Type': 'application/pdf'
                })
            });
        });

        it('returns 400 for an unsupported MIME type', async () => {
            await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenA)
                .send({
                    fileName: 'virus.exe',
                    mimeType: 'application/x-msdownload',
                    fileSize: 512
                })
                .expect(400);
        });

        it('returns 400 when fileSize exceeds the configured maximum', async () => {
            const over50mb = 51 * 1024 * 1024;
            await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenA)
                .send({ fileName: 'big.pdf', mimeType: 'application/pdf', fileSize: over50mb })
                .expect(400);
        });

        it('returns 400 when required fields are missing', async () => {
            await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenA)
                .send({ fileName: 'test.pdf' }) // missing mimeType and fileSize
                .expect(400);
        });

        it('creates a PENDING DynamoDB record', async () => {
            const res = await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenA)
                .send({ fileName: 'doc.txt', mimeType: 'text/plain', fileSize: 200 })
                .expect(201);

            const { documentId } = res.body as { documentId: string };

            const getRes = await request(app.getHttpServer())
                .get(`/api/documents/${documentId}`)
                .set('Authorization', tokenA)
                .expect(200);

            expect(getRes.body.status).toBe('PENDING');
        });
    });

    describe('GET /api/documents', () => {
        it('returns an empty list when the user has no documents', async () => {
            const res = await request(app.getHttpServer())
                .get('/api/documents')
                .set('Authorization', tokenA)
                .expect(200);

            expect(res.body.items).toEqual([]);
        });

        it("lists only the authenticated user's documents", async () => {
            const presignA = await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenA)
                .send({ fileName: 'a.pdf', mimeType: 'application/pdf', fileSize: 512 });

            await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenB)
                .send({ fileName: 'b.pdf', mimeType: 'application/pdf', fileSize: 512 });

            const resA = await request(app.getHttpServer())
                .get('/api/documents')
                .set('Authorization', tokenA)
                .expect(200);

            expect(resA.body.items).toHaveLength(1);
            expect(resA.body.items[0].id).toBe(presignA.body.documentId);
        });

        it('respects the limit query parameter', async () => {
            for (let i = 0; i < 3; i++) {
                await request(app.getHttpServer())
                    .post('/api/documents/presign')
                    .set('Authorization', tokenA)
                    .send({ fileName: `file${i}.txt`, mimeType: 'text/plain', fileSize: 100 });
            }

            const res = await request(app.getHttpServer())
                .get('/api/documents?limit=2')
                .set('Authorization', tokenA)
                .expect(200);

            expect(res.body.items).toHaveLength(2);
        });
    });

    describe('GET /api/documents/:id', () => {
        it('returns 200 with document fields', async () => {
            const presign = await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenA)
                .send({ fileName: 'report.pdf', mimeType: 'application/pdf', fileSize: 4096 });
            const { documentId } = presign.body as { documentId: string };

            const res = await request(app.getHttpServer())
                .get(`/api/documents/${documentId}`)
                .set('Authorization', tokenA)
                .expect(200);

            expect(res.body).toMatchObject({
                id: documentId,
                fileName: 'report.pdf',
                mimeType: 'application/pdf',
                status: 'PENDING'
            });
        });

        it('returns 404 for a non-existent document', async () => {
            await request(app.getHttpServer())
                .get(`/api/documents/${NONEXISTENT_UUID}`)
                .set('Authorization', tokenA)
                .expect(404);
        });

        it("returns 404 when user B requests user A's document", async () => {
            const presign = await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenA)
                .send({ fileName: 'private.pdf', mimeType: 'application/pdf', fileSize: 512 });
            const { documentId } = presign.body as { documentId: string };

            await request(app.getHttpServer())
                .get(`/api/documents/${documentId}`)
                .set('Authorization', tokenB)
                .expect(404);
        });

        it('returns 400 for an invalid UUID format', async () => {
            await request(app.getHttpServer())
                .get('/api/documents/not-a-uuid')
                .set('Authorization', tokenA)
                .expect(400);
        });
    });

    describe('GET /api/documents/:id/download', () => {
        it('returns a presigned downloadUrl', async () => {
            const presign = await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenA)
                .send({ fileName: 'dl.pdf', mimeType: 'application/pdf', fileSize: 1024 });
            const { documentId } = presign.body as { documentId: string };

            const res = await request(app.getHttpServer())
                .get(`/api/documents/${documentId}/download`)
                .set('Authorization', tokenA)
                .expect(200);

            expect(res.body.downloadUrl).toMatch(/^http/);
            expect(res.body.downloadUrl).toContain('dl.pdf');
        });

        it("returns 404 when requesting download URL for another user's document", async () => {
            const presign = await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenA)
                .send({ fileName: 'restricted.pdf', mimeType: 'application/pdf', fileSize: 512 });
            const { documentId } = presign.body as { documentId: string };

            await request(app.getHttpServer())
                .get(`/api/documents/${documentId}/download`)
                .set('Authorization', tokenB)
                .expect(404);
        });
    });

    describe('DELETE /api/documents/:id', () => {
        it('returns 204 and soft-deletes the document (status becomes DELETED)', async () => {
            const presign = await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenA)
                .send({ fileName: 'to-delete.pdf', mimeType: 'application/pdf', fileSize: 512 });
            const { documentId } = presign.body as { documentId: string };

            await request(app.getHttpServer())
                .delete(`/api/documents/${documentId}`)
                .set('Authorization', tokenA)
                .expect(204);

            const res = await request(app.getHttpServer())
                .get(`/api/documents/${documentId}`)
                .set('Authorization', tokenA)
                .expect(200);

            expect(res.body.status).toBe('DELETED');
        });

        it('returns 404 for a non-existent document', async () => {
            await request(app.getHttpServer())
                .delete(`/api/documents/${NONEXISTENT_UUID}`)
                .set('Authorization', tokenA)
                .expect(404);
        });

        it("returns 404 when user B tries to delete user A's document", async () => {
            const presign = await request(app.getHttpServer())
                .post('/api/documents/presign')
                .set('Authorization', tokenA)
                .send({ fileName: 'protected.pdf', mimeType: 'application/pdf', fileSize: 512 });
            const { documentId } = presign.body as { documentId: string };

            await request(app.getHttpServer())
                .delete(`/api/documents/${documentId}`)
                .set('Authorization', tokenB)
                .expect(404);

            await request(app.getHttpServer())
                .get(`/api/documents/${documentId}`)
                .set('Authorization', tokenA)
                .expect(200);
        });
    });
});
