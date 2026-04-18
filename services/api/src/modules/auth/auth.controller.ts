import { Controller, Post, Get, Body, Request, UseGuards } from '@nestjs/common';
import { ApiTags, ApiOperation, ApiBearerAuth } from '@nestjs/swagger';
import { plainToInstance } from 'class-transformer';
import { AuthService } from './auth.service';
import { LoginDto, RegisterDto, RefreshDto, AuthResponseDto, UserResponseDto } from './auth.dto';
import { JwtAuthGuard } from './jwt-auth.guard';
import { Request as ExpressRequest } from 'express';

interface AuthRequest extends ExpressRequest {
    user: { userId: string; email: string };
}

@ApiTags('auth')
@Controller('auth')
export class AuthController {
    constructor(private readonly authService: AuthService) {}

    @Post('register')
    @ApiOperation({ summary: 'Register a new user' })
    register(@Body() dto: RegisterDto): Promise<AuthResponseDto> {
        return this.authService.register(dto.email, dto.password);
    }

    @Post('login')
    @ApiOperation({ summary: 'Login with email and password' })
    login(@Body() dto: LoginDto): Promise<AuthResponseDto> {
        return this.authService.login(dto.email, dto.password);
    }

    @Post('refresh')
    @ApiOperation({ summary: 'Refresh access token' })
    refresh(@Body() dto: RefreshDto): Promise<AuthResponseDto> {
        return this.authService.refresh(dto.refreshToken);
    }

    @Get('me')
    @UseGuards(JwtAuthGuard)
    @ApiBearerAuth()
    @ApiOperation({ summary: 'Get current user' })
    me(@Request() req: AuthRequest): UserResponseDto {
        return plainToInstance(UserResponseDto, req.user, { excludeExtraneousValues: true });
    }
}
