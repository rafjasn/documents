"""Retrieval: dense and/or BM25 search with RRF, optional reranking and parent expansion.

The tenant is a required argument and goes into every Qdrant query as a filter
condition; see docai.core.vectorstore.
"""

import time
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import QdrantClient

from docai.core.embeddings import Embedder, embed_query
from docai.core.rerank import Reranker
from docai.core.sparse import SparseEncoder
from docai.core.vectorstore import SearchFilters, payload, search


@dataclass(frozen=True)
class RetrievalConfig:
    dense: bool = True
    sparse: bool = False
    rerank: bool = False
    parents: bool = False  # return each hit's parent group instead of the chunk
    candidates: int = 50  # per search arm, and the pool the reranker sorts
    rrf_k: int = 60


# Chosen in Stage 03 (docs/RESULTS.md): hybrid search over contextualised chunks. The
# cross-encoders tried lowered MRR on contextualised chunks, and parents tripled the text
# for no measurable gain; Stage 04 decides on parents by answer quality.
DEFAULT_RETRIEVAL = RetrievalConfig(sparse=True)


@dataclass(frozen=True)
class Hit:
    doc_id: str
    chunk: int
    pages: list[int]
    text: str
    score: float
    payload: dict[str, Any]


@dataclass
class Timings:
    seconds: dict[str, float] = field(default_factory=dict)

    def add(self, step: str, started: float) -> None:
        self.seconds[step] = round(time.monotonic() - started, 4)


def rerank_text(item: dict[str, Any]) -> str:
    headings = " > ".join(item.get("headings") or [])
    return "\n".join(
        part
        for part in (item.get("context") or "", headings, item.get("text") or "")
        if part
    )


class Retriever:
    def __init__(
        self,
        client: QdrantClient,
        collection: str,
        embedder: Embedder | None,
        sparse: SparseEncoder | None,
        reranker: Reranker | None = None,
    ) -> None:
        self.client, self.collection = client, collection
        self.embedder, self.sparse, self.reranker = embedder, sparse, reranker

    def search(
        self,
        query: str,
        user_id: str,
        config: RetrievalConfig,
        k: int = 10,
        filters: SearchFilters | None = None,
        timings: Timings | None = None,
    ) -> list[Hit]:
        timings = timings or Timings()
        started = time.monotonic()
        dense = None
        if config.dense:
            if self.embedder is None:
                raise ValueError("Dense retrieval needs an embedder")
            dense = embed_query(self.embedder, query)
        sparse = None
        if config.sparse:
            if self.sparse is None:
                raise ValueError("Sparse retrieval needs a sparse encoder")
            sparse = self.sparse.query(query)
        timings.add("encode", started)

        started = time.monotonic()
        limit = config.candidates if (config.rerank or config.parents) else k
        points = search(
            self.client,
            self.collection,
            user_id,
            dense=dense,
            sparse=sparse,
            limit=limit,
            filters=filters,
            rrf_k=config.rrf_k,
        )
        timings.add("search", started)
        hits = [
            Hit(
                doc_id=str(p["doc_id"]),
                chunk=int(p["chunk_id"]),
                pages=list(p.get("pages") or []),
                text=str(p.get("text") or ""),
                score=float(point.score),
                payload=p,
            )
            for point in points
            for p in [payload(point)]
        ]
        # A fixed order for tied scores, so the same query always returns the same hits.
        hits.sort(key=lambda h: (-h.score, h.doc_id, h.chunk))
        hits = hits[:limit]

        if config.rerank and hits:
            if self.reranker is None:
                raise ValueError("Reranking needs a reranker")
            started = time.monotonic()
            scores = self.reranker.scores(query, [rerank_text(h.payload) for h in hits])
            hits = [
                Hit(h.doc_id, h.chunk, h.pages, h.text, s, h.payload)
                for s, h in sorted(zip(scores, hits), key=lambda pair: -pair[0])
            ]
            timings.add("rerank", started)

        if config.parents:
            hits = expand_to_parents(hits)
        return hits[:k]


def expand_to_parents(hits: list[Hit]) -> list[Hit]:
    """Replace each hit by its parent group, keeping the best-ranked hit per parent."""
    seen: set[tuple[str, int]] = set()
    parents: list[Hit] = []
    for hit in hits:
        key = (hit.doc_id, int(hit.payload.get("parent_id", hit.chunk)))
        if key in seen:
            continue
        seen.add(key)
        parents.append(
            Hit(
                doc_id=hit.doc_id,
                chunk=hit.chunk,
                pages=list(hit.payload.get("parent_pages") or hit.pages),
                text=str(hit.payload.get("parent_text") or hit.text),
                score=hit.score,
                payload=hit.payload,
            )
        )
    return parents
