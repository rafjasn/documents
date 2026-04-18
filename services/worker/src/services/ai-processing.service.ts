import { Injectable, Logger, Inject } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { DynamoDBDocumentClient, UpdateCommand, GetCommand } from '@aws-sdk/lib-dynamodb';
import { SNSClient, PublishCommand } from '@aws-sdk/client-sns';
import { DocumentRecord, DocumentStatus, WsDocumentStatusEvent } from '@documents/shared';
import { ModelProviderService } from './model-provider.service';

interface RetryOptions {
    maxAttempts: number;
    baseMs: number;
}

async function retry<T>(fn: () => Promise<T>, opts: RetryOptions): Promise<T> {
    const { maxAttempts, baseMs } = opts;
    let lastError: unknown;

    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
        try {
            return await fn();
        } catch (err) {
            lastError = err;
            if (attempt === maxAttempts) break;

            const exponential = baseMs * Math.pow(2, attempt - 1);
            const jitter = exponential * 0.2 * (Math.random() * 2 - 1); // ±20 %
            const delay = Math.round(exponential + jitter);
            console.warn(
                `[AI retry] attempt ${attempt}/${maxAttempts} failed — retrying in ${delay}ms. Error: ${err}`
            );
            await new Promise((r) => setTimeout(r, delay));
        }
    }

    throw lastError;
}

interface AiClassification {
    language?: string;
    category?: string;
    keywords?: string[];
    tags?: string[];
    people?: string[];
    organizations?: string[];
    locations?: string[];
    confidence?: number;
}

export interface AiJob {
    documentId: string;
    userId: string;
    category?: string;
}

@Injectable()
export class AiProcessingService {
    private readonly logger = new Logger(AiProcessingService.name);
    private readonly tableName: string;
    private readonly snsTopicArn: string;

    constructor(
        @Inject('DYNAMODB_CLIENT')
        private readonly dynamodb: DynamoDBDocumentClient,
        @Inject('SNS_CLIENT') private readonly sns: SNSClient,
        private readonly model: ModelProviderService,
        config: ConfigService
    ) {
        this.tableName = config.get<string>('aws.dynamodb.tableName')!;
        this.snsTopicArn = config.get<string>('aws.sns.topicArn')!;
    }

    async classify(job: AiJob): Promise<string> {
        const { documentId, userId } = job;
        this.logger.log(`Classifying document ${documentId}`);

        const extractedText = await this.fetchExtractedText(userId, documentId);

        const result = await retry(
            () =>
                this.model.complete({
                    systemPrompt: `You are a document analysis assistant. Return a JSON object that strictly follows the provided schema. Do not include any explanations or extra text.`,
                    userPrompt: `Analyze the following document and extract structured metadata.\n\nDocument:\n${extractedText}`,
                    maxTokens: 600,
                    format: {
                        type: 'object',
                        properties: {
                            language: {
                                type: 'string',
                                description: "ISO 639-1 language code, e.g. 'en'"
                            },
                            category: {
                                type: 'string',
                                description:
                                    'Document category: INVOICE, RECEIPT, CONTRACT, REPORT, LETTER, FORM, or OTHER'
                            },
                            keywords: {
                                type: 'array',
                                items: { type: 'string' },
                                description: '5-10 key terms from the document'
                            },
                            tags: {
                                type: 'array',
                                items: { type: 'string' },
                                description: '2-5 broad topic tags; always provide at least one'
                            },
                            people: {
                                type: 'array',
                                items: { type: 'string' },
                                description: 'Full names of people mentioned'
                            },
                            organizations: {
                                type: 'array',
                                items: { type: 'string' },
                                description: 'Names of companies or organizations mentioned'
                            },
                            locations: {
                                type: 'array',
                                items: { type: 'string' },
                                description: 'Cities, countries, or addresses mentioned'
                            },
                            confidence: {
                                type: 'number',
                                description: 'Classification confidence between 0 and 1'
                            }
                        },
                        required: [
                            'language',
                            'category',
                            'keywords',
                            'tags',
                            'people',
                            'organizations',
                            'locations',
                            'confidence'
                        ]
                    }
                }),
            { maxAttempts: 3, baseMs: 1_000 }
        );

        let parsed: AiClassification = {};
        try {
            parsed = JSON.parse(result.text) as AiClassification;
        } catch {
            this.logger.warn(
                `Document ${documentId}: could not parse classification JSON, using raw text`
            );
        }

        const category = parsed.category ?? 'OTHER';
        const now = new Date().toISOString();

        const metadata = {
            keywords: parsed.keywords ?? [],
            tags: parsed.tags?.length ? parsed.tags : [category.toLowerCase()],
            names: [...(parsed.people ?? []), ...(parsed.organizations ?? [])],
            locations: parsed.locations ?? [],
            language: parsed.language ?? 'unknown',
            confidence: parsed.confidence ?? 0
        };

        await this.dynamodb.send(
            new UpdateCommand({
                TableName: this.tableName,
                Key: { PK: `USER#${userId}`, SK: `DOC#${documentId}` },
                UpdateExpression:
                    'SET category = :category, #metadata = :metadata, updatedAt = :now',
                ExpressionAttributeNames: { '#metadata': 'metadata' },
                ExpressionAttributeValues: {
                    ':category': category,
                    ':metadata': metadata,
                    ':now': now
                }
            })
        );

        await this.publishStatusEvent(userId, documentId, DocumentStatus.CLASSIFYING, {
            category,
            metadata
        });

        this.logger.log(
            `Document ${documentId} classified as ${category} (${result.tokensUsed} tokens)`
        );

        return category;
    }

