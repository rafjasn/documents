import { Module } from '@nestjs/common';
import { AuthModule as SharedAuthModule } from '@documents/shared';
import { AuthController } from './auth.controller';
import { AuthService } from './auth.service';

@Module({
    imports: [SharedAuthModule],
    controllers: [AuthController],
    providers: [AuthService]
})
export class AuthModule {}
