import { Injectable, UnauthorizedException, BadRequestException } from '@nestjs/common';
import { AuthProvider, AuthTokens } from '../auth-provider.interface';

@Injectable()
export class KeycloakProvider implements AuthProvider {
    private readonly url = process.env.KEYCLOAK_URL!;
    private readonly realm = process.env.KEYCLOAK_REALM!;
    private readonly clientId = process.env.KEYCLOAK_CLIENT_ID!;

    async register(email: string, password: string): Promise<string> {
        const adminToken = await this.getAdminToken();
        const response = await fetch(`${this.url}/admin/realms/${this.realm}/users`, {
            method: 'POST',
            headers: {
                Authorization: `Bearer ${adminToken}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                email,
                username: email,
                enabled: true,
                emailVerified: true,
                requiredActions: [],
                credentials: [{ type: 'password', value: password, temporary: false }]
            })
        });

        if (!response.ok) {
            const body = await response.text();
            console.error(`Keycloak register failed: ${response.status} ${body}`);
            let parsed: { errorMessage?: string; error?: string } = {};

            try {
                parsed = JSON.parse(body);
            } catch {}

            throw new BadRequestException(
                parsed.errorMessage ?? parsed.error ?? `Registration failed (${response.status})`
            );
        }

        const location = response.headers.get('location');

        return location!.split('/').pop()!;
    }

    async login(email: string, password: string): Promise<AuthTokens> {
        return this.getToken(email, password);
    }

    async refresh(refreshToken: string): Promise<AuthTokens> {
        const response = await fetch(
            `${this.url}/realms/${this.realm}/protocol/openid-connect/token`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({
                    client_id: this.clientId,
                    grant_type: 'refresh_token',
                    refresh_token: refreshToken
                })
            }
        );

        if (!response.ok) {
            throw new UnauthorizedException('Invalid refresh token');
        }

        return response.json();
    }

    private async getToken(email: string, password: string): Promise<AuthTokens> {
        const response = await fetch(
            `${this.url}/realms/${this.realm}/protocol/openid-connect/token`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({
                    client_id: this.clientId,
                    username: email,
                    password,
                    grant_type: 'password'
                })
            }
        );

        if (!response.ok) throw new UnauthorizedException('Invalid credentials');
        return response.json();
    }

    private async getAdminToken(): Promise<string> {
        const clientId = process.env.KEYCLOAK_ADMIN_CLIENT_ID;
        const clientSecret = process.env.KEYCLOAK_ADMIN_CLIENT_SECRET;

        if (!clientId || !clientSecret) {
            throw new Error(
                'KEYCLOAK_ADMIN_CLIENT_ID and KEYCLOAK_ADMIN_CLIENT_SECRET are required'
            );
        }

        const response = await fetch(
            `${this.url}/realms/${this.realm}/protocol/openid-connect/token`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({
                    grant_type: 'client_credentials',
                    client_id: clientId,
                    client_secret: clientSecret
                })
            }
        );

        if (!response.ok) {
            const error = await response.text();
            throw new Error(`Failed to get admin token: ${error}`);
        }

        const data = await response.json();
        return data.access_token;
    }
}
