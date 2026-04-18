import { Provider } from '@nestjs/common';
import { KeycloakProvider } from './keycloak.provider';
import { CognitoProvider } from './cognito.provider';
import { AuthProvider } from '../auth-provider.interface';

export const AUTH_PROVIDER = 'AUTH_PROVIDER_TOKEN';

export const AuthProviderFactory: Provider = {
    provide: AUTH_PROVIDER,
    useFactory: (): AuthProvider => {
        const provider = process.env.AUTH_PROVIDER ?? 'keycloak';
        return provider === 'cognito' ? new CognitoProvider() : new KeycloakProvider();
    }
};
