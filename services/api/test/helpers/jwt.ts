import { JwtService } from '@nestjs/jwt';

export const TEST_JWT_SECRET = 'e2e-test-secret-not-for-production';

const jwtService = new JwtService({
    secret: TEST_JWT_SECRET,
    signOptions: { expiresIn: '1h' }
});

export interface TestTokenPayload {
    sub: string;
    email: string;
}

export function signTestToken(payload: TestTokenPayload): string {
    return jwtService.sign(payload);
}

export function bearerToken(payload: TestTokenPayload): string {
    return `Bearer ${signTestToken(payload)}`;
}
