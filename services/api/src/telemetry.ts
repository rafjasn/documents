/**
 * Tracing and metrics for the API (Stage 10c). `main.ts` imports this module first.
 *
 * Spans go to Langfuse over OTLP when LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and
 * LANGFUSE_BASE_URL are set. Only chat requests are traced: they are what a trace in
 * Langfuse is for, and every other request (document lists, thumbnails, health checks)
 * would spend the plan's units. `traceChatRequests` opens the request's span in code: the
 * API is bundled by webpack, so the http, Express and AWS SDK instrumentations, which
 * patch modules as they load, never see them. `fetch` is instrumented through Node's
 * diagnostics channel, which bundling doesn't affect, so the call to the ai-service is a
 * child span and carries the W3C `traceparent` the ai-service continues.
 *
 * This exporter can't reach the Python services' masking function, so the API's spans
 * carry no text: the method, the route template, the status and the timings.
 *
 * Metrics are prom-client's default registry, served on METRICS_PORT (default 9464) by a
 * server of their own, which nginx doesn't route to.
 */
import { createServer, type Server } from 'node:http';
import {
    context,
    diag,
    DiagConsoleLogger,
    DiagLogLevel,
    SpanKind,
    SpanStatusCode,
    trace,
    type Attributes,
    type Context
} from '@opentelemetry/api';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-proto';
import { registerInstrumentations } from '@opentelemetry/instrumentation';
import { UndiciInstrumentation } from '@opentelemetry/instrumentation-undici';
import { resourceFromAttributes } from '@opentelemetry/resources';
import {
    BatchSpanProcessor,
    ParentBasedSampler,
    SamplingDecision,
    type Sampler,
    type SamplingResult
} from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { collectDefaultMetrics, Counter, Histogram, register } from 'prom-client';

/** The chat stream: `POST /api/ai/conversations/:id/stream`. */
export const CHAT_PATH = /^\/api\/ai\/conversations\/[^/?]+\/stream(?:\?|$)/;
const CHAT_ROUTE = '/api/ai/conversations/:id/stream';

/** Starts a trace only for a chat request; a span with a parent follows the parent, and a
 * client call outside a request (a JWKS fetch, a queue poll) isn't recorded. */
export class ChatRequestSampler implements Sampler {
    shouldSample(
        _context: Context,
        _traceId: string,
        _name: string,
        _kind: SpanKind,
        attributes: Attributes
    ): SamplingResult {
        const path = String(attributes['url.path'] ?? attributes['http.target'] ?? '');
        return {
            decision: CHAT_PATH.test(path)
                ? SamplingDecision.RECORD_AND_SAMPLED
                : SamplingDecision.NOT_RECORD
        };
    }

    toString(): string {
        return 'ChatRequestSampler';
    }
}

export function startTracing(env: NodeJS.ProcessEnv = process.env): boolean {
    const publicKey = env.LANGFUSE_PUBLIC_KEY;
    const secretKey = env.LANGFUSE_SECRET_KEY;
    const baseUrl = env.LANGFUSE_BASE_URL;
    if (!publicKey || !secretKey || !baseUrl) {
        return false;
    }
    const level = env.OTEL_LOG_LEVEL?.toUpperCase() as keyof typeof DiagLogLevel | undefined;
    if (level && level in DiagLogLevel) {
        diag.setLogger(new DiagConsoleLogger(), DiagLogLevel[level]); // the SDK is silent otherwise
    }
    const credentials = Buffer.from(`${publicKey}:${secretKey}`).toString('base64');
    const exporter = new OTLPTraceExporter({
        url: `${baseUrl.replace(/\/$/, '')}/api/public/otel/v1/traces`,
        headers: { Authorization: `Basic ${credentials}` }
    });
    const provider = new NodeTracerProvider({
        resource: resourceFromAttributes({ 'service.name': 'documents-api' }),
        sampler: new ParentBasedSampler({ root: new ChatRequestSampler() }),
        spanProcessors: [new BatchSpanProcessor(exporter)]
    });
    provider.register();
    registerInstrumentations({
        tracerProvider: provider,
        instrumentations: [new UndiciInstrumentation()]
    });
    return true;
}

interface TracedRequest {
    method: string;
    path: string;
}

interface TracedResponse {
    statusCode: number;
    once(event: 'close', listener: () => void): unknown;
}

/** Express middleware: a server span for each chat request, active while the request is
 * handled, so the ai-service call below it joins its trace. Other requests pass by. */
export function traceChatRequests(
    request: TracedRequest,
    response: TracedResponse,
    next: () => void
): void {
    if (!CHAT_PATH.test(request.path)) {
        next();
        return;
    }
    const span = trace.getTracer('documents-api').startSpan(`${request.method} ${CHAT_ROUTE}`, {
        kind: SpanKind.SERVER,
        attributes: {
            'http.request.method': request.method,
            'http.route': CHAT_ROUTE,
            'url.path': request.path
        }
    });
    response.once('close', () => {
        span.setAttribute('http.response.status_code', response.statusCode);
        if (response.statusCode >= 500) {
            span.setStatus({ code: SpanStatusCode.ERROR });
        }
        span.end();
    });
    context.with(trace.setSpan(context.active(), span), next);
}

// Metrics ------------------------------------------------------------------------------

export const httpRequests = new Counter({
    name: 'api_http_requests_total',
    help: 'HTTP requests by method, route and status',
    labelNames: ['method', 'route', 'status']
});

export const httpRequestSeconds = new Histogram({
    name: 'api_http_request_seconds',
    help: 'HTTP request time, to the end of the response (a chat stream included)',
    labelNames: ['method', 'route'],
    buckets: [0.05, 0.1, 0.25, 0.5, 1, 2, 3, 4, 5, 7.5, 10, 15, 20, 30, 60, 120]
});

interface TimedRequest {
    method: string;
    baseUrl?: string;
    route?: { path?: string };
}

interface TimedResponse {
    statusCode: number;
    on(event: 'finish', listener: () => void): unknown;
}

/** Express middleware that counts and times each request by its route template, so IDs
 * never become labels. */
export function measureRequests(
    request: TimedRequest,
    response: TimedResponse,
    next: () => void
): void {
    const started = process.hrtime.bigint();
    response.on('finish', () => {
        const route = request.route?.path
            ? `${request.baseUrl ?? ''}${request.route.path}`
            : 'unmatched';
        const seconds = Number(process.hrtime.bigint() - started) / 1e9;
        httpRequests.inc({ method: request.method, route, status: String(response.statusCode) });
        httpRequestSeconds.observe({ method: request.method, route }, seconds);
    });
    next();
}

export function serveMetrics(port: number): Server {
    collectDefaultMetrics();
    const server = createServer((request, response) => {
        if (request.url !== '/metrics') {
            response.writeHead(404).end();
            return;
        }
        register
            .metrics()
            .then((body) => {
                response.writeHead(200, { 'Content-Type': register.contentType }).end(body);
            })
            .catch(() => response.writeHead(500).end());
    });
    return server.listen(port);
}

startTracing();
