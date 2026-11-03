"""Service telemetry (Stage 10c): which spans reach Langfuse, the trace context the
gateway gets, and the metrics the dashboard and alerts read."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from docai.api.app import create_app
from docai.core import telemetry
from docai.core.gateway import gateway_chat
from docai.core.masking import Masker
from docai.core.settings import Settings
from docai.core.tracing import otel_masking
from tests.aws_support import Aws
from tests.ingest_support import text_pdf
from tests.service_support import FakeRetriever, fake_services, hit, token
from tests.test_gateway import ANSWERED, gateway_settings, sent  # noqa: F401
from tests.test_service import REQUEST, OverBudget, answering_calls, events
from tests.test_worker import DOC, handle_one, worker


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """Spans this process records, in memory, on a provider of the test's own."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    provider.shutdown()


class Scoped:
    def __init__(self, scope: str, attributes: dict[str, Any] | None = None) -> None:
        self.instrumentation_scope = type("Scope", (), {"name": scope})()
        self.attributes = attributes or {}
        self.name = "span"


@pytest.mark.parametrize(
    ("scope", "exported"),
    [
        ("opentelemetry.instrumentation.fastapi", True),
        ("opentelemetry.instrumentation.httpx", True),
        ("opentelemetry.instrumentation.botocore.dynamodb", True),
        ("docai", True),
        ("langfuse-sdk", True),  # model spans: Langfuse's own filter
        ("opentelemetry.instrumentation.requests", False),
        ("some.library", False),
    ],
)
def test_service_spans_are_exported_beside_the_model_spans(
    scope: str, exported: bool
) -> None:
    assert telemetry.should_export(Scoped(scope)) is exported


def test_the_gateway_gets_the_requests_trace_context() -> None:
    provider = TracerProvider()
    model = gateway_chat("google_genai:gemini-test", "http://gw:4000", "sk-x")

    with provider.get_tracer("test").start_as_current_span("chat") as current:
        payload = model._get_request_payload([HumanMessage("Hi")])
    trace_id = format(current.get_span_context().trace_id, "032x")

    assert payload["extra_headers"]["traceparent"].split("-")[1] == trace_id
    assert "extra_headers" not in model._get_request_payload([HumanMessage("Hi")])


def test_a_call_the_fallback_answered_is_counted(
    gateway_settings: Settings,  # noqa: F811
    sent: list[dict[str, Any]],  # noqa: F811
) -> None:
    from docai.core.llm import chat_model

    labels = {"requested": "gemini-test-chat", "answered": ANSWERED}
    before = value("docai_llm_fallbacks_total", **labels)
    calls_before = value("docai_llm_calls_total", model=ANSWERED)

    chat_model(gateway_settings.chat_model, gateway_settings).invoke("Total?")

    assert value("docai_llm_fallbacks_total", **labels) == before + 1
    assert sent and value("docai_llm_calls_total", model=ANSWERED) >= calls_before


def test_chats_are_counted_by_outcome_and_served_as_metrics() -> None:
    answered = value("docai_chat_requests_total", outcome="answered")
    refused = value("docai_budget_refusals_total", kind="chat", scope="user")
    headers = {"Authorization": f"Bearer {token()}"}
    retriever = FakeRetriever(results=[[hit("doc-1", 2, "Gross total 1,230.00 PLN")]])
    services = fake_services(answering_calls(), retriever)
    with TestClient(create_app(services=services)) as client:
        assert events(
            client.post("/v1/chat/stream", json=REQUEST, headers=headers).text
        )
        text = client.get("/metrics").text
    with TestClient(create_app(services=fake_services(OverBudget({})))) as client:
        client.post("/v1/chat/stream", json=REQUEST, headers=headers)

    assert value("docai_chat_requests_total", outcome="answered") == answered + 1
    assert (
        value("docai_budget_refusals_total", kind="chat", scope="user") == refused + 1
    )
    assert 'docai_http_requests_total{endpoint="/v1/chat/stream",status="200"}' in text
    assert "docai_graph_node_seconds_count" in text


