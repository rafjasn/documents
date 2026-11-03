"""Fakes for the chat graph and the AI service: scripted model replies, a retriever and
documents in memory, and real RS256 tokens signed with a test key."""

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

from docai.agent.metadata import Record
from docai.agent.models import Calls
from docai.api.app import Services
from docai.api.auth import TokenVerifier
from docai.core.contracts import ChatScope
from docai.core.retrieval import Hit, RetrievalConfig, Retriever, Timings
from docai.core.settings import AuthSettings
from docai.core.tracing import Tracing
from docai.core.vectorstore import SearchFilters

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
AUTH = AuthSettings(
    auth_jwks_uri="http://keycloak.test/certs",
    auth_issuer="http://keycloak.test/realms/documents",
    auth_audience="documents-app",
)


def token(
    sub: str = "user-a",
    *,
    audience: str = AUTH.auth_audience,
    issuer: str = AUTH.auth_issuer,
    expires_in: int = 300,
    key: Any = KEY,
) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "aud": audience,
        "iss": issuer,
        "iat": now,
        "exp": now + expires_in,
    }
    return jwt.encode(claims, key, algorithm="RS256")


def verifier() -> TokenVerifier:
    return TokenVerifier(AUTH, key_for=lambda _: KEY.public_key())


class FakeCalls(Calls):
    """Model replies scripted by run name. Answer-writing calls go through a fake chat
    model, so their tokens stream like a real model's."""

    def __init__(
        self, replies: dict[str, list[Any]], limit: int = 10, input_tokens: int = 40_000
    ) -> None:
        self.replies = {name: list(queue) for name, queue in replies.items()}
        self.limit, self.made = limit, 0
        self.input_budget, self.input_spent = input_tokens, 0
        self.log: list[tuple[str, list[BaseMessage]]] = []
        self.used_prompts: list[Any] = []
        import threading

        self._lock = threading.Lock()

    def _next(self, run_name: str, messages: Sequence[BaseMessage]) -> Any:
        self._spend(messages)
        self.log.append((run_name, list(messages)))
        queue = self.replies.get(run_name)
        if not queue:
            raise AssertionError(f"no scripted reply left for {run_name}")
        return queue.pop(0)

    def structured(
        self,
        schema: type[Any],
        messages: Sequence[BaseMessage],
        *,
        run_name: str,
        max_tokens: int = 600,
        prompt: Any = None,
    ) -> Any:
        self.used_prompts.append(prompt)
        reply = self._next(run_name, messages)
        assert isinstance(reply, BaseModel) and isinstance(reply, schema)
        return reply

    def text(
        self,
        messages: Sequence[BaseMessage],
        *,
        run_name: str,
        max_tokens: int = 900,
        prompt: Any = None,
    ) -> str:
        self.used_prompts.append(prompt)
        reply = self._next(run_name, messages)
        model = GenericFakeChatModel(messages=iter([AIMessage(content=reply)]))
        return model.invoke(list(messages), config={"run_name": run_name}).text

    def prompts(self, run_name: str) -> list[str]:
        return [
            "\n".join(str(m.content) for m in messages)
            for name, messages in self.log
            if name == run_name
        ]


def hit(
    doc_id: str, chunk: int, text: str, page: int = 1, title: str = "doc.pdf"
) -> Hit:
    return Hit(
        doc_id=doc_id,
        chunk=chunk,
        pages=[page],
        text=text,
        score=1.0,
        payload={
            "title": title,
            "headings": [],
            "bbox": [0.1, 0.2, 0.9, 0.4],
            "chunk_id": chunk,
        },
    )


@dataclass
class FakeRetriever(Retriever):
    """Returns the scripted hits for each search in turn; records who searched what."""

    results: list[list[Hit]] = field(default_factory=list)
    searches: list[tuple[str, str, SearchFilters | None]] = field(default_factory=list)

    def __post_init__(self) -> None:
        pass

    def search(
        self,
        query: str,
        user_id: str,
        config: RetrievalConfig,
        k: int = 10,
        filters: SearchFilters | None = None,
        timings: Timings | None = None,
    ) -> list[Hit]:
        self.searches.append((query, user_id, filters))
        return self.results.pop(0) if self.results else []


@dataclass
class FakeRecords:
    records: list[Record] = field(default_factory=list)
    loads: list[tuple[str, ChatScope | None]] = field(default_factory=list)

    def load(self, user_id: str, scope: ChatScope | None = None) -> list[Record]:
        self.loads.append((user_id, scope))
        return list(self.records)


def fake_services(
    calls: FakeCalls | None = None,
    retriever: FakeRetriever | None = None,
    records: FakeRecords | None = None,
) -> Services:
    from datetime import date

    found_retriever = retriever or FakeRetriever()
    found_calls = calls or FakeCalls({})
    return Services(
        verifier=verifier(),
        calls=lambda _end_user: found_calls,
        retriever=lambda: found_retriever,
        records=records or FakeRecords(),  # type: ignore[arg-type]
        tracing=Tracing(),
        today=lambda: date(2026, 10, 12),
    )
