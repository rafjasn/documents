import { Injectable, Inject } from '@nestjs/common';
import { DynamoDBDocumentClient, PutCommand, UpdateCommand } from '@aws-sdk/lib-dynamodb';
import type { ConfigType } from '@nestjs/config';
import { plainToInstance } from 'class-transformer';
import { AUTH_PROVIDER } from '@documents/shared';
import type { AuthProvider } from '@documents/shared';
import { awsConfig } from '@config/index';
import { AuthResponseDto } from './auth.dto';

interface JwtPayload {
    sub: string;
    email?: string;
}

@Injectable()
export class AuthService {
    constructor(
        @Inject('DYNAMODB_CLIENT')
        private readonly dynamodb: DynamoDBDocumentClient,
        @Inject(awsConfig.KEY)
        private readonly aws: ConfigType<typeof awsConfig>,
        @Inject(AUTH_PROVIDER)
        private readonly authProvider: AuthProvider
    ) {}

    async register(email: string, password: string): Promise<AuthResponseDto> {
        const providerId = await this.authProvider.register(email, password);

        const now = new Date().toISOString();

        await this.dynamodb.send(
            new PutCommand({
                TableName: this.aws.dynamodb.tableName,
                Item: {
                    PK: `USER#${providerId}`,
                    SK: 'PROFILE',
                    GSI1PK: `EMAIL#${email}`,
                    GSI1SK: `USER#${providerId}`,
                    userId: providerId,
                    email,
                    createdAt: now,
                    updatedAt: now
                },
                ConditionExpression: 'attribute_not_exists(PK)'
            })
        );

        const token = await this.authProvider.login(email, password);

        return this.toAuthResponse(token);
    }

    async login(email: string, password: string): Promise<AuthResponseDto> {
        const token = await this.authProvider.login(email, password);

        return this.toAuthResponse(token);
    }

    async refresh(refreshToken: string): Promise<AuthResponseDto> {
        const token = await this.authProvider.refresh(refreshToken);

        return this.toAuthResponse(token);
    }

    private toAuthResponse(token: {
        access_token: string;
        refresh_token?: string;
    }): AuthResponseDto {
        const payload = JSON.parse(
            Buffer.from(token.access_token.split('.')[1], 'base64').toString()
        ) as JwtPayload;

        const providerId = payload.sub;
        const email = payload.email ?? payload['preferred_username'] ?? '';
        this.dynamodb.send(
            new UpdateCommand({
                TableName: this.aws.dynamodb.tableName,
                Key: { PK: `USER#${providerId}`, SK: 'PROFILE' },
                UpdateExpression:
                    'SET email = :e, GSI1PK = :g1pk, GSI1SK = :g1sk, updatedAt = :now',
                ExpressionAttributeValues: {
                    ':e': email,
                    ':g1pk': `EMAIL#${email}`,
                    ':g1sk': `USER#${providerId}`,
                    ':now': new Date().toISOString()
                }
            })
        );

        return plainToInstance(
            AuthResponseDto,
            {
                accessToken: token.access_token,
                refreshToken: token.refresh_token,
                userId: providerId,
                email
            },
            { excludeExtraneousValues: true }
        );
    }
}
