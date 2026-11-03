"""The LLM gateway's client (Stage 10b): what it sends, and how refusals are read."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
import yaml
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage
from langchain_core.outputs import LLMResult
from langchain_google_genai import ChatGoogleGenerativeAI

from docai.agent.models import AgentModels, Calls, Usage
from docai.core import gateway
from docai.core.gateway import (
    GatewayChat,
    budget_refusal,
    for_user,
    gateway_chat,
    next_reset,
)
from docai.core.llm import chat_model
from docai.core.prices import cost
from docai.core.settings import Settings
from docai.core.tracing import setup_tracing
from docai.ingest.models import IngestModels

LITELLM = Path(__file__).resolve().parents[3] / "infrastructure" / "litellm"
IMAGE = {"type": "image", "base64": "iVBORw0KGgo=", "mime_type": "image/png"}


@pytest.fixture
def gateway_settings(settings: Settings) -> Settings:
    return with_values(
        settings, llm_gateway_url="http://llm-gateway:4000", llm_gateway_key="sk-x"
    )


def with_values(settings: Settings, **values: str) -> Settings:
    return Settings(**{**settings.model_dump(), **values})


def payload(model: GatewayChat, content: Any) -> dict[str, Any]:
    result: dict[str, Any] = model._get_request_payload([HumanMessage(content=content)])
    return result


def test_with_the_gateway_set_models_go_through_it(gateway_settings: Settings) -> None:
    model = chat_model(
        gateway_settings.fast_model, gateway_settings, max_tokens=600, max_retries=3
    )

    assert isinstance(model, GatewayChat)
    assert model.model_name == "gemini-test-fast"  # the spec without its provider
    assert str(model.openai_api_base) == "http://llm-gateway:4000/v1"
    assert model.max_retries == 0  # the gateway retries, not the client
    assert payload(model, "Hi")["max_completion_tokens"] == 600


def test_direct_models_and_an_unset_gateway_call_gemini(
    settings: Settings, gateway_settings: Settings
) -> None:
    assert isinstance(chat_model(settings.chat_model, settings), ChatGoogleGenerativeAI)
    direct = chat_model(gateway_settings.chat_model, gateway_settings, direct=True)
    assert isinstance(direct, ChatGoogleGenerativeAI)


def test_a_gateway_url_without_its_key_is_an_error(settings: Settings) -> None:
    broken = with_values(settings, llm_gateway_url="http://gw:4000")
    with pytest.raises(ValueError, match="LLM_GATEWAY_KEY"):
        chat_model(settings.chat_model, broken)


def test_images_keep_their_resolution_as_detail() -> None:
    model = gateway_chat("google_genai:gemini-test", "http://gw:4000", "sk-x")
    low = dict(IMAGE, media_resolution="MEDIA_RESOLUTION_LOW")

    parts = payload(model, [{"type": "text", "text": "Read it."}, low, IMAGE])[
        "messages"
    ][0]["content"]

    assert parts[0] == {"type": "text", "text": "Read it."}
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,iVBORw0KGgo=", "detail": "low"},
    }
    assert "detail" not in parts[2]["image_url"]  # default resolution: no detail


def test_each_call_names_its_end_user() -> None:
    model = gateway_chat("google_genai:gemini-test", "http://gw:4000", "sk-x")

    named = for_user(model, "a1b2c3")

    assert payload(named, "Hi")["user"] == "a1b2c3"
    assert "user" not in payload(model, "Hi")  # the shared model is unchanged
    direct = ChatGoogleGenerativeAI(model="gemini-test", api_key="k")
    assert for_user(direct, "a1b2c3") is direct


def litellm_error(status: int, message: str) -> openai.APIStatusError:
    response = httpx2.Response(
        status, request=httpx2.Request("POST", "http://gw:4000/v1/chat/completions")
    )
    return openai.APIStatusError(
        f"Error code: {status} - {{'error': {{'message': '{message}'}}}}",
        response=response,
        body=None,
    )


def test_budget_refusals_are_told_apart() -> None:
    user = litellm_error(
        422, "ExceededBudget: End User=a1b2 over budget. Spend=0.6, Budget=0.5"
    )
    every = litellm_error(
        422, "Budget has been exceeded! Current cost: 5.2, Max budget: 5.0"
    )

    refused = budget_refusal(user)
    assert refused is not None and refused.scope == "user"
    assert refused.resets_at == next_reset()
    assert "your AI usage" in str(refused)
    found = budget_refusal(every)
    assert found is not None and found.scope == "global"
    assert budget_refusal(litellm_error(429, "Rate limit reached")) is None
    assert budget_refusal(ValueError("unrelated")) is None


def test_a_wrapped_refusal_is_found() -> None:
    """LangChain's Gemini client chains the HTTP error under its own."""
    try:
        try:
            raise litellm_error(422, "ExceededBudget: End User=a1b2 over budget.")
        except openai.APIStatusError as error:
            raise RuntimeError("Error calling model 'gemini-test'") from error
    except RuntimeError as wrapped:
        found = budget_refusal(wrapped)
    assert found is not None and found.scope == "user"


