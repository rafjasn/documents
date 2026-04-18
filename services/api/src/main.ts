import { NestFactory, Reflector } from '@nestjs/core';
import { ValidationPipe, ClassSerializerInterceptor } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { DocumentBuilder, SwaggerModule } from '@nestjs/swagger';
import { CloudWatchLogger } from '@documents/shared';
import { AppModule } from './app.module';
import { GlobalExceptionFilter } from './common/filters/global-exception.filter';
import { LoggingInterceptor } from './common/interceptors/logging.interceptor';

async function bootstrap() {
    const logger = new CloudWatchLogger('/documents/api');
    logger.setupProcessHandlers();

    const app = await NestFactory.create(AppModule, { logger });

    const config = app.get(ConfigService);
    const port = config.get<number>('app.port', 3001);
    const corsOrigin = config.get<string>('app.corsOrigin', 'http://localhost:3000');

    app.enableCors({ origin: corsOrigin, credentials: true });
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

    const isProduction = process.env.NODE_ENV === 'production';

    if (!isProduction) {
        const swaggerConfig = new DocumentBuilder()
            .setTitle('Documents API')
            .setDescription('Document Processing Pipeline API')
            .setVersion('1.0')
            .addBearerAuth()
            .build();
        SwaggerModule.setup('api/docs', app, SwaggerModule.createDocument(app, swaggerConfig));
        logger.log(`Swagger docs at http://localhost:${port}/api/docs`, 'Bootstrap');
    }

    app.enableShutdownHooks();
    await app.listen(port);
    logger.log(`API running on http://localhost:${port}/api`, 'Bootstrap');
}

bootstrap();
