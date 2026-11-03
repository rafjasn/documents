"""Cached, traced model calls for ingestion: figure captions and document extraction.

Each reply is cached on disk (INGEST_CACHE_DIR) under a hash of the model, parameters
and messages, images included, so reprocessing an unchanged file makes no calls. Through
the LLM gateway, each call names the document's owner (the hashed user ID of the trace
metadata) for the user's daily budget; the cache key doesn't depend on the route.
"""

import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

from docai.core.gateway import for_user
from docai.core.llm import answered_by, chat_model
from docai.core.prices import cost
from docai.core.settings import CoreSettings
from docai.core.telemetry import record_call
from docai.core.tracing import Tracing
from docai.ingest.cache import DiskCache


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int
    output_tokens: int
    seconds: float
    cached: bool  # served from our disk cache: no call made now
    cache_read_tokens: int = 0  # input tokens Gemini served from its prefix cache

    @property
    def cost_usd(self) -> Decimal | None:
        """What the call cost when it was made, cached or not."""
        return cost(
            self.model, self.input_tokens, self.output_tokens, self.cache_read_tokens
        )


@dataclass(frozen=True)
class Reply:
    text: str
    usage: Usage


class Models(Protocol):
    """What the pipeline needs; IngestModels in production, fakes in tests."""

    def call(
        self,
        spec: str,
        messages: list[BaseMessage],
        *,
        max_tokens: int,
        run_name: str,
        metadata: dict[str, Any],
        schema: type[BaseModel] | None = None,
    ) -> Reply: ...


def cache_key(spec: str, messages: list[BaseMessage], params: dict[str, Any]) -> str:
    payload = {
        "model": spec,
        "params": params,
        "messages": [{"type": m.type, "content": m.content} for m in messages],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class IngestModels:
    def __init__(
        self, settings: CoreSettings, tracing: Tracing, cache: DiskCache | None
    ) -> None:
        self.settings, self.tracing, self.cache = settings, tracing, cache
        self._models: dict[tuple[str, int], BaseChatModel] = {}

    def _model(self, spec: str, max_tokens: int) -> BaseChatModel:
        if (spec, max_tokens) not in self._models:
            self._models[spec, max_tokens] = chat_model(
                spec,
                self.settings,
                max_tokens=max_tokens,
                max_retries=4,  # direct calls only; through the gateway, none
                timeout=120,
            )
        return self._models[spec, max_tokens]

    def call(
        self,
        spec: str,
        messages: list[BaseMessage],
        *,
        max_tokens: int,
        run_name: str,
        metadata: dict[str, Any],
        schema: type[BaseModel] | None = None,
    ) -> Reply:
        params = {
            "max_tokens": max_tokens,
            "schema": schema.model_json_schema() if schema else None,
        }
        key = cache_key(spec, messages, params)
        if self.cache is not None and (hit := self.cache.get_reply(key)) is not None:
            return Reply(
                hit["text"],
                Usage(
                    hit.get("model", spec),
                    hit["input_tokens"],
                    hit["output_tokens"],
                    hit["seconds"],
                    cached=True,
                    cache_read_tokens=hit.get("cache_read_tokens", 0),
                ),
            )
        model = for_user(
            self._model(spec, max_tokens), metadata.get("langfuse_user_id")
        )
        config: Any = {
            "callbacks": self.tracing.callbacks,
            "run_name": run_name,
            "metadata": metadata,
        }
        started = time.monotonic()
        if schema is None:
            raw = model.invoke(messages, config=config)
        else:
            structured = model.with_structured_output(
                schema, method="json_schema", include_raw=True
            ).invoke(messages, config=config)
            assert isinstance(structured, dict)
            if structured["parsing_error"] is not None:
                raise ValueError(
                    f"{run_name}: the model returned invalid structured output: "
                    f"{structured['parsing_error']}"
                )
            raw = structured["raw"]
        assert isinstance(raw, AIMessage)
        usage: dict[str, Any] = dict(raw.usage_metadata or {})
        reply = Reply(
            raw.text,
            Usage(
                answered_by(spec, raw),
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
                round(time.monotonic() - started, 3),
                cached=False,
                cache_read_tokens=int(
                    (usage.get("input_token_details") or {}).get("cache_read") or 0
                ),
            ),
        )
        record_call(raw, reply.usage.model)
        if self.cache is not None:
            self.cache.put_reply(
                key,
                {
                    "model": reply.usage.model,
                    "text": reply.text,
                    "input_tokens": reply.usage.input_tokens,
                    "output_tokens": reply.usage.output_tokens,
                    "cache_read_tokens": reply.usage.cache_read_tokens,
                    "seconds": reply.usage.seconds,
                },
            )
        return reply
