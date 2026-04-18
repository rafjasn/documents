import { IsEmail, IsString, MinLength } from 'class-validator';
import { Exclude, Expose } from 'class-transformer';
import { ApiProperty, ApiPropertyOptional } from '@nestjs/swagger';

export class RegisterDto {
    @ApiProperty({ example: 'user@example.com' })
    @IsEmail()
    email!: string;

    @ApiProperty({ example: 'securepassword123', minLength: 8 })
    @IsString()
    @MinLength(8)
    password!: string;
}

export class LoginDto {
    @ApiProperty({ example: 'user@example.com' })
    @IsEmail()
    email!: string;

    @ApiProperty({ example: 'securepassword123' })
    @IsString()
    password!: string;
}

export class RefreshDto {
    @ApiProperty()
    @IsString()
    refreshToken!: string;
}

@Exclude()
export class AuthResponseDto {
    @Expose()
    @ApiProperty()
    accessToken!: string;

    @Expose()
    @ApiPropertyOptional()
    refreshToken?: string;

    @Expose()
    @ApiProperty()
    userId!: string;

    @Expose()
    @ApiProperty()
    email!: string;
}

@Exclude()
export class UserResponseDto {
    @Expose()
    @ApiProperty()
    userId!: string;

    @Expose()
    @ApiProperty()
    email!: string;
}
