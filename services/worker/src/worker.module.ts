import { Module, Global } from '@nestjs/common';
import { ConfigModule, ConfigService } from '@nestjs/config';
import { awsConfig, appConfig } from './config';
import {
    createDynamoDBClient,
    createS3Client,
    createSQSClient,
    createSNSClient
} from '@documents/shared';
import { ProcessingService } from './services/processing.service';
import { ExtractionService } from './services/extraction.service';
import { SqsPublisherService } from './services/sqs-publisher.service';
import { ThumbnailService } from './services/thumbnail.service';
import { ThumbnailPollService } from './services/thumbnail-poll.service';
import { ModelProviderService } from './services/model-provider.service';
import { AiProcessingService } from './services/ai-processing.service';
import { HealthController } from './health/health.controller';

@Global()
@Module({
    imports: [ConfigModule.forRoot({ isGlobal: true, load: [awsConfig, appConfig] })],
    controllers: [HealthController],
    providers: [
        ModelProviderService,
        AiProcessingService,
        ProcessingService,
        ExtractionService,
        SqsPublisherService,
        ThumbnailService,
        ThumbnailPollService,
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
    exports: ['DYNAMODB_CLIENT', 'S3_CLIENT', 'SQS_CLIENT', 'SNS_CLIENT']
})
export class WorkerModule {}
