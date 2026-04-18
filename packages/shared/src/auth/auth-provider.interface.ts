export interface AuthProvider {
    register(email: string, password: string): Promise<string>;
    login(email: string, password: string): Promise<AuthTokens>;
    refresh(refreshToken: string): Promise<AuthTokens>;
}

export interface AuthTokens {
    access_token: string;
    refresh_token: string;
    expires_in: number;
}
