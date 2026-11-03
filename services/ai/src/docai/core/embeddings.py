"""Embedding models: open models served by TEI, and Gemini Embedding 2.

Each model has its own query and passage formats: e5 expects "query: " and "passage: ",
Gemini Embedding 2 takes task prefixes instead of a task type. Vectors come back
normalised, so cosine similarity is a dot product.
"""

import hashlib
import sqlite3
import threading
import time
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from docai.core.settings import CoreSettings


@dataclass(frozen=True)
class EmbeddingModel:
    key: str
    model_id: str
    dim: int
    provider: Literal["tei", "gemini"]
    query_template: str = "{text}"
    passage_template: str = "{text}"

    def query(self, text: str) -> str:
        return self.query_template.format(text=text)

    def passage(self, text: str) -> str:
        return self.passage_template.format(text=text)


QWEN_QUERY = (
    "Instruct: Given a question about business documents, retrieve passages that "
    "answer it\nQuery: {text}"
)

MODELS = {
    m.key: m
    for m in (
        EmbeddingModel(
            "e5-small",
            "intfloat/multilingual-e5-small",
            384,
            "tei",
            "query: {text}",
            "passage: {text}",
        ),
        EmbeddingModel(
            "e5-base",
            "intfloat/multilingual-e5-base",
            768,
            "tei",
            "query: {text}",
            "passage: {text}",
        ),
        EmbeddingModel(
            "arctic-m-v2",
            "Snowflake/snowflake-arctic-embed-m-v2.0",
            768,
            "tei",
            "query: {text}",
        ),
        EmbeddingModel(
            "qwen3-0.6b", "Qwen/Qwen3-Embedding-0.6B", 1024, "tei", QWEN_QUERY
        ),
        EmbeddingModel(
            "gemini-2",
            "gemini-embedding-2",
            768,
            "gemini",
            "task: search result | query: {text}",
            "title: none | text: {text}",
        ),
    )
}


class Embedder(Protocol):
    model: EmbeddingModel

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Vectors for already-formatted texts (see EmbeddingModel.query/passage)."""
        ...


def embed_passages(embedder: Embedder, texts: Sequence[str]) -> list[list[float]]:
    return embedder.embed([embedder.model.passage(t) for t in texts])


def embed_query(embedder: Embedder, text: str) -> list[float]:
    return embedder.embed([embedder.model.query(text)])[0]


def post_with_retries(
    client: httpx.Client, url: str, payload: object, attempts: int = 4
) -> object:
    for attempt in range(attempts):
        try:
            response = client.post(url, json=payload)
            if response.status_code == 429 or response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"{response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response.json()
        except httpx.TransportError, httpx.HTTPStatusError:
            if attempt == attempts - 1:
                raise
            time.sleep(1 + 2 * attempt)
    raise AssertionError("unreachable")


def served_model(url: str, wait_seconds: float = 180) -> str:
    """The model a TEI server runs, from its /info endpoint. Waits while TEI starts: on
    its first start it downloads and loads the model before it listens."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            response = httpx.get(f"{url.rstrip('/')}/info", timeout=30)
            response.raise_for_status()
            return str(response.json()["model_id"])
        except httpx.TransportError, httpx.HTTPStatusError:
            if time.monotonic() > deadline:
                raise
            time.sleep(5)


def check_served(url: str, model_id: str) -> None:
    """Fail before the first request if TEI serves another model. Called lazily, so
    runs answered entirely from a cache need no server."""
    served = served_model(url)
    if served != model_id:
        raise ValueError(f"TEI at {url} serves {served}, not {model_id}")


class TeiEmbedder:
    def __init__(
        self, url: str, model: EmbeddingModel, batch: int = 16, timeout: float = 300
    ) -> None:
        self.model, self.url, self.batch = model, url.rstrip("/"), batch
        self.client = httpx.Client(timeout=timeout)
        self.checked = False

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.checked:
            check_served(self.url, self.model.model_id)
            self.checked = True
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch):
            batch = list(texts[start : start + self.batch])
            result = post_with_retries(
                self.client,
                f"{self.url}/embed",
                {"inputs": batch, "normalize": True, "truncate": True},
            )
            assert isinstance(result, list)
            vectors.extend(result)
        return vectors


class GeminiEmbedder:
    """Gemini Embedding 2. Each text goes in its own Content: a plain list of strings
    would come back as one aggregated embedding."""

    def __init__(self, settings: CoreSettings, model: EmbeddingModel, batch: int = 50):
        from google import genai

        self.model, self.batch = model, batch
        self.client = genai.Client(api_key=settings.gemini_key)
        self.characters = 0  # for cost estimates: the API reports no token counts

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        from google.genai import types

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch):
            batch = list(texts[start : start + self.batch])
            contents: Any = [
                types.Content(parts=[types.Part.from_text(text=t)]) for t in batch
            ]
            result = self.client.models.embed_content(
                model=self.model.model_id,
                contents=contents,
                config=types.EmbedContentConfig(output_dimensionality=self.model.dim),
            )
            assert result.embeddings is not None
            vectors.extend(list(e.values or []) for e in result.embeddings)
            self.characters += sum(len(t) for t in batch)
        return vectors


class VectorCache:
    """SQLite cache of embeddings and reranker scores for evaluation runs, keyed by
    model and input, so changing one ablation setting recomputes only what changed."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, data BLOB)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS scores (key TEXT PRIMARY KEY, score REAL)"
        )

    @staticmethod
    def key(*parts: str) -> str:
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    def get_vectors(self, keys: Sequence[str]) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        with self.lock:
            for start in range(0, len(keys), 500):
                part = list(keys[start : start + 500])
                marks = ",".join("?" * len(part))
                for key, data in self.db.execute(
                    f"SELECT key, data FROM vectors WHERE key IN ({marks})", part
                ):
                    found[key] = array("f", data).tolist()
        return found

    def put_vectors(self, items: dict[str, list[float]]) -> None:
        with self.lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO vectors VALUES (?, ?)",
                [(k, array("f", v).tobytes()) for k, v in items.items()],
            )
            self.db.commit()

    def get_scores(self, keys: Sequence[str]) -> dict[str, float]:
        found: dict[str, float] = {}
        with self.lock:
            for start in range(0, len(keys), 500):
                part = list(keys[start : start + 500])
                marks = ",".join("?" * len(part))
                for key, score in self.db.execute(
                    f"SELECT key, score FROM scores WHERE key IN ({marks})", part
                ):
                    found[key] = float(score)
        return found

    def put_scores(self, items: dict[str, float]) -> None:
        with self.lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO scores VALUES (?, ?)", list(items.items())
            )
            self.db.commit()


class CachedEmbedder:
    def __init__(self, inner: Embedder, cache: VectorCache) -> None:
        self.inner, self.cache, self.model = inner, cache, inner.model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        keys = [
            self.cache.key(self.model.model_id, str(self.model.dim), t) for t in texts
        ]
        found = self.cache.get_vectors(keys)
        missing = sorted({i for i, k in enumerate(keys) if k not in found})
        if missing:
            fresh = self.inner.embed([texts[i] for i in missing])
            computed = {keys[i]: v for i, v in zip(missing, fresh)}
            self.cache.put_vectors(computed)
            found |= computed
        return [found[k] for k in keys]


def embedder(
    key: str, settings: CoreSettings, tei_url: str, cache: VectorCache | None = None
) -> Embedder:
    model = MODELS[key]
    inner: Embedder = (
        GeminiEmbedder(settings, model)
        if model.provider == "gemini"
        else TeiEmbedder(tei_url, model)
    )
    return CachedEmbedder(inner, cache) if cache is not None else inner
