import { Injectable, Logger } from '@nestjs/common';
import {
    BedrockRuntimeClient,
    InvokeModelCommand,
    InvokeModelWithResponseStreamCommand
} from '@aws-sdk/client-bedrock-runtime';

export interface CompletionRequest {
    systemPrompt: string;
    userPrompt: string;
    maxTokens?: number;
    format?: {
        type: string;
        properties: object;
        required: string[];
    };
}

export interface CompletionResponse {
    text: string;
    tokensUsed: number;
}

const MAX_PROMPT_CHARS = 3200;

@Injectable()
export class ModelProviderService {
    private readonly logger = new Logger(ModelProviderService.name);
    private readonly provider = process.env.AI_PROVIDER || 'ollama';
    private readonly model = process.env.AI_MODEL || 'llama3.2:3b';

    async complete(request: CompletionRequest): Promise<CompletionResponse> {
        switch (this.provider) {
            case 'ollama':
                return this.callOllama(request);
            case 'openai':
                return this.callOpenAI(request);
            case 'anthropic':
                return this.callAnthropic(request);
            case 'bedrock':
                return this.callBedrock(request);
            default:
                throw new Error(`Unknown AI provider: ${this.provider}`);
        }
    }

    async *stream(request: CompletionRequest): AsyncGenerator<string> {
        switch (this.provider) {
            case 'ollama':
                yield* this.streamOllama(request);
                break;
            case 'openai':
                yield* this.streamOpenAI(request);
                break;
            case 'bedrock':
                yield* this.streamBedrock(request);
                break;
            default: {
                const result = await this.complete(request);
                yield result.text;
            }
        }
    }

