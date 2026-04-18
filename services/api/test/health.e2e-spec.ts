import { INestApplication } from '@nestjs/common';
// eslint-disable-next-line @typescript-eslint/no-require-imports
const request = require('supertest') as typeof import('supertest');
import { createE2eApp } from './helpers/app';

describe('Health (e2e)', () => {
    let app: INestApplication;

    beforeAll(async () => {
        ({ app } = await createE2eApp());
    });

    describe('GET /api/auth/register', () => {
        it('returns 200 with status, timestamp, and services on success', async () => {
            const res = await request(app.getHttpServer()).get('/api/health').expect(200);

            expect(res.body).toMatchObject({
                status: 'healthy',
                timestamp: expect.any(String),
                services: {
                    s3: { status: 'healthy', latency: expect.any(Number) },
                    sqs: { status: 'healthy', latency: expect.any(Number) }
                }
            });
        });
    });
});
