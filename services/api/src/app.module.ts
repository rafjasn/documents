import { Module, Global } from '@nestjs/common';
import { ConfigModule, ConfigService } from '@nestjs/config';
import { awsConfig, appConfig } from './config';
import {
    createDynamoDBClient,
    createS3Client,
    createSQSClient,
    createSNSClient
} from '@documents/shared';
import { DocumentsModule } from './modules/documents/documents.module';
import { AuthModule } from './modules/auth/auth.module';
import { HealthModule } from './modules/health/health.module';
import { ChatModule } from './modules/chat/chat.module';
import { DocumentGateway } from './modules/gateway/document.gateway';
import { NotificationsConsumerService } from './modules/gateway/notifications-consumer.service';

@Global()
@Module({
    imports: [
        ConfigModule.forRoot({ isGlobal: true, load: [awsConfig, appConfig] }),
        AuthModule,
        DocumentsModule,
        HealthModule,
        ChatModule
    ],
    providers: [
        DocumentGateway,
        NotificationsConsumerService,
        {
            provide: 'DYNAMODB_CLIENT',
            inject: [ConfigService],
            useFactory: (config: ConfigService) =>
                createDynamoDBClient({
                    region: config.get<string>('aws.region')!,
                    endpoint: config.get<string>('aws.endpoint')!,
                    credentials: config.get('aws.credentials')!
                })
        },
        {
            provide: 'S3_CLIENT',
            inject: [ConfigService],
            useFactory: (config: ConfigService) =>
                createS3Client({
                    region: config.get<string>('aws.region')!,
                    endpoint: config.get<string>('aws.endpoint')!,
                    credentials: config.get('aws.credentials')!
                })
        },
        {
            provide: 'SQS_CLIENT',
            inject: [ConfigService],
            useFactory: (config: ConfigService) =>
                createSQSClient({
                    region: config.get<string>('aws.region')!,
                    endpoint: config.get<string>('aws.endpoint')!,
                    credentials: config.get('aws.credentials')!
                })
        },
        {
            provide: 'SNS_CLIENT',
            inject: [ConfigService],
            useFactory: (config: ConfigService) =>
                createSNSClient({
                    region: config.get<string>('aws.region')!,
                    endpoint: config.get<string>('aws.endpoint')!,
                    credentials: config.get('aws.credentials')!
                })
        }
    ],
    exports: ['DYNAMODB_CLIENT', 'S3_CLIENT', 'SQS_CLIENT', 'SNS_CLIENT', DocumentGateway]
})
export class AppModule {}
