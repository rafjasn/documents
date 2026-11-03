"""Service telemetry (Stage 10c): OpenTelemetry spans beside the model calls, and
Prometheus metrics.

Spans. With tracing on, the Langfuse client exports this process's spans through its
span processor, which masks first (docai.core.tracing), on the provider of
`tracer_provider`, which drops traces that would start at a bare client call. `instrument` adds the service instrumentations: FastAPI (server
spans, continuing the W3C trace context the API sends), httpx (Qdrant, TEI, Gemini) and
botocore (S3, DynamoDB, SNS, SQS). Langfuse's default filter exports only model spans;
`should_export` admits these too. They carry IDs, sizes, statuses and timings: none of
the three records a request or response body, and botocore records no keys or values.
`trace_headers` carries the current trace to the LLM gateway, whose span joins it.

Metrics. Counters and histograms in prometheus_client's default registry, served at the
ai-service's /metrics and on the worker's metrics port. Prometheus adds which process
each came from, so the metrics carry no service label.
"""

import functools
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
)
from opentelemetry.trace import SpanKind
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from docai.core.prices import cost

logger = logging.getLogger(__name__)

TRACER = trace.get_tracer("docai")
SERVICE_SCOPES = (
    "docai",
    "opentelemetry.instrumentation.fastapi",
    "opentelemetry.instrumentation.asgi",
    "opentelemetry.instrumentation.httpx",
    "opentelemetry.instrumentation.botocore",
)
# Paths whose requests make no spans: probes and scrapes, every few seconds.
UNTRACED_PATHS = "health,metrics"

F = TypeVar("F", bound=Callable[..., Any])


def is_service_span(span: Any) -> bool:
    scope = getattr(span.instrumentation_scope, "name", "") or ""
    return any(scope == s or scope.startswith(f"{s}.") for s in SERVICE_SCOPES)


def should_export(span: Any) -> bool:
    """Langfuse's model spans, and the service spans of `SERVICE_SCOPES`."""
    from langfuse.span_filter import is_default_export_span

    return bool(is_default_export_span(span)) or is_service_span(span)


_instrumented = False


def instrument(app: Any = None) -> None:
    """Service spans for this process: httpx and botocore always, and `app`'s requests.
    Without tracing the global tracer provider is a no-op, and so are the spans."""
    global _instrumented
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        # A streamed answer sends a body chunk per token; a span for each would cost
        # Langfuse units for nothing, so only the request itself is a span.
        FastAPIInstrumentor.instrument_app(
            app, excluded_urls=UNTRACED_PATHS, exclude_spans=["receive", "send"]
        )
    if _instrumented:
        return
    _instrumented = True
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()
    try:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

        BotocoreInstrumentor().instrument()  # type: ignore[no-untyped-call]
    except ImportError:  # the botocore instrumentation ships with the AWS extras
        pass


class NoClientRoots(Sampler):
    """A trace starts at a request, an upload or a model call. A client call outside one
    (the worker's queue polls, Langfuse's own API calls, a warm-up) would each be a trace
    of one span, costing a Langfuse unit for nothing, so it isn't recorded."""

    def should_sample(
        self,
        parent_context: Any,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Any = None,
        links: Any = None,
        trace_state: Any = None,
    ) -> SamplingResult:
        if kind in (SpanKind.CLIENT, SpanKind.PRODUCER, SpanKind.CONSUMER):
            return SamplingResult(Decision.DROP)
        return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)

    def get_description(self) -> str:
        return "NoClientRoots"


def tracer_provider() -> TracerProvider:
    """This process's tracer provider, made global so the instrumentations use it; the
    Langfuse client adds its span processor to it. A span with a parent follows the
    parent's decision, as the API's sampling of chat requests needs."""
    provider = TracerProvider(sampler=ParentBased(root=NoClientRoots()))
    trace.set_tracer_provider(provider)
    return provider


def trace_headers() -> dict[str, str]:
    """W3C trace context for an outgoing request (`traceparent`), or nothing."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A span of this service's own around a step, such as one upload."""
    with TRACER.start_as_current_span(
        name, attributes={k: v for k, v in attributes.items() if v is not None}
    ) as current:
        yield current


# Metrics ------------------------------------------------------------------------------

