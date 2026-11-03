import { EventEmitter } from 'node:events';
import { ROOT_CONTEXT, SpanKind, trace } from '@opentelemetry/api';
import {
    InMemorySpanExporter,
    SamplingDecision,
    SimpleSpanProcessor
} from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { register } from 'prom-client';
import { ChatRequestSampler, measureRequests, startTracing, traceChatRequests } from './telemetry';

describe('telemetry', () => {
    const sampler = new ChatRequestSampler();
    const decision = (attributes: Record<string, string>) =>
        sampler.shouldSample(ROOT_CONTEXT, 'trace', 'POST', SpanKind.SERVER, attributes).decision;

    it('traces chat requests only', () => {
        expect(decision({ 'http.target': '/api/ai/conversations/c-1/stream' })).toBe(
            SamplingDecision.RECORD_AND_SAMPLED
        );
        expect(decision({ 'url.path': '/api/ai/conversations/c-1/stream' })).toBe(
            SamplingDecision.RECORD_AND_SAMPLED
        );
        for (const path of [
            '/api/documents',
            '/api/ai/conversations',
            '/api/ai/conversations/c-1/messages',
            '/api/health'
        ]) {
            expect(decision({ 'http.target': path })).toBe(SamplingDecision.NOT_RECORD);
        }
        expect(decision({})).toBe(SamplingDecision.NOT_RECORD); // e.g. a queue poll
    });

    it('stays off without the Langfuse keys', () => {
        expect(startTracing({})).toBe(false);
        expect(startTracing({ LANGFUSE_PUBLIC_KEY: 'pk', LANGFUSE_SECRET_KEY: 'sk' })).toBe(false);
    });

    it('counts requests by their route template, not their path', async () => {
        const response = Object.assign(new EventEmitter(), { statusCode: 200 });
        const next = jest.fn();
        measureRequests(
            { method: 'GET', baseUrl: '/api/documents', route: { path: '/:id' } },
            response,
            next
        );
        response.emit('finish');

        expect(next).toHaveBeenCalled();
        const metrics = await register.getSingleMetricAsString('api_http_requests_total');
        expect(metrics).toContain('route="/api/documents/:id"');
        expect(metrics).not.toContain('doc-');
    });

    it('opens a span for a chat request, active while it is handled', () => {
        const spans = new InMemorySpanExporter();
        const provider = new NodeTracerProvider({
            spanProcessors: [new SimpleSpanProcessor(spans)]
        });
        provider.register();
        const handled = (path: string) => {
            const response = Object.assign(new EventEmitter(), { statusCode: 200 });
            let active: string | undefined;
            traceChatRequests({ method: 'POST', path }, response, () => {
                active = trace.getActiveSpan()?.spanContext().traceId;
            });
            response.emit('close');
            return active;
        };

        const inChat = handled('/api/ai/conversations/c-1/stream');
        const elsewhere = handled('/api/documents');

        const [span] = spans.getFinishedSpans();
        expect(spans.getFinishedSpans()).toHaveLength(1);
        expect(span.name).toBe('POST /api/ai/conversations/:id/stream');
        expect(span.attributes['http.response.status_code']).toBe(200);
        expect(inChat).toBe(span.spanContext().traceId);
        expect(elsewhere).toBeUndefined();
        return provider.shutdown();
    });
});
