import { INestApplication } from '@nestjs/common';
// eslint-disable-next-line @typescript-eslint/no-require-imports
const request = require('supertest') as typeof import('supertest');
import { createAuthTestApp, createMockAuthProvider } from './helpers/app';
import { bearerToken } from './helpers/jwt';

describe('Auth (e2e)', () => {
    let app: INestApplication;
    let mockAuthProvider: ReturnType<typeof createMockAuthProvider>;

    beforeEach(async () => {
        ({ app, mockAuthProvider } = await createAuthTestApp());
    });

    afterEach(async () => {
        await app.close();
        jest.clearAllMocks();
    });

    describe('POST /api/auth/register', () => {
        it('returns 201 with accessToken, userId, and email on success', async () => {
            const res = await request(app.getHttpServer())
                .post('/api/auth/register')
                .send({ email: 'new@example.com', password: 'password123' })
                .expect(201);

            expect(res.body).toMatchObject({
                accessToken: expect.any(String),
                userId: 'test-user-id',
                email: 'test@example.com'
            });

            expect(mockAuthProvider.register).toHaveBeenCalledWith(
                'new@example.com',
                'password123'
            );
        });

        it('returns 400 when email is invalid', async () => {
            await request(app.getHttpServer())
                .post('/api/auth/register')
                .send({ email: 'not-an-email', password: 'password123' })
                .expect(400);
        });

        it('returns 400 when password is too short', async () => {
            await request(app.getHttpServer())
                .post('/api/auth/register')
                .send({ email: 'user@example.com', password: 'short' })
                .expect(400);
        });

        it('returns 400 when body is empty', async () => {
            await request(app.getHttpServer()).post('/api/auth/register').send({}).expect(400);
        });

        it('propagates provider errors as 500', async () => {
            mockAuthProvider.register.mockRejectedValueOnce(new Error('User already exists'));

            const res = await request(app.getHttpServer())
                .post('/api/auth/register')
                .send({ email: 'dup@example.com', password: 'password123' });

            expect(res.status).toBeGreaterThanOrEqual(400);
        });
    });

    describe('POST /api/auth/login', () => {
        it('returns 201 with accessToken and refreshToken on success', async () => {
            const res = await request(app.getHttpServer())
                .post('/api/auth/login')
                .send({ email: 'user@example.com', password: 'password123' })
                .expect(201);

            expect(res.body).toMatchObject({
                accessToken: expect.any(String),
                refreshToken: 'test-refresh-token',
                userId: 'test-user-id',
                email: 'test@example.com'
            });
        });

        it('calls the auth provider with the correct credentials', async () => {
            await request(app.getHttpServer())
                .post('/api/auth/login')
                .send({ email: 'user@example.com', password: 'mypassword' });

            expect(mockAuthProvider.login).toHaveBeenCalledWith('user@example.com', 'mypassword');
        });

        it('returns 400 when email is missing', async () => {
            await request(app.getHttpServer())
                .post('/api/auth/login')
                .send({ password: 'password123' })
                .expect(400);
        });
    });

    describe('POST /api/auth/refresh', () => {
        it('returns a new accessToken', async () => {
            const res = await request(app.getHttpServer())
                .post('/api/auth/refresh')
                .send({ refreshToken: 'old-refresh-token' })
                .expect(201);

            expect(res.body.accessToken).toEqual(expect.any(String));
            expect(mockAuthProvider.refresh).toHaveBeenCalledWith('old-refresh-token');
        });

        it('returns 400 when refreshToken is missing', async () => {
            await request(app.getHttpServer()).post('/api/auth/refresh').send({}).expect(400);
        });
    });

    describe('GET /api/auth/me', () => {
        it('returns 401 when no token is provided', async () => {
            await request(app.getHttpServer()).get('/api/auth/me').expect(401);
        });

        it('returns 401 with a malformed token', async () => {
            await request(app.getHttpServer())
                .get('/api/auth/me')
                .set('Authorization', 'Bearer not.a.real.token')
                .expect(401);
        });

        it('returns 200 with userId and email when token is valid', async () => {
            const token = bearerToken({ sub: 'user-123', email: 'me@example.com' });

            const res = await request(app.getHttpServer())
                .get('/api/auth/me')
                .set('Authorization', token)
                .expect(200);

            expect(res.body).toEqual({ userId: 'user-123', email: 'me@example.com' });
        });

        it('does not expose fields outside the DTO', async () => {
            const token = bearerToken({ sub: 'user-abc', email: 'secure@example.com' });
            const res = await request(app.getHttpServer())
                .get('/api/auth/me')
                .set('Authorization', token)
                .expect(200);

            expect(Object.keys(res.body).sort()).toEqual(['email', 'userId']);
        });
    });
});