SECONDS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 4, 5, 7.5, 10, 15, 20, 30, 60, 120)
REQUESTS = Counter(
    "docai_http_requests",
    "HTTP requests by endpoint and status",
    ["endpoint", "status"],
)
REQUEST_SECONDS = Histogram(
    "docai_http_request_seconds", "HTTP request time", ["endpoint"], buckets=SECONDS
)
CHATS = Counter(
    "docai_chat_requests",
    "Chat questions by outcome: answered, failed, or refused over a budget",
    ["outcome"],
)
CHAT_SECONDS = Histogram(
    "docai_chat_seconds", "Time from a question to its last event", buckets=SECONDS
)
NODE_SECONDS = Histogram(
    "docai_graph_node_seconds", "Time in each node of the chat graph", ["node"],
    buckets=SECONDS,
)  # fmt: skip
LLM_CALLS = Counter("docai_llm_calls", "Model calls made (not cached)", ["model"])
LLM_TOKENS = Counter(
    "docai_llm_tokens",
    "Tokens of model calls: input (cached ones included), cached input, output",
    ["model", "kind"],
)
LLM_COST = Counter(
    "docai_llm_cost_usd",
    "Cost of model calls, at the answering model's price",
    ["model"],
)
REQUEST_COST = Histogram(
    "docai_request_cost_usd",
    "Model cost of one chat question or one upload",
    ["kind"],
    buckets=(0.0005, 0.001, 0.002, 0.003, 0.005, 0.01, 0.02, 0.05, 0.1),
)
FALLBACKS = Counter(
    "docai_llm_fallbacks",
    "Calls the gateway's fallback answered",
    ["requested", "answered"],
)
BUDGET_REFUSALS = Counter(
    "docai_budget_refusals", "Requests refused over a daily budget", ["kind", "scope"]
)
DOCUMENTS = Counter("docai_documents", "Uploads processed, by final status", ["status"])
DOCUMENT_SECONDS = Histogram(
    "docai_document_seconds", "Time to process one upload", buckets=SECONDS
)
QUEUE_MESSAGES = Gauge(
    "docai_queue_messages",
    "Messages on the processing queue: visible (waiting) or in flight",
    ["state"],
)
MESSAGE_AGE = Histogram(
    "docai_queue_message_age_seconds",
    "A message's age when the worker receives it, from its SentTimestamp",
    buckets=(1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
)


def record_call(message: Any, model: str) -> float | None:
    """Count one model call from its reply; returns its cost. Replies from a LangChain
    cache carry a zero `total_cost` and are not counted."""
    usage = dict(getattr(message, "usage_metadata", None) or {})
    if usage.get("total_cost") == 0:
        return None
    inputs = int(usage.get("input_tokens", 0))
    outputs = int(usage.get("output_tokens", 0))
    cached = int((usage.get("input_token_details") or {}).get("cache_read") or 0)
    name = model.partition(":")[2] or model
    LLM_CALLS.labels(name).inc()
    LLM_TOKENS.labels(name, "input").inc(inputs)
    LLM_TOKENS.labels(name, "cached").inc(cached)
    LLM_TOKENS.labels(name, "output").inc(outputs)
    price = cost(name, inputs, outputs, cached)
    if price is None:
        return None
    LLM_COST.labels(name).inc(float(price))
    return float(price)


def timed_node(node: F) -> F:
    """`node` of the chat graph, timed. `functools.wraps` keeps the signature that
    LangGraph reads to pass the runtime."""

    @functools.wraps(node)
    def timed(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return node(*args, **kwargs)
        finally:
            NODE_SECONDS.labels(node.__name__).observe(time.monotonic() - started)

    return timed  # type: ignore[return-value]


def serve_metrics(port: int) -> None:
    """Metrics on their own port, for processes without an HTTP server."""
    start_http_server(port)
    logger.info("Metrics on port %d", port)


@contextmanager
def evaluation_metrics() -> Iterator[None]:
    """With METRICS_PORT set, an evaluation run serves its metrics while it runs (the
    `evals` job of infrastructure/prometheus), and waits for one more scrape at the end
    so its last values are recorded."""
    port = os.environ.get("METRICS_PORT")
    if not port:
        yield
        return
    serve_metrics(int(port))
    try:
        yield
    finally:
        time.sleep(FINAL_SCRAPE_SECONDS)


FINAL_SCRAPE_SECONDS = 20  # Prometheus scrapes every 15 s
