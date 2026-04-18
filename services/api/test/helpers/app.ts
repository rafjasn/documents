import {
    INestApplication,
    Injectable,
    ValidationPipe,
    ClassSerializerInterceptor
} from '@nestjs/common';
import { Reflector } from '@nestjs/core';
import { Test, TestingModule } from '@nestjs/testing';
import { PassportStrategy } from '@nestjs/passport';
import { ExtractJwt, Strategy } from 'passport-jwt';
import { AppModule } from '../../src/app.module';
import { JwtStrategy } from '@documents/shared';
import { AUTH_PROVIDER } from '@documents/shared';
import { NotificationsConsumerService } from '../../src/modules/gateway/notifications-consumer.service';
import { GlobalExceptionFilter } from '../../src/common/filters/global-exception.filter';
import { LoggingInterceptor } from '../../src/common/interceptors/logging.interceptor';
import { TEST_JWT_SECRET, signTestToken } from './jwt';

@Injectable()
class TestJwtStrategy extends PassportStrategy(Strategy, 'jwt') {
    constructor() {
        super({
            jwtFromRequest: ExtractJwt.fromAuthHeaderAsBearerToken(),
            ignoreExpiration: false,
            secretOrKey: TEST_JWT_SECRET
        });
    }

    async validate(payload: { sub: string; email: string }) {
        return { userId: payload.sub, email: payload.email };
    }
}

export function createMockAuthProvider(userId = 'test-user-id', email = 'test@example.com') {
    return {
        register: jest.fn().mockResolvedValue(userId),
        login: jest.fn().mockResolvedValue({
            access_token: signTestToken({ sub: userId, email }),
            refresh_token: 'test-refresh-token'
        }),
        refresh: jest.fn().mockResolvedValue({
            access_token: signTestToken({ sub: userId, email }),
            refresh_token: 'test-refresh-token-new'
        })
    };
}

function applyGlobalSetup(app: INestApplication) {
    app.setGlobalPrefix('api');
    app.useGlobalPipes(
        new ValidationPipe({
            whitelist: true,
            forbidNonWhitelisted: true,
            transform: true,
            transformOptions: { enableImplicitConversion: true }
        })
    );
    app.useGlobalFilters(new GlobalExceptionFilter());
    app.useGlobalInterceptors(
        new LoggingInterceptor(),
        new ClassSerializerInterceptor(app.get(Reflector))
    );
}

const noopLogger = {
    log: () => {},
    error: () => {},
    warn: () => {},
    debug: () => {},
    verbose: () => {}
};

export interface AuthTestApp {
    app: INestApplication;
    module: TestingModule;
    mockAuthProvider: ReturnType<typeof createMockAuthProvider>;
    mockDynamoDB: { send: (...args: any[]) => any };
}

export async function createAuthTestApp(
    overrides?: Partial<Pick<AuthTestApp, 'mockAuthProvider'>>
): Promise<AuthTestApp> {
    const mockAuthProvider = overrides?.mockAuthProvider ?? createMockAuthProvider();
    const mockDynamoDB = { send: jest.fn().mockResolvedValue({}) };

    const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
        .setLogger(noopLogger)
        .overrideProvider(JwtStrategy)
        .useClass(TestJwtStrategy)
        .overrideProvider(AUTH_PROVIDER)
        .useValue(mockAuthProvider)
        .overrideProvider('DYNAMODB_CLIENT')
        .useValue(mockDynamoDB)
        .overrideProvider(NotificationsConsumerService)
        .useValue({ onModuleInit: jest.fn(), onModuleDestroy: jest.fn() })
        .compile();

    const app = moduleRef.createNestApplication();
    applyGlobalSetup(app);
    await app.init();

    return { app, module: moduleRef, mockAuthProvider, mockDynamoDB };
}

export interface E2eTestApp {
    app: INestApplication;
    module: TestingModule;
    mockAuthProvider: ReturnType<typeof createMockAuthProvider>;
}

export async function createE2eApp(): Promise<E2eTestApp> {
    const mockAuthProvider = createMockAuthProvider();

    const moduleRef = await Test.createTestingModule({ imports: [AppModule] })
        .setLogger(noopLogger)
        .overrideProvider(JwtStrategy)
        .useClass(TestJwtStrategy)
        .overrideProvider(AUTH_PROVIDER)
        .useValue(mockAuthProvider)
        .overrideProvider(NotificationsConsumerService)
        .useValue({ onModuleInit: jest.fn(), onModuleDestroy: jest.fn() })
        .compile();

    const app = moduleRef.createNestApplication();
    applyGlobalSetup(app);
    await app.init();

    return { app, module: moduleRef, mockAuthProvider };
}
