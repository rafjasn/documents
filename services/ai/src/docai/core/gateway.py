"""The LLM gateway (Stage 10b): LiteLLM's OpenAI-compatible router between the services
and Gemini, holding the spend limits and the fallback model.

With LLM_GATEWAY_URL set, `docai.core.llm.chat_model` builds a `GatewayChat` instead of
a Gemini client. It asks for the configured model without its provider
(`google_genai:gemini-3.1-flash-lite` asks for `gemini-3.1-flash-lite`), authenticates
with the services' virtual key (LLM_GATEWAY_KEY), and doesn't retry: the gateway
retries, then falls back (infrastructure/litellm/config.yaml). Unset, calls go straight
to Gemini.

Each call names its end user, the hashed user ID the traces use, so the gateway keeps a
daily budget per user; a global daily budget stops every call. Each call also carries
the trace context, so the gateway's span joins the request's trace. Over either, the gateway
refuses the call, and `budget_refusal` turns its error into `BudgetExceeded`.
"""

from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal

import httpx
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI

from docai.core.telemetry import FALLBACKS, trace_headers

USER_BUDGET_ID = "daily-user"  # litellm_settings.max_end_user_budget_id
BUDGET_DURATION = "1d"  # LiteLLM resets daily budgets at midnight UTC
# The client waits for the gateway's 30-second attempts, a retry and the fallback.
TIMEOUT_SECONDS = 100
# Gemini's per-image resolution levels, as the OpenAI format's `detail`.
DETAIL = {
    "MEDIA_RESOLUTION_LOW": "low",
    "MEDIA_RESOLUTION_MEDIUM": "medium",
    "MEDIA_RESOLUTION_HIGH": "high",
}


def gateway_model(spec: str) -> str:
    """The gateway's name for a configured model: the spec without its provider."""
    return spec.partition(":")[2] or spec


def image_part(block: dict[str, Any]) -> dict[str, Any]:
    """A LangChain image block as an OpenAI `image_url` part, keeping its resolution:
    LangChain's own conversion drops it, and the gateway maps `detail` back to Gemini's
    media resolution."""
    image: dict[str, Any] = {
        "url": block.get("url") or f"data:{block['mime_type']};base64,{block['base64']}"
    }
    if detail := DETAIL.get(str(block.get("media_resolution"))):
        image["detail"] = detail
    return {"type": "image_url", "image_url": image}


def with_image_parts(message: BaseMessage) -> BaseMessage:
    if isinstance(message.content, str) or not any(
        isinstance(part, dict) and part.get("type") == "image"
        for part in message.content
    ):
        return message
    content = [
        image_part(part)
        if isinstance(part, dict) and part.get("type") == "image"
        else part
        for part in message.content
    ]
    return message.model_copy(update={"content": content})


def with_answering_model(result: ChatResult, requested: str) -> ChatResult:
    """A streamed result carries no `llm_output`, from which Langfuse reads the model
    that answered; without it the trace would name the requested model even when the
    fallback answered. The model is in the last chunk's metadata. A call the fallback
    answered is counted."""
    if result.llm_output is None and result.generations:
        model = result.generations[0].message.response_metadata.get("model_name")
        if model:
            result.llm_output = {"model_name": model}
    answered = (result.llm_output or {}).get("model_name")
    if answered and answered != requested:
        FALLBACKS.labels(requested, answered).inc()
    return result


