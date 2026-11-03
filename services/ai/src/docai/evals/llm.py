"""Cached, rate-limited model calls for evaluation runs.

Every reply is stored in SQLite under a hash of the model, parameters and messages, so
re-scoring or re-running an unchanged variant makes no calls. Gemma, which runs on
the free tier (30 requests per minute), gets a client-side rate limiter.
"""

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.rate_limiters import InMemoryRateLimiter

from docai.core.llm import chat_model
from docai.core.settings import Settings
from docai.core.tracing import Tracing

EVALS_DIR = Path(__file__).resolve().parents[3] / "evals"
CACHE_PATH = EVALS_DIR / "cache" / "llm.sqlite"
FREE_TIER_REQUESTS_PER_SECOND = 25 / 60  # below Gemma's 30 requests per minute

Messages = list[tuple[str, str]]


@dataclass(frozen=True)
class Reply:
    text: str
    input_tokens: int
    output_tokens: int
    seconds: float
    cached: bool


class LlmCache:
    def __init__(self, path: Path = CACHE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS replies (key TEXT PRIMARY KEY, model TEXT, text TEXT,"
            " input_tokens INTEGER, output_tokens INTEGER, seconds REAL, created TEXT)"
        )

    def get(self, key: str) -> Reply | None:
        row = self.db.execute(
            "SELECT text, input_tokens, output_tokens, seconds FROM replies WHERE key = ?",
            (key,),
        ).fetchone()
        return Reply(row[0], row[1], row[2], row[3], cached=True) if row else None

    def put(self, key: str, model: str, reply: Reply) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO replies VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                key,
                model,
                reply.text,
                reply.input_tokens,
                reply.output_tokens,
                reply.seconds,
            ),
        )
        self.db.commit()


def cache_key(spec: str, messages: Messages, params: dict[str, Any]) -> str:
    payload = json.dumps(
        {"model": spec, "params": params, "messages": messages}, sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class Models(Protocol):
    """What the runner and judge need; EvalModels in production, fakes in tests."""

    def is_cached(self, spec: str, messages: Messages, **params: Any) -> bool: ...

    def call(
        self,
        spec: str,
        messages: Messages,
        *,
        run_name: str,
        metadata: dict[str, str],
        **params: Any,
    ) -> Reply: ...


class EvalModels:
    def __init__(self, settings: Settings, tracing: Tracing, cache: LlmCache) -> None:
        self.settings, self.tracing, self.cache = settings, tracing, cache
        self.models: dict[str, BaseChatModel] = {}

    def model(self, spec: str, params: dict[str, Any]) -> BaseChatModel:
        key = json.dumps([spec, params], sort_keys=True)
        if key not in self.models:
            extra: dict[str, Any] = {"max_retries": 6}
            if "gemma" in spec:
                extra["rate_limiter"] = InMemoryRateLimiter(
                    requests_per_second=FREE_TIER_REQUESTS_PER_SECOND,
                    check_every_n_seconds=0.1,
                )
            # The evaluation harness calls Gemini and Gemma directly, gateway or not.
            self.models[key] = chat_model(
                spec, self.settings, direct=True, **params, **extra
            )
        return self.models[key]

    def is_cached(self, spec: str, messages: Messages, **params: Any) -> bool:
        return self.cache.get(cache_key(spec, messages, params)) is not None

    def call(
        self,
        spec: str,
        messages: Messages,
        *,
        run_name: str,
        metadata: dict[str, str],
        **params: Any,
    ) -> Reply:
        key = cache_key(spec, messages, params)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        started = time.monotonic()
        response = self.model(spec, params).invoke(
            messages,
            config={
                "callbacks": self.tracing.callbacks,
                "run_name": run_name,
                "metadata": metadata,
            },
        )
        usage: dict[str, Any] = dict(response.usage_metadata or {})
        reply = Reply(
            text=response.text,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            seconds=round(time.monotonic() - started, 3),
            cached=False,
        )
        self.cache.put(key, spec, reply)
        return reply
