import { Inject, Injectable } from '@nestjs/common';
import type { ConfigType } from '@nestjs/config';
import type { ChatRequest, ChatStreamEvent } from '@documents/shared';
import { appConfig } from '@config/index';

export interface StreamedEvent {
    /** The event exactly as ai-service sent it, to pass through to the browser. */
    raw: string;
    event: ChatStreamEvent;
}

const STREAM_TIMEOUT_MS = 120_000;

/** Server-sent events from a fetch response: blocks of `event:` and `data:` lines. */
export async function* readEvents(body: AsyncIterable<Uint8Array>): AsyncGenerator<StreamedEvent> {
    const decoder = new TextDecoder();
    let buffer = '';
    for await (const bytes of body) {
        buffer += decoder.decode(bytes, { stream: true });
        let end: number;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
            const block = buffer.slice(0, end);
            buffer = buffer.slice(end + 2);
            const data = block
                .split('\n')
                .filter((line) => line.startsWith('data: '))
                .map((line) => line.slice(6))
                .join('\n');
            if (data) {
                yield { raw: `${block}\n\n`, event: JSON.parse(data) as ChatStreamEvent };
            }
        }
    }
}

/** The AI service's chat endpoint. The user's own token goes with every request. */
@Injectable()
export class AiServiceClient {
    constructor(@Inject(appConfig.KEY) private readonly app: ConfigType<typeof appConfig>) {}

    async *stream(authorization: string, request: ChatRequest): AsyncGenerator<StreamedEvent> {
        const response = await fetch(`${this.app.aiServiceUrl}/v1/chat/stream`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                Accept: 'text/event-stream',
                Authorization: authorization
            },
            body: JSON.stringify(request),
            signal: AbortSignal.timeout(STREAM_TIMEOUT_MS)
        });
        if (!response.ok || !response.body) {
            throw new Error(`AI service answered ${response.status}`);
        }
        yield* readEvents(response.body as unknown as AsyncIterable<Uint8Array>);
    }
}