    private async callOllama(req: CompletionRequest): Promise<CompletionResponse> {
        const baseUrl = process.env.OLLAMA_BASE_URL || 'http://ollama:11434';
        const res = await fetch(`${baseUrl}/api/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                model: this.model,
                prompt: `${req.systemPrompt}\n\n${req.userPrompt}`.slice(0, MAX_PROMPT_CHARS),
                stream: false,
                ...(req.format ? { format: req.format } : {})
            })
        });

        if (!res.ok) {
            const body = await res.text();
            throw new Error(`Ollama ${res.status}: ${body}`);
        }

        const data = (await res.json()) as { response?: string; eval_count?: number };
        if (!data.response) {
            throw new Error(`Ollama returned empty response for model ${this.model}`);
        }

        return { text: data.response, tokensUsed: data.eval_count || 0 };
    }

    private async *streamOllama(req: CompletionRequest): AsyncGenerator<string> {
        const baseUrl = process.env.OLLAMA_BASE_URL || 'http://ollama:11434';
        const res = await fetch(`${baseUrl}/api/generate`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                model: this.model,
                prompt: `${req.systemPrompt}\n\n${req.userPrompt}`.slice(0, MAX_PROMPT_CHARS),
                stream: true
            })
        });

        if (!res.ok) {
            const body = await res.text();
            throw new Error(`Ollama ${res.status}: ${body}`);
        }

        const reader = res.body?.getReader();

        if (!reader) {
            return;
        }

        const decoder = new TextDecoder();

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            const lines = decoder.decode(value, { stream: true }).split('\n').filter(Boolean);
            for (const line of lines) {
                try {
                    const parsed = JSON.parse(line);
                    if (parsed.response) yield parsed.response;
                } catch {}
            }
        }
    }

    private async callOpenAI(req: CompletionRequest): Promise<CompletionResponse> {
        const res = await fetch('https://api.openai.com/v1/chat/completions', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                Authorization: `Bearer ${process.env.OPENAI_API_KEY}`
            },
            body: JSON.stringify({
                model: this.model,
                messages: [
                    { role: 'system', content: req.systemPrompt },
                    { role: 'user', content: req.userPrompt }
                ],
                max_tokens: req.maxTokens || 1000,
                ...(req.format
                    ? {
                          response_format: {
                              type: 'json_schema',
                              json_schema: { name: 'response', schema: req.format, strict: true }
                          }
                      }
                    : {})
            })
        });

        if (!res.ok) {
            const body = await res.text();
            throw new Error(`OpenAI ${res.status}: ${body}`);
        }

        const data = (await res.json()) as {
            choices: { message: { content: string } }[];
            usage?: { total_tokens: number };
        };

        return {
            text: data.choices[0].message.content,
            tokensUsed: data.usage?.total_tokens || 0
        };
    }

    private async *streamOpenAI(req: CompletionRequest): AsyncGenerator<string> {
        const res = await fetch('https://api.openai.com/v1/chat/completions', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                Authorization: `Bearer ${process.env.OPENAI_API_KEY}`
            },
            body: JSON.stringify({
                model: this.model,
                messages: [
                    { role: 'system', content: req.systemPrompt },
                    { role: 'user', content: req.userPrompt }
                ],
                max_tokens: req.maxTokens || 1000,
                stream: true
            })
        });

        if (!res.ok) {
            const body = await res.text();
            throw new Error(`OpenAI ${res.status}: ${body}`);
        }

        const reader = res.body?.getReader();

        if (!reader) {
            return;
        }

        const decoder = new TextDecoder();

        while (true) {
            const { done, value } = await reader.read();

            if (done) {
                break;
            }

            const lines = decoder.decode(value, { stream: true }).split('\n').filter(Boolean);

            for (const line of lines) {
                if (!line.startsWith('data: ') || line === 'data: [DONE]') {
                    continue;
                }

                try {
                    const parsed = JSON.parse(line.slice(6));
                    const delta = parsed.choices?.[0]?.delta?.content;
                    if (delta) yield delta;
                } catch {
                    // eslint-ignore
                }
            }
        }
    }

    private async callBedrock(req: CompletionRequest): Promise<CompletionResponse> {
        const client = new BedrockRuntimeClient({
            region: process.env.AWS_REGION || 'us-east-1'
        });

        const body = JSON.stringify({
            anthropic_version: 'bedrock-2023-05-31',
            max_tokens: req.maxTokens || 1000,
            system: req.systemPrompt,
            messages: [{ role: 'user', content: req.userPrompt }]
        });

        const response = await client.send(
            new InvokeModelCommand({
                modelId: this.model,
                contentType: 'application/json',
                accept: 'application/json',
                body: Buffer.from(body)
            })
        );

        const result = JSON.parse(Buffer.from(response.body).toString()) as {
            content: { text: string }[];
            usage?: { input_tokens: number; output_tokens: number };
        };

        return {
            text: result.content[0].text,
            tokensUsed: (result.usage?.input_tokens || 0) + (result.usage?.output_tokens || 0)
        };
    }

    private async *streamBedrock(req: CompletionRequest): AsyncGenerator<string> {
        const client = new BedrockRuntimeClient({
            region: process.env.AWS_REGION || 'us-east-1'
        });

        const body = JSON.stringify({
            anthropic_version: 'bedrock-2023-05-31',
            max_tokens: req.maxTokens || 1000,
            system: req.systemPrompt,
            messages: [{ role: 'user', content: req.userPrompt }]
        });

        const response = await client.send(
            new InvokeModelWithResponseStreamCommand({
                modelId: this.model,
                contentType: 'application/json',
                accept: 'application/json',
                body: Buffer.from(body)
            })
        );

        const stream = response.body as unknown as AsyncIterable<{
            chunk?: { bytes?: Uint8Array };
        }>;

        for await (const event of stream) {
            if (!event.chunk?.bytes) {
                continue;
            }

            try {
                const chunk = JSON.parse(Buffer.from(event.chunk.bytes).toString()) as {
                    type: string;
                    delta?: { type: string; text?: string };
                };

                if (chunk.type === 'content_block_delta' && chunk.delta?.text) {
                    yield chunk.delta.text;
                }
            } catch {}
        }
    }

    private async callAnthropic(req: CompletionRequest): Promise<CompletionResponse> {
        const res = await fetch('https://api.anthropic.com/v1/messages', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'x-api-key': process.env.ANTHROPIC_API_KEY!,
                'anthropic-version': '2023-06-01'
            },
            body: JSON.stringify({
                model: this.model,
                max_tokens: req.maxTokens || 1000,
                system: req.systemPrompt,
                messages: [{ role: 'user', content: req.userPrompt }]
            })
        });

        if (!res.ok) {
            const body = await res.text();
            throw new Error(`Anthropic ${res.status}: ${body}`);
        }

        const data = (await res.json()) as {
            content: { text: string }[];
            usage?: { input_tokens: number; output_tokens: number };
        };

        return {
            text: data.content[0].text,
            tokensUsed: (data.usage?.input_tokens || 0) + (data.usage?.output_tokens || 0)
        };
    }
}
