import { Injectable, UnauthorizedException, BadRequestException } from '@nestjs/common';
import {
    CognitoIdentityProviderClient,
    SignUpCommand,
    InitiateAuthCommand,
    AuthFlowType
} from '@aws-sdk/client-cognito-identity-provider';
import { AuthProvider, AuthTokens } from '../auth-provider.interface';

@Injectable()
export class CognitoProvider implements AuthProvider {
    private readonly client = new CognitoIdentityProviderClient({
        region: process.env.AWS_REGION!
    });

    private readonly clientId = process.env.COGNITO_CLIENT_ID!;

    async register(email: string, password: string): Promise<string> {
        try {
            const response = await this.client.send(
                new SignUpCommand({
                    ClientId: this.clientId,
                    Username: email,
                    Password: password,
                    UserAttributes: [{ Name: 'email', Value: email }]
                })
            );

            return response.UserSub!;
        } catch (error: any) {
            throw new BadRequestException(error.message ?? 'Registration failed');
        }
    }

    async login(email: string, password: string): Promise<AuthTokens> {
        try {
            const response = await this.client.send(
                new InitiateAuthCommand({
                    AuthFlow: AuthFlowType.USER_PASSWORD_AUTH,
                    ClientId: this.clientId,
                    AuthParameters: {
                        USERNAME: email,
                        PASSWORD: password
                    }
                })
            );

            return {
                access_token: response.AuthenticationResult!.AccessToken!,
                refresh_token: response.AuthenticationResult!.RefreshToken!,
                expires_in: response.AuthenticationResult!.ExpiresIn!
            };
        } catch (error: any) {
            throw new UnauthorizedException('Invalid credentials');
        }
    }

    async refresh(refreshToken: string): Promise<AuthTokens> {
        try {
            const response = await this.client.send(
                new InitiateAuthCommand({
                    AuthFlow: AuthFlowType.REFRESH_TOKEN_AUTH,
                    ClientId: this.clientId,
                    AuthParameters: {
                        REFRESH_TOKEN: refreshToken
                    }
                })
            );

            return {
                access_token: response.AuthenticationResult!.AccessToken!,
                refresh_token: refreshToken, // Cognito doesn't rotate refresh tokens
                expires_in: response.AuthenticationResult!.ExpiresIn!
            };
        } catch (error: any) {
            throw new UnauthorizedException('Invalid refresh token');
        }
    }
}
