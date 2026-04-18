import { Injectable } from '@nestjs/common';
import { PassportStrategy } from '@nestjs/passport';
import { ExtractJwt, Strategy } from 'passport-jwt';
import { passportJwtSecret } from 'jwks-rsa';

@Injectable()
export class JwtStrategy extends PassportStrategy(Strategy) {
    constructor() {
        if (!process.env.AUTH_AUDIENCE) {
            throw new Error(
                'AUTH_AUDIENCE env var is required — without it, any token ' +
                    'minted for any client in the same issuer/realm is accepted.'
            );
        }

        super({
            jwtFromRequest: ExtractJwt.fromAuthHeaderAsBearerToken(),
            ignoreExpiration: false,
            secretOrKeyProvider: passportJwtSecret({
                cache: true,
                rateLimit: true,
                jwksRequestsPerMinute: 5,
                jwksUri: process.env.AUTH_JWKS_URI!
            }),
            issuer: process.env.AUTH_ISSUER,
            audience: process.env.AUTH_AUDIENCE,
            algorithms: ['RS256']
        });
    }

    validate(payload: any) {
        return {
            userId: payload.sub,
            email: payload.email ?? payload.preferred_username
        };
    }
}