def test_daily_budgets_reset_at_midnight_utc() -> None:
    assert next_reset(datetime(2026, 10, 22, 23, 59, tzinfo=UTC)) == datetime(
        2026, 10, 23, tzinfo=UTC
    )


def test_the_drill_config_differs_only_in_the_failing_primary() -> None:
    config = yaml.safe_load((LITELLM / "config.yaml").read_text())
    drill = yaml.safe_load((LITELLM / "drill-primary-down.yaml").read_text())

    primary = drill["model_list"][0]["litellm_params"]
    assert primary.pop("mock_response") == "litellm.RateLimitError"
    assert drill == config
    assert config["router_settings"]["fallbacks"] == [
        {"gemini-3.1-flash-lite": ["gemini-3.5-flash-lite"]}
    ]


# Against a fake gateway that answers as LiteLLM does when the fallback answered ----------

ANSWERED = "gemini-3.5-flash-lite"


def chunk(**fields: Any) -> str:
    body = {"id": "c", "object": "chat.completion.chunk", "created": 0}
    return f"data: {json.dumps(body | {'model': ANSWERED} | fields)}\n\n"


def answer(request: httpx2.Request) -> httpx2.Response:
    body = json.loads(request.content)
    usage = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}
    if body.get("stream"):
        stream = "".join(
            [
                chunk(choices=[{"index": 0, "delta": {"content": "Total "}}]),
                chunk(
                    choices=[
                        {
                            "index": 0,
                            "delta": {"content": "1,230.00 PLN"},
                            "finish_reason": "stop",
                        }
                    ]
                ),
                chunk(choices=[], usage=usage),
                "data: [DONE]\n\n",
            ]
        )
        return httpx2.Response(
            200, text=stream, headers={"content-type": "text/event-stream"}
        )
    message = {"role": "assistant", "content": "Total 1,230.00 PLN"}
    return httpx2.Response(
        200,
        json={
            "id": "c",
            "object": "chat.completion",
            "created": 0,
            "model": ANSWERED,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": usage,
        },
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """The request bodies the gateway receives."""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return answer(request)

    real = gateway.gateway_chat

    def faked(*args: Any, **kwargs: Any) -> GatewayChat:
        client = httpx2.Client(transport=httpx2.MockTransport(handler))
        return real(*args, http_client=client, **kwargs)

    monkeypatch.setattr("docai.core.llm.gateway_chat", faked)
    yield bodies


class Answers(BaseCallbackHandler):
    def __init__(self) -> None:
        self.models: list[Any] = []

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        self.models.append((response.llm_output or {}).get("model_name"))


def test_chat_calls_name_the_user_and_are_priced_as_the_model_that_answered(
    gateway_settings: Settings, sent: list[dict[str, Any]]
) -> None:
    calls = Calls(AgentModels(gateway_settings), end_user="a1b2c3")

    assert calls.text([HumanMessage("Total?")], run_name="generate").endswith("PLN")
    assert sent[0]["user"] == "a1b2c3" and sent[0]["model"] == "gemini-test-chat"

    usage, answers = Usage(), Answers()
    model = chat_model(gateway_settings.chat_model, gateway_settings)
    # Streamed, as every call in the chat graph is: Langfuse reads the answering model
    # from llm_output, which a stream doesn't carry by itself.
    streaming = model.model_copy(update={"streaming": True})
    streaming.invoke([HumanMessage("Total?")], config={"callbacks": [usage, answers]})

    assert sent[-1]["stream"] is True
    assert answers.models == [ANSWERED]
    assert usage.cost == float(cost(ANSWERED, 1000, 100) or Decimal(0))


def test_ingestion_calls_name_the_owner_and_record_the_answering_model(
    gateway_settings: Settings, sent: list[dict[str, Any]]
) -> None:
    models = IngestModels(gateway_settings, setup_tracing(gateway_settings), None)

    reply = models.call(
        "google_genai:gemini-3.1-flash-lite",
        [HumanMessage("Extract.")],
        max_tokens=100,
        run_name="extract",
        metadata={"langfuse_user_id": "a1b2c3"},
    )

    assert sent[0]["user"] == "a1b2c3"
    assert reply.usage.model == f"google_genai:{ANSWERED}"
    assert reply.usage.cost_usd == cost(ANSWERED, 1000, 100)
