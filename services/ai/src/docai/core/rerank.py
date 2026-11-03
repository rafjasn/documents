"""Cross-encoder reranking with a model served by TEI."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from docai.core.embeddings import VectorCache, check_served, post_with_retries


@dataclass(frozen=True)
class RerankModel:
    key: str
    model_id: str


RERANKERS = {
    m.key: m
    for m in (
        RerankModel("mmarco-minilm", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"),
        RerankModel("gte-multilingual", "Alibaba-NLP/gte-multilingual-reranker-base"),
        RerankModel("bge-v2-m3", "BAAI/bge-reranker-v2-m3"),
    )
}


class Reranker(Protocol):
    model: RerankModel

    def scores(self, query: str, texts: Sequence[str]) -> list[float]:
        """Relevance of each text to the query, in input order."""
        ...


class TeiReranker:
    def __init__(
        self, url: str, model: RerankModel, batch: int = 32, timeout: float = 300
    ) -> None:
        self.model, self.url, self.batch = model, url.rstrip("/"), batch
        self.client = httpx.Client(timeout=timeout)
        self.checked = False

    def scores(self, query: str, texts: Sequence[str]) -> list[float]:
        """Scores in input order. TEI takes at most `batch` texts per request (32 by
        default); scores are per pair, so batches don't change them."""
        if not self.checked:
            check_served(self.url, self.model.model_id)
            self.checked = True
        scores: list[float] = []
        for start in range(0, len(texts), self.batch):
            part = list(texts[start : start + self.batch])
            result = post_with_retries(
                self.client,
                f"{self.url}/rerank",
                {"query": query, "texts": part, "truncate": True},
            )
            assert isinstance(result, list)
            by_index = {int(r["index"]): float(r["score"]) for r in result}
            scores.extend(by_index[i] for i in range(len(part)))
        return scores


class CachedReranker:
    def __init__(self, inner: Reranker, cache: VectorCache) -> None:
        self.inner, self.cache, self.model = inner, cache, inner.model

    def scores(self, query: str, texts: Sequence[str]) -> list[float]:
        keys = [self.cache.key(self.model.model_id, query, t) for t in texts]
        found = self.cache.get_scores(keys)
        missing = [i for i, k in enumerate(keys) if k not in found]
        if missing:
            fresh = self.inner.scores(query, [texts[i] for i in missing])
            computed = {keys[i]: s for i, s in zip(missing, fresh)}
            self.cache.put_scores(computed)
            found |= computed
        return [found[k] for k in keys]