def test_a_key_in_a_url_never_leaves_the_process() -> None:
    from langfuse.types import MaskOtelSpansParams, OtelSpanData, OtelSpanIdentifier

    span = OtelSpanIdentifier(trace_id="0" * 32, span_id="1" * 16)
    url = "https://generativelanguage.googleapis.com/v1beta/models/m:generate?key=AIzaSecret&alt=sse"
    params = MaskOtelSpansParams(
        spans={
            span: OtelSpanData(
                trace_id="0" * 32,
                span_id="1" * 16,
                parent_span_id=None,
                name="POST",
                instrumentation_scope_name="opentelemetry.instrumentation.httpx",
                instrumentation_scope_version=None,
                attributes={"http.url": url},
                resource_attributes={},
            )
        }
    )

    patch = otel_masking(Masker(b"k"))(params=params).span_patches[span]

    assert patch is not None
    assert patch.set_attributes["http.url"].endswith("?key=REDACTED&alt=sse")


def test_an_upload_is_one_span_with_its_age_and_status_counted(
    aws: Aws, spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    monkeypatch.setattr(telemetry, "TRACER", provider.get_tracer("docai"))
    received = value("docai_queue_message_age_seconds_count")
    completed = value("docai_documents_total", status="completed")
    aws.upload(text_pdf(["Faktura"]))
    aws.s3_event()

    handle_one(aws, worker(aws))

    [upload] = [s for s in spans.get_finished_spans() if s.name == "ingest"]
    assert upload.attributes is not None
    assert upload.attributes["document.id"] == DOC
    assert upload.attributes["messaging.receive_count"] == 1
    assert value("docai_queue_message_age_seconds_count") == received + 1
    assert value("docai_documents_total", status="completed") == completed + 1


def test_a_trace_never_starts_at_a_bare_client_call() -> None:
    from opentelemetry.sdk.trace.sampling import ParentBased
    from opentelemetry.trace import SpanKind

    provider = TracerProvider(sampler=ParentBased(telemetry.NoClientRoots()))
    tracer = provider.get_tracer("test")

    poll = tracer.start_span("SQS.ReceiveMessage", kind=SpanKind.CLIENT)
    with tracer.start_as_current_span("ingest") as upload:
        inside = tracer.start_span("S3.GetObject", kind=SpanKind.CLIENT)

    def sampled(span: Any) -> bool:
        return bool(span.get_span_context().trace_flags.sampled)

    assert not sampled(poll)  # the worker's queue poll: no trace of its own
    assert sampled(upload) and sampled(inside)


def test_a_cached_upload_isnt_traced(
    aws: Aws, spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    monkeypatch.setattr(telemetry, "TRACER", provider.get_tracer("docai"))
    aws.upload(text_pdf(["Faktura"]))
    aws.s3_event()
    the_worker = worker(aws)
    monkeypatch.setattr(the_worker.pipeline, "is_cached", lambda data: True)

    handle_one(aws, the_worker)

    assert aws.item() is not None and aws.item()["status"] == "COMPLETED"  # type: ignore[index]
    assert not [s for s in spans.get_finished_spans() if s.name == "ingest"]


GRAFANA = Path(__file__).resolve().parents[3] / "infrastructure" / "grafana"


def test_the_dashboard_and_alerts_query_metrics_that_exist() -> None:
    queries = (GRAFANA / "provisioning" / "alerting" / "rules.yaml").read_text() + (
        GRAFANA / "dashboards" / "documents.json"
    ).read_text()
    used = {
        re.sub(r"_(total|bucket|count|sum)$", "", name)
        for name in re.findall(r"\bdocai_[a-z_]+", queries)
    }
    known = {m.name for m in REGISTRY.collect()}

    assert used and used <= known, used - known
    rules = yaml.safe_load(
        (GRAFANA / "provisioning" / "alerting" / "rules.yaml").read_text()
    )
    uids = {rule["uid"] for rule in rules["groups"][0]["rules"]}
    assert {"error-rate", "chat-latency-p95", "hourly-spend"} <= uids
