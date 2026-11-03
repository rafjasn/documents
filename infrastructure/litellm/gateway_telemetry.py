"""The gateway's OpenTelemetry callback (Stage 10c): LiteLLM's own, exporting only what a
request's trace needs.

LiteLLM records a span for every database read, key check, router step and spend write,
about nine for each model call, and its `litellm_request` span carries the call's token
counts and cost, which Langfuse would price a second time beside the services' own
generation. This exporter keeps the two spans that show the gateway's part, the request
as the gateway received it and the call it made, with only the attributes below: the
model asked for and the one that answered, the end user (already hashed), streaming,
status and errors. Cost, key hashes, budgets and the caller's address stay out.

Loaded by `callbacks` in config.yaml; the entrypoint sets OTEL_ENDPOINT and OTEL_HEADERS
when the Langfuse keys are set, and turns the SDK off otherwise.
"""

import os
from collections.abc import Sequence

from litellm.integrations.opentelemetry import OpenTelemetry, OpenTelemetryConfig
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

SPANS = {"Received Proxy Server Request", "litellm_request"}
ATTRIBUTES = {
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.system",
    "litellm.model_group",
    "litellm.provider.model",
    "llm.is_streaming",
    "llm.user",
    "http.route",
    "http.response.status_code",
    "error.type",
    "exception.type",
}


def trimmed(span: ReadableSpan) -> ReadableSpan:
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes={k: v for k, v in (span.attributes or {}).items() if k in ATTRIBUTES},
        events=(),  # message events, if any were recorded
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class GatewaySpans(SpanExporter):
    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        kept = [trimmed(span) for span in spans if span.name in SPANS]
        return self.inner.export(kept) if kept else SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self.inner.force_flush(timeout_millis)


def exporter() -> SpanExporter | str:
    endpoint = os.environ.get("OTEL_ENDPOINT")
    if not endpoint:
        return "console"  # unused: without the keys the SDK is off
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    name, _, value = os.environ.get("OTEL_HEADERS", "").partition("=")
    return GatewaySpans(OTLPSpanExporter(endpoint=endpoint, headers={name: value}))


proxy_handler_instance = OpenTelemetry(config=OpenTelemetryConfig(exporter=exporter()))