    async summarize(job: AiJob): Promise<void> {
        const { documentId, userId, category } = job;
        this.logger.log(`Summarizing document ${documentId} (${category ?? 'unknown'})`);

        const extractedText = await this.fetchExtractedText(userId, documentId);

        const summary = await retry(
            () =>
                this.model.complete({
                    systemPrompt: `You are a document analysis assistant. Summarize the following ${category ?? ''} document concisely in 2-3 sentences, highlighting the key information. Do not ask questions, do not ask for additional information, do not mention it's a summary, just get to the point`,
                    userPrompt: extractedText,
                    maxTokens: 500
                }),
            { maxAttempts: 3, baseMs: 1_000 }
        );

        const now = new Date().toISOString();

        await this.dynamodb.send(
            new UpdateCommand({
                TableName: this.tableName,
                Key: { PK: `USER#${userId}`, SK: `DOC#${documentId}` },
                UpdateExpression:
                    'SET #status = :status, summary = :summary, updatedAt = :now, processingCompletedAt = :now, GSI1SK = :gsi1sk',
                ExpressionAttributeNames: { '#status': 'status' },
                ExpressionAttributeValues: {
                    ':status': DocumentStatus.COMPLETED,
                    ':summary': summary.text,
                    ':now': now,
                    ':gsi1sk': `STATUS#${DocumentStatus.COMPLETED}#${now}`
                }
            })
        );

        await this.publishStatusEvent(userId, documentId, DocumentStatus.COMPLETED, {
            summary: summary.text
        });
        this.logger.log(`Document ${documentId} summarized (${summary.tokensUsed} tokens)`);
    }

    async markFailed(job: AiJob, error: Error): Promise<void> {
        const { documentId, userId } = job;
        const now = new Date().toISOString();

        await this.dynamodb.send(
            new UpdateCommand({
                TableName: this.tableName,
                Key: { PK: `USER#${userId}`, SK: `DOC#${documentId}` },
                UpdateExpression:
                    'SET #status = :status, errorMessage = :error, updatedAt = :now, processingCompletedAt = :now, GSI1SK = :gsi1sk',
                ExpressionAttributeNames: { '#status': 'status' },
                ExpressionAttributeValues: {
                    ':status': DocumentStatus.FAILED,
                    ':error': error?.message ?? 'AI processing failed',
                    ':now': now,
                    ':gsi1sk': `STATUS#${DocumentStatus.FAILED}#${now}`
                }
            })
        );

        await this.publishStatusEvent(userId, documentId, DocumentStatus.FAILED);
        this.logger.warn(`Document ${documentId} marked FAILED: ${error?.message}`);
    }

    private async fetchExtractedText(userId: string, documentId: string): Promise<string> {
        const result = await this.dynamodb.send(
            new GetCommand({
                TableName: this.tableName,
                Key: { PK: `USER#${userId}`, SK: `DOC#${documentId}` },
                ProjectionExpression: 'extractedText'
            })
        );
        return (result.Item?.extractedText as string) || '';
    }

    private async publishStatusEvent(
        userId: string,
        documentId: string,
        status: DocumentStatus,
        document?: Partial<DocumentRecord>
    ): Promise<void> {
        const event: WsDocumentStatusEvent = {
            documentId,
            status,
            timestamp: new Date().toISOString(),
            ...(document ? { document } : {})
        };
        try {
            await this.sns.send(
                new PublishCommand({
                    TopicArn: this.snsTopicArn,
                    Message: JSON.stringify(event),
                    MessageAttributes: {
                        eventType: { DataType: 'String', StringValue: 'DOCUMENT_STATUS' },
                        userId: { DataType: 'String', StringValue: userId }
                    }
                })
            );
        } catch (error) {
            this.logger.warn(`SNS publish failed for ${documentId}: ${error}`);
        }
    }
}
