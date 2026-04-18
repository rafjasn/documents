import { Module } from '@nestjs/common';
import { PassportModule } from '@nestjs/passport';
import { JwtStrategy } from './jwt.strategy';
import { AuthProviderFactory, AUTH_PROVIDER } from './providers/auth-provider.factory';

@Module({
    imports: [PassportModule.register({ defaultStrategy: 'jwt' })],
    providers: [JwtStrategy, AuthProviderFactory],
    exports: [PassportModule, JwtStrategy, AUTH_PROVIDER]
})
export class AuthModule {}
