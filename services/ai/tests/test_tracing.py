import gzip
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from docai.core.masking import Masker
from docai.core.settings import Settings
from docai.core.tracing import ExperimentRecord, Tracing, otel_masking, setup_tracing


def fake_model() -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter([AIMessage(content="received")]))


def with_langfuse(
    settings: Settings, base_url: str, public_key: str = "pk-lf-test"
) -> Settings:
    # Langfuse keeps one client per public key, so a test that needs its own client
    # passes its own key.
    return settings.model_copy(
        update={
            "langfuse_public_key": public_key,
            "langfuse_secret_key": SecretStr("sk-lf-test"),
            "langfuse_base_url": base_url,
        }
    )


def test_tracing_is_off_without_keys(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="docai.core.tracing"):
        tracing = setup_tracing(settings)

    assert not tracing.enabled
    assert tracing.callbacks == []
    assert tracing.last_trace_url() is None
    tracing.flush()
    assert "Langfuse tracing off: no LANGFUSE_* variables are set" in caplog.text


def test_partial_keys_warn_without_leaking_values(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    partial = settings.model_copy(
        update={"langfuse_secret_key": SecretStr("sk-lf-secret")}
    )
    with caplog.at_level(logging.INFO, logger="docai.core.tracing"):
        tracing = setup_tracing(partial)

    assert not tracing.enabled
    assert "LANGFUSE_PUBLIC_KEY, LANGFUSE_BASE_URL not set" in caplog.text
    assert "sk-lf-secret" not in caplog.text


def test_model_call_succeeds_with_tracing_off(settings: Settings) -> None:
    tracing = setup_tracing(settings)
    reply = fake_model().invoke("hello", config={"callbacks": tracing.callbacks})
    assert reply.text == "received"


def test_unreachable_langfuse_does_not_break_model_calls(settings: Settings) -> None:
    # Port 9 (discard) refuses connections, standing in for a Langfuse outage.
    tracing = setup_tracing(with_langfuse(settings, "http://127.0.0.1:9"))
    assert tracing.enabled and tracing.handler and tracing.client

    reply = fake_model().invoke("hello", config={"callbacks": tracing.callbacks})
    tracing.flush()

    assert reply.text == "received"
    # The trace was recorded; only the URL lookup, which needs the API, fails softly.
    assert tracing.handler.last_trace_id
    assert tracing.last_trace_url() is None
    tracing.client.shutdown()


def test_empty_tracing_has_no_callbacks() -> None:
    assert Tracing().callbacks == []


class FakeDatasetItem:
    def __init__(self, id_: str, metadata: dict[str, object]) -> None:
        self.id, self.metadata = id_, metadata


class FakeLangfuse:
    """Records dataset calls and runs the experiment task and evaluator locally."""

    def __init__(self) -> None:
        self.items: dict[str, FakeDatasetItem] = {}
        self.scores: list[tuple[str, object, object]] = []

    def create_dataset(self, *, name: str, description: str) -> None:
        self.dataset = name

    def create_dataset_item(
        self, *, dataset_name: str, id: str, metadata: dict[str, object], **_: object
    ) -> None:
        self.items[id] = FakeDatasetItem(id, metadata)

    def get_dataset(self, name: str) -> object:
        return type("Dataset", (), {"items": list(self.items.values())})()

    def run_experiment(
        self,
        *,
        data: list[FakeDatasetItem],
        task: Any,
        evaluators: list[Any],
        **_: object,
    ) -> None:
        for item in data:
            output = task(item=item)
            evaluation = evaluators[0](
                input=None, output=output, expected_output=None, metadata=item.metadata
            )
            self.scores.append((output, evaluation.value, evaluation.comment))

    def flush(self) -> None:
        pass


def test_finished_runs_are_logged_as_experiments_without_model_calls() -> None:
    fake = FakeLangfuse()
    tracing = Tracing(handler=None, client=fake)  # type: ignore[arg-type]
    record = ExperimentRecord(
        id="q1",
        input={"question": "?"},
        expected_output={"answer": "42"},
        metadata={"split": "dev"},
        output="It is 42.",
        score=1.0,
        comment="decided by rule",
    )
    assert tracing.log_experiment(
        dataset="d", run_name="r", description="x", records=[record], metadata={}
    )
    assert fake.items["q1"].metadata == {"split": "dev", "golden_id": "q1"}
    assert fake.scores == [("It is 42.", 1.0, "decided by rule")]


def test_experiment_logging_is_skipped_or_contained() -> None:
    assert not Tracing().log_experiment(
        dataset="d", run_name="r", description="x", records=[], metadata={}
    )
    broken = Tracing(handler=None, client=object())  # type: ignore[arg-type]
    assert not broken.log_experiment(
        dataset="d", run_name="r", description="x", records=[], metadata={}
    )


class Collector(BaseHTTPRequestHandler):
    """Stands in for Langfuse: keeps every exported request body, decompressed."""

    bodies: list[bytes] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        Collector.bodies.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:
        pass


def test_exported_traces_carry_masked_identifiers_only(settings: Settings) -> None:
    server = HTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Collector.bodies = []
    base_url = f"http://127.0.0.1:{server.server_port}"
    masked = settings.model_copy(update={"masking_key": SecretStr("k")})
    tracing = setup_tracing(with_langfuse(masked, base_url, "pk-lf-masking"))
    try:
        fake_model().invoke(
            "Sprzedawca NIP 262-771-21-24, konto PL00 1665 5996 1140 2723 6734 1861, "
            "do zapłaty 22 212,00 PLN",
            config={"callbacks": tracing.callbacks},
        )
        tracing.flush()
    finally:
        assert tracing.client is not None
        tracing.client.shutdown()
        server.shutdown()

    exported = b"".join(Collector.bodies)
    assert b"[NIP:" in exported and b"[ACCOUNT:" in exported
    assert b"262-771-21-24" not in exported
    assert b"1665 5996" not in exported
    assert "22 212,00 PLN".encode() in exported


def test_span_attributes_are_patched_only_where_masking_changes_them() -> None:
    from langfuse.types import MaskOtelSpansParams, OtelSpanData, OtelSpanIdentifier

    span = OtelSpanIdentifier(trace_id="0" * 32, span_id="1" * 16)
    params = MaskOtelSpansParams(
        spans={
            span: OtelSpanData(
                trace_id="0" * 32,
                span_id="1" * 16,
                parent_span_id=None,
                name="POST /v1/chat/stream",
                instrumentation_scope_name="opentelemetry.instrumentation.fastapi",
                instrumentation_scope_version=None,
                attributes={
                    "http.route": "/v1/chat/stream",
                    "note": "NIP 262-771-21-24",
                    "tags": ("a@example.pl", "plain"),
                    "size": 3,
                },
                resource_attributes={},
            )
        }
    )
    result = otel_masking(Masker(b"k"))(params=params)

    patch = result.span_patches[span]
    assert patch is not None
    assert set(patch.set_attributes) == {"note", "tags"}
    assert "262-771-21-24" not in str(patch.set_attributes["note"])


def test_dataset_items_are_masked_before_they_are_written() -> None:
    fake = FakeLangfuse()
    stored: dict[str, Any] = {}
    original = fake.create_dataset_item

    def keep(**kwargs: Any) -> None:
        stored.update(kwargs)
        original(**kwargs)

    fake.create_dataset_item = keep  # type: ignore[method-assign]
    tracing = Tracing(client=fake, masker=Masker(b"k"))  # type: ignore[arg-type]
    record = ExperimentRecord(
        id="q1",
        input={"question": "What is the NIP of Herba?"},
        expected_output={"answer": "262-771-21-24"},
        metadata={"split": "dev"},
        output="NIP 262-771-21-24",
        score=1.0,
        comment="",
    )
    assert tracing.log_experiment(
        dataset="d", run_name="r", description="x", records=[record], metadata={}
    )
    assert "262-771-21-24" not in str(stored["expected_output"])
    assert "262-771-21-24" not in fake.scores[0][0]
