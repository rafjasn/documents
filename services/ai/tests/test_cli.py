import os

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from docai import cli
from docai.core.settings import Settings


def test_smoke_succeeds_with_tracing_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("CHAT_MODEL", "google_genai:gemini-test-chat")
    monkeypatch.setenv("FAST_MODEL", "google_genai:gemini-test-fast")
    requested: list[str] = []

    def fake_chat_model(spec: str, settings: Settings) -> GenericFakeChatModel:
        requested.append(spec)
        return GenericFakeChatModel(messages=iter([AIMessage(content="Received.")]))

    monkeypatch.setattr(cli, "chat_model", fake_chat_model)

    assert cli.main(["smoke", "--model", "chat"]) == 0
    output = capsys.readouterr().out
    assert requested == ["google_genai:gemini-test-chat"]
    assert "reply: Received." in output
    assert "trace: tracing off" in output


@pytest.mark.integration
@pytest.mark.skipif(
    not all(
        os.environ.get(name) for name in ("GEMINI_API_KEY", "FAST_MODEL", "CHAT_MODEL")
    ),
    reason="needs GEMINI_API_KEY, FAST_MODEL and CHAT_MODEL",
)
def test_real_gemini_call_with_tracing_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    assert cli.main(["smoke"]) == 0
    output = capsys.readouterr().out
    assert "reply: " in output
    assert "trace: tracing off" in output


def test_graph_prints_the_compiled_chat_graph(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["graph"]) == 0

    diagram = capsys.readouterr().out
    assert "graph TD;" in diagram
    assert "__start__ --> route_intent;" in diagram
    assert "check_grounding -.-> generate;" in diagram  # the one regeneration
