"""Model access for the graph: two roles, structured output, a per-request call budget.

CHAT_MODEL writes answers; FAST_MODEL routes, plans, grades and checks grounding. Both
default to gemini-3.1-flash-lite. An optional LangChain cache makes evaluation reruns
free; the service runs without one.
"""

import threading
from collections.abc import Sequence
from typing import Any, Literal, TypeVar

from langchain_core.caches import BaseCache
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.runnables import Runnable, RunnableConfig, RunnablePassthrough
from pydantic import BaseModel

from docai.agent.prompt_store import Rendered
from docai.core.gateway import for_user
from docai.core.llm import chat_model
from docai.core.prices import cost
from docai.core.settings import Settings
from docai.core.telemetry import record_call

Role = Literal["chat", "fast"]
T = TypeVar("T", bound=BaseModel)
MAX_CALLS = 10
# Input tokens one request may send, counted before each call: a question takes about
# 13,000 over four calls, so this stops runaway histories or sources, not real use.
MAX_INPUT_TOKENS = 40_000
CHARS_PER_TOKEN = 3  # a cautious estimate for Polish and English text


class CallBudgetExceeded(RuntimeError):
    pass


class AgentModels:
    def __init__(self, settings: Settings, cache: BaseCache | None = None) -> None:
        self.settings, self.cache = settings, cache
        self.specs: dict[Role, str] = {
            "chat": settings.chat_model,
            "fast": settings.fast_model,
        }
        self._models: dict[tuple[Role, int], BaseChatModel] = {}
        self._lock = threading.Lock()

    def model(self, role: Role, max_tokens: int) -> BaseChatModel:
        with self._lock:
            if (role, max_tokens) not in self._models:
                extra: dict[str, Any] = {"cache": self.cache} if self.cache else {}
                self._models[role, max_tokens] = chat_model(
                    self.specs[role],
                    self.settings,
                    max_tokens=max_tokens,
                    max_retries=3,  # direct calls only; through the gateway, none
                    timeout=60,
                    **extra,
                )
            return self._models[role, max_tokens]


class Usage(BaseCallbackHandler):
    """Tokens, calls and cost of one request, from every model call in it. Each call is
    also counted in the service's metrics."""

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = self.output_tokens = 0
        self.cost: float | None = 0.0
        self._lock = threading.Lock()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        with self._lock:
            self.calls += 1
            for generations in response.generations:
                for generation in generations:
                    if not isinstance(generation, ChatGeneration):
                        continue
                    message = generation.message
                    usage = getattr(message, "usage_metadata", None) or {}
                    model = str(message.response_metadata.get("model_name") or "")
                    inputs = int(usage.get("input_tokens", 0))
                    outputs = int(usage.get("output_tokens", 0))
                    cached = int(
                        (usage.get("input_token_details") or {}).get("cache_read") or 0
                    )
                    record_call(message, model)
                    self.input_tokens += inputs
                    self.output_tokens += outputs
                    price = cost(model, inputs, outputs, cached)
                    if price is None or self.cost is None:
                        self.cost = None
                    else:
                        self.cost += float(price)


def with_prompt(
    model: Runnable[Any, Any], prompt: Rendered | None
) -> Runnable[Any, Any]:
    """The model under a step that carries its Langfuse prompt: Langfuse's handler
    links a generation to a prompt registered on a run above it, so the trace shows the
    version that produced each call."""
    if prompt is None or prompt.source is None:
        return model
    step: Runnable[Any, Any] = RunnablePassthrough().with_config(
        run_name=f"prompt {prompt.label}",
        metadata={"langfuse_prompt": prompt.source},
    )
    return step | model


def call_config(run_name: str, prompt: Rendered | None) -> RunnableConfig:
    metadata = {"prompt": prompt.label} if prompt else {}
    return {"run_name": run_name, "metadata": metadata}


class Calls:
    """The calls one request makes, capped at MAX_CALLS and MAX_INPUT_TOKENS (estimated
    from the prompt before the call, so the call that would exceed them isn't made).

    `end_user`, the hashed user ID, is named to the LLM gateway on each call, for the
    user's daily budget (docai.core.gateway)."""

    def __init__(
        self,
        models: AgentModels,
        limit: int = MAX_CALLS,
        input_tokens: int = MAX_INPUT_TOKENS,
        end_user: str | None = None,
    ) -> None:
        self.models, self.limit, self.made = models, limit, 0
        self.input_budget, self.input_spent = input_tokens, 0
        self.end_user = end_user
        self._lock = threading.Lock()

    def _model(self, role: Role, max_tokens: int) -> BaseChatModel:
        model: BaseChatModel = for_user(
            self.models.model(role, max_tokens), self.end_user
        )
        return model

    def _spend(self, messages: Sequence[BaseMessage]) -> None:
        estimate = sum(len(m.text) for m in messages) // CHARS_PER_TOKEN + 1
        with self._lock:
            if self.made >= self.limit:
                raise CallBudgetExceeded(f"more than {self.limit} model calls")
            if self.input_spent + estimate > self.input_budget:
                raise CallBudgetExceeded(
                    f"more than {self.input_budget} input tokens in one request"
                )
            self.made += 1
            self.input_spent += estimate

    def structured(
        self,
        schema: type[T],
        messages: Sequence[BaseMessage],
        *,
        run_name: str,
        max_tokens: int = 600,
        prompt: Rendered | None = None,
    ) -> T:
        self._spend(messages)
        model = self._model("fast", max_tokens).with_structured_output(
            schema, method="json_schema"
        )
        result = with_prompt(model, prompt).invoke(
            list(messages), config=call_config(run_name, prompt)
        )
        if not isinstance(result, schema):
            raise ValueError(f"{run_name}: no {schema.__name__} in the reply")
        return result

    def text(
        self,
        messages: Sequence[BaseMessage],
        *,
        run_name: str,
        max_tokens: int = 900,
        prompt: Rendered | None = None,
    ) -> str:
        self._spend(messages)
        reply = with_prompt(self._model("chat", max_tokens), prompt).invoke(
            list(messages), config=call_config(run_name, prompt)
        )
        return str(reply.text).strip()