class GatewayChat(ChatOpenAI):
    """ChatOpenAI against the gateway, naming the end user of each call and reporting
    the model that answered, streamed or not."""

    end_user: str | None = None

    def _generate_with_cache(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return with_answering_model(
            super()._generate_with_cache(messages, stop, run_manager, **kwargs),
            self.model_name,
        )

    async def _agenerate_with_cache(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return with_answering_model(
            await super()._agenerate_with_cache(messages, stop, run_manager, **kwargs),
            self.model_name,
        )

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        messages = [
            with_image_parts(m) for m in self._convert_input(input_).to_messages()
        ]
        payload: dict[str, Any] = super()._get_request_payload(
            messages, stop=stop, **kwargs
        )
        if self.end_user:
            payload["user"] = self.end_user
        if headers := trace_headers():
            # The gateway's own span joins the request's trace (Stage 10c).
            payload["extra_headers"] = headers
        return payload


def gateway_chat(
    spec: str, url: str, key: str, *, max_tokens: int | None = None, **kwargs: Any
) -> GatewayChat:
    kwargs.pop("max_retries", None)  # the gateway retries
    kwargs["timeout"] = max(kwargs.get("timeout") or 0, TIMEOUT_SECONDS)
    return GatewayChat(
        model=gateway_model(spec),
        base_url=f"{url.rstrip('/')}/v1",
        api_key=key,
        max_tokens=max_tokens,
        max_retries=0,
        stream_usage=True,
        use_responses_api=False,
        **kwargs,
    )


def for_user(model: Any, end_user: str | None) -> Any:
    """The model, naming `end_user` to the gateway on each call; a direct Gemini client
    is returned as it is."""
    if isinstance(model, GatewayChat) and end_user and model.end_user != end_user:
        return model.model_copy(update={"end_user": end_user})
    return model


# Budget refusals ---------------------------------------------------------------------


class BudgetExceeded(Exception):
    """The gateway refused a call: a user's daily budget, or the global one, is spent."""

    def __init__(self, scope: Literal["user", "global"], resets_at: datetime) -> None:
        self.scope, self.resets_at = scope, resets_at
        who = "your" if scope == "user" else "the service's"
        super().__init__(
            f"The daily limit on {who} AI usage has been reached. "
            f"It resets at {resets_at:%Y-%m-%d %H:%M} UTC."
        )


def next_reset(now: datetime | None = None) -> datetime:
    """When a daily budget resets: the next midnight UTC."""
    now = now or datetime.now(UTC)
    return datetime.combine(now.date() + timedelta(days=1), time(), tzinfo=UTC)


def budget_refusal(error: BaseException | None) -> BudgetExceeded | None:
    """The BudgetExceeded behind a gateway error, if that is what it is.

    LiteLLM answers an exceeded budget with HTTP 422 and a message naming the end user
    for a user's budget ("ExceededBudget: End User=...") and the totals for the global
    one ("Budget has been exceeded!"). Clients wrap the HTTP error differently (OpenAI's
    keeps it, LangChain's Gemini client chains it), so every error in the chain is read.
    """
    seen = 0
    while error is not None and seen < 10:
        message = str(error)
        if "ExceededBudget: End User=" in message:
            return BudgetExceeded("user", next_reset())
        if "Budget has been exceeded" in message:
            return BudgetExceeded("global", next_reset())
        error, seen = error.__cause__ or error.__context__, seen + 1
    return None


# Setup (`docai gateway setup`) -------------------------------------------------------


def setup(url: str, master_key: str, service_key: str, user_budget: float) -> list[str]:
    """Create or update, in the gateway's database, the daily budget every end user
    gets and the services' virtual key. Idempotent."""
    lines = []
    with httpx.Client(
        base_url=url.rstrip("/"),
        headers={"Authorization": f"Bearer {master_key}"},
        timeout=30,
    ) as client:
        budget = {
            "budget_id": USER_BUDGET_ID,
            "max_budget": user_budget,
            "budget_duration": BUDGET_DURATION,
        }
        found = client.post("/budget/info", json={"budgets": [USER_BUDGET_ID]})
        exists = found.status_code == 200 and bool(found.json())
        response = client.post(
            "/budget/update" if exists else "/budget/new", json=budget
        )
        response.raise_for_status()
        lines.append(
            f"per-user budget {USER_BUDGET_ID}: ${user_budget} a day "
            f"({'updated' if exists else 'created'})"
        )

        info = client.get("/key/info", params={"key": service_key})
        if info.status_code == 200:
            lines.append("services' key: already there")
        else:
            client.post(
                "/key/generate",
                json={"key": service_key, "key_alias": "documents-services"},
            ).raise_for_status()
            lines.append("services' key: created")
    return lines
