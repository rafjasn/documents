import type { LoggerService } from '@nestjs/common';
import {
    CloudWatchLogsClient,
    CreateLogGroupCommand,
    CreateLogStreamCommand,
    PutLogEventsCommand
} from '@aws-sdk/client-cloudwatch-logs';
import { hostname } from 'os';

interface LogEntry {
    timestamp: number;
    message: string;
}

export class CloudWatchLogger implements LoggerService {
    private readonly client: CloudWatchLogsClient | null;
    private readonly logGroupName: string;
    private readonly logStreamName: string;
    private buffer: LogEntry[] = [];
    private flushTimer: NodeJS.Timeout | null = null;
    private ready = false;

    constructor(logGroupName: string) {
        this.logGroupName = logGroupName;
        // Unique per-process stream so parallel containers don't collide
        this.logStreamName = `${hostname()}-${new Date().toISOString().slice(0, 10)}-${process.pid}`;

        const endpoint = process.env.AWS_ENDPOINT;
        const enabled = process.env.CLOUDWATCH_ENABLED === 'true';

        if (endpoint || enabled) {
            this.client = new CloudWatchLogsClient({
                region: process.env.AWS_REGION ?? 'us-east-1',
                ...(endpoint ? { endpoint } : {}),
                credentials: process.env.AWS_ACCESS_KEY_ID
                    ? {
                          accessKeyId: process.env.AWS_ACCESS_KEY_ID,
                          secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY ?? ''
                      }
                    : undefined
            });

            this.ensureLogStream()
                .then(() => {
                    this.ready = true;
                    this.flushTimer = setInterval(() => void this.flush(), 2000);
                })
                .catch((e: Error) => console.error(`[CloudWatchLogger] init failed: ${e.message}`));
        } else {
            this.client = null;
        }
    }

    log(message: unknown, context?: string): void {
        this.write('INFO', message, context);
    }

    error(message: unknown, trace?: string, context?: string): void {
        this.write('ERROR', message, context, trace);
    }

    warn(message: unknown, context?: string): void {
        this.write('WARN', message, context);
    }

    debug(message: unknown, context?: string): void {
        this.write('DEBUG', message, context);
    }

    verbose(message: unknown, context?: string): void {
        this.write('VERBOSE', message, context);
    }

    fatal(message: unknown, context?: string): void {
        this.write('FATAL', message, context);
    }

    setupProcessHandlers(): void {
        process.on('unhandledRejection', (reason) => {
            const msg = reason instanceof Error ? reason.message : String(reason);
            const trace = reason instanceof Error ? reason.stack : undefined;
            this.error(`Unhandled promise rejection: ${msg}`, trace, 'Process');
        });

        process.on('uncaughtException', (err) => {
            this.error(`Uncaught exception: ${err.message}`, err.stack, 'Process');
            void this.flushAndClose().finally(() => process.exit(1));
        });
    }

    async flushAndClose(): Promise<void> {
        if (this.flushTimer) {
            clearInterval(this.flushTimer);
            this.flushTimer = null;
        }
        await this.flush();
    }

    private write(level: string, message: unknown, context?: string, trace?: string): void {
        const msg = typeof message === 'string' ? message : JSON.stringify(message);
        const ctx = context ? ` [${context}]` : '';
        const line = `[Nest]${ctx} ${level} - ${msg}`;

        if (level === 'ERROR' || level === 'FATAL') {
            console.error(line);
            if (trace) console.error(trace);
        } else if (level === 'WARN') {
            console.warn(line);
        } else {
            console.log(line);
        }

        if (this.client) {
            this.buffer.push({
                timestamp: Date.now(),
                message: JSON.stringify({
                    level,
                    context: context ?? '',
                    message: msg,
                    ...(trace ? { trace } : {}),
                    timestamp: new Date().toISOString()
                })
            });
        }
    }

    private async ensureLogStream(): Promise<void> {
        if (!this.client) return;
        try {
            await this.client.send(new CreateLogGroupCommand({ logGroupName: this.logGroupName }));
        } catch (e: unknown) {
            if ((e as { name?: string }).name !== 'ResourceAlreadyExistsException') throw e;
        }
        try {
            await this.client.send(
                new CreateLogStreamCommand({
                    logGroupName: this.logGroupName,
                    logStreamName: this.logStreamName
                })
            );
        } catch (e: unknown) {
            if ((e as { name?: string }).name !== 'ResourceAlreadyExistsException') throw e;
        }
    }

    private async flush(): Promise<void> {
        if (!this.client || !this.ready || !this.buffer.length) return;
        // CloudWatch requires events sorted by timestamp
        const events = this.buffer.splice(0).sort((a, b) => a.timestamp - b.timestamp);
        try {
            await this.client.send(
                new PutLogEventsCommand({
                    logGroupName: this.logGroupName,
                    logStreamName: this.logStreamName,
                    logEvents: events
                })
            );
        } catch {
            // Re-queue on failure
            this.buffer.unshift(...events);
        }
    }
}
