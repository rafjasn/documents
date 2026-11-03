"""Retrieval evaluation: the ablation rows V1-V6, embedders E1-E3 and rerankers.

Every variant is scored against the golden set's relevance labels, which name
documents and pages, so labels survive any chunking: a retrieved chunk is a hit when it
comes from a labelled document and, if the label has a page, covers that page.

- recall@k: the share of a question's labelled (document, page) items found in the top k
- MRR@10: one over the rank of the first hit
- nDCG@10: binary gain for each hit that finds a labelled item not found above it
- document recall@10: the share of labelled documents found, ignoring pages

Each variant's index is a separate Qdrant collection (eval-<config>) holding the whole
corpus under the tenant `corpus`, so every split's documents are distractors for the
others. Collections are rebuilt only when missing or incomplete; embeddings, reranker
scores and chunk contexts are cached, so rebuilding repeats no paid call.
"""

import json
import math
import statistics
import threading
import time
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models

from docai.core.embeddings import MODELS, Embedder
from docai.core.index_config import IndexConfig
from docai.core.retrieval import Hit, RetrievalConfig, Retriever, Timings
from docai.core.vectorstore import (
    collection_exists,
    create_collection,
    replace_document,
)
from docai.corpus.manifest import DocumentRecord
from docai.evals.golden import GoldenItem
from docai.evals.runner import RUNS_DIR, SCRATCH_DIR
from docai.ingest.indexer import build_points
from docai.ingest.models import Models, Usage
from docai.ingest.pipeline import ParsedDocument

TENANT = "corpus"
K = 10
GEMINI_EMBED_CHARS_PER_TOKEN = 4  # the embedding API reports no token counts
BUILD_THREADS = 8


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    index: IndexConfig
    retrieval: RetrievalConfig
    reranker: str | None = None


STRUCTURE = IndexConfig()
DENSE = RetrievalConfig()
HYBRID = RetrievalConfig(sparse=True)
RERANKED = RetrievalConfig(sparse=True, rerank=True)

VARIANTS = {
    v.name: v
    for v in (
        Variant(
            "V1",
            "Fixed-size chunks (400 tokens) + dense",
            IndexConfig(chunker="fixed"),
            DENSE,
        ),
        Variant("V2", "Structure-aware chunks + dense", STRUCTURE, DENSE),
        Variant("V3", "V2 + BM25, reciprocal rank fusion", STRUCTURE, HYBRID),
        Variant(
            "V3s",
            "V3 with BM25 prefix stemming (6 characters)",
            IndexConfig(stem=6),
            HYBRID,
        ),
        Variant(
            "V4", "V3 + cross-encoder rerank", STRUCTURE, RERANKED, "mmarco-minilm"
        ),
        Variant(
            "V5h",
            "V4 + document header context (category and summary), free",
            IndexConfig(context="header"),
            RERANKED,
            "mmarco-minilm",
        ),
        Variant(
            "V5h-20",
            "V5h reranking 20 candidates instead of 50",
            IndexConfig(context="header"),
            RetrievalConfig(sparse=True, rerank=True, candidates=20),
            "mmarco-minilm",
        ),
        Variant(
            "V5",
            "V4 + contextual retrieval (Gemini context per chunk)",
            IndexConfig(context="llm"),
            RERANKED,
            "mmarco-minilm",
        ),
        Variant(
            "V5s-nr",
            "V5-nr with BM25 prefix stemming (6 characters)",
            IndexConfig(context="llm", stem=6),
            HYBRID,
        ),
        Variant(
            "V5a-nr",
            "V5-nr with the snowflake-arctic-embed-m-v2.0 embedder",
            IndexConfig(context="llm", embedder="arctic-m-v2"),
            HYBRID,
        ),
        Variant(
            "V6-nr",
            "V5-nr + parent-child expansion",
            IndexConfig(context="llm"),
            RetrievalConfig(sparse=True, parents=True),
        ),
        Variant(
            "V6s-nr",
            "V5s-nr + parent-child expansion",
            IndexConfig(context="llm", stem=6),
            RetrievalConfig(sparse=True, parents=True),
        ),
        Variant(
            "V5-20",
            "V5 reranking 20 candidates",
            IndexConfig(context="llm"),
            RetrievalConfig(sparse=True, rerank=True, candidates=20),
            "mmarco-minilm",
        ),
        Variant(
            "V6-20",
            "V6 reranking 20 candidates",
            IndexConfig(context="llm"),
            RetrievalConfig(sparse=True, rerank=True, parents=True, candidates=20),
            "mmarco-minilm",
        ),
        Variant(
            "V6",
            "V5 + parent-child expansion",
            IndexConfig(context="llm"),
            RetrievalConfig(sparse=True, rerank=True, parents=True),
            "mmarco-minilm",
        ),
        Variant(
            "V5h-nr",
            "V3 + document header context, no rerank",
            IndexConfig(context="header"),
            HYBRID,
        ),
        Variant(
            "V5hs-nr",
            "V5h-nr with BM25 prefix stemming (6 characters)",
            IndexConfig(context="header", stem=6),
            HYBRID,
        ),
        Variant(
            "V5-nr",
            "V3 + contextual retrieval, no rerank",
            IndexConfig(context="llm"),
            HYBRID,
        ),
        Variant(
            "BM25",
            "BM25 only, whole words",
            STRUCTURE,
            RetrievalConfig(dense=False, sparse=True),
        ),
        Variant(
            "BM25s",
            "BM25 only, prefix stemming (6 characters)",
            IndexConfig(stem=6),
            RetrievalConfig(dense=False, sparse=True),
        ),
        Variant("E1", "Embedder: multilingual-e5-small (TEI), dense", STRUCTURE, DENSE),
        Variant(
            "E2",
            "Embedder: snowflake-arctic-embed-m-v2.0 (TEI), dense",
            IndexConfig(embedder="arctic-m-v2"),
            DENSE,
        ),
        Variant(
            "E3",
            "Embedder: Gemini Embedding 2 (768 dimensions), dense",
            IndexConfig(embedder="gemini-2"),
            DENSE,
        ),
        Variant(
            "R1",
            "Reranker: mmarco-mMiniLMv2 (base), on V3",
            STRUCTURE,
            RERANKED,
            "mmarco-minilm",
        ),
        Variant(
            "R1g",
            "Reranker: gte-multilingual-reranker-base, on V3",
            STRUCTURE,
            RERANKED,
            "gte-multilingual",
        ),
    )
}


# Metrics --------------------------------------------------------------------------------


def covers(hit: Hit, doc_id: str, page: int | None) -> bool:
    return hit.doc_id == doc_id and (page is None or not hit.pages or page in hit.pages)


def metrics(item: GoldenItem, hits: Sequence[Hit]) -> dict[str, float]:
    labels = [(r.doc_id, r.page) for r in item.relevant]
    found_at: dict[tuple[str, int | None], int] = {}
    first_hit: int | None = None
    gains: list[float] = []
    for rank, hit in enumerate(hits[:K], 1):
        new = [x for x in labels if x not in found_at and covers(hit, *x)]
        if any(covers(hit, *x) for x in labels) and first_hit is None:
            first_hit = rank
        for label in new:
            found_at[label] = rank
        gains.append(1.0 if new else 0.0)
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(labels), K)))
    docs = {d for d, _ in labels}
    found_docs = {h.doc_id for h in hits[:K]} & docs
    return {
        "recall@5": sum(rank <= 5 for rank in found_at.values()) / len(labels),
        "recall@10": len(found_at) / len(labels),
        "mrr@10": 1 / first_hit if first_hit else 0.0,
        "ndcg@10": dcg / ideal if ideal else 0.0,
        "doc_recall@10": len(found_docs) / len(docs),
        # What an answer step would read: parents are longer than chunks.
        "chars@5": float(sum(len(h.text) for h in hits[:5])),
    }


@dataclass(frozen=True)
class QueryResult:
    id: str
    type: str
    language: str
    question: str
    relevant: list[tuple[str, int | None]]
    retrieved: list[tuple[str, list[int], float]]
    metrics: dict[str, float]
    seconds: float


def mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 3) if values else None


def summarise(results: Sequence[QueryResult]) -> dict[str, Any]:
    names = ("recall@5", "recall@10", "mrr@10", "ndcg@10", "doc_recall@10", "chars@5")

    def block(part: Sequence[QueryResult]) -> dict[str, Any]:
        return {"n": len(part)} | {n: mean([r.metrics[n] for r in part]) for n in names}

    by_type: dict[str, list[QueryResult]] = defaultdict(list)
    by_language: dict[str, list[QueryResult]] = defaultdict(list)
    for r in results:
        by_type[r.type].append(r)
        by_language[r.language].append(r)
    seconds = sorted(r.seconds for r in results)
    return {
        "overall": block(results),
        "by_type": {k: block(v) for k, v in sorted(by_type.items())},
        "by_language": {k: block(v) for k, v in sorted(by_language.items())},
        "latency_seconds": {
            "p50": round(statistics.median(seconds), 3) if seconds else None,
            "p95": round(seconds[int(0.95 * (len(seconds) - 1))], 3)
            if seconds
            else None,
        },
    }


# Index building ---------------------------------------------------------------------------


@dataclass
class IndexStats:
    """Costs are what building the index from scratch costs, cached replies included;
    paid_calls counts the calls this build made. Kept in the collection's metadata."""

    collection: str
    documents: int = 0
    chunks: int = 0
    built: bool = False
    seconds: float = 0.0
    context_calls: int = 0
    context_cost_usd: str = "0"
    embedding_cost_usd: str = "0"
    paid_calls: int = 0


class CountingEmbedder:
    """Counts the characters it embeds: Gemini reports no token counts."""

    def __init__(self, inner: Embedder) -> None:
        self.inner, self.model, self.characters = inner, inner.model, 0
        self.lock = threading.Lock()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        with self.lock:
            self.characters += sum(len(t) for t in texts)
        return self.inner.embed(texts)


def collection_name(config: IndexConfig) -> str:
    return f"eval-{config.name}"


def build_index(
    client: QdrantClient,
    config: IndexConfig,
    documents: Sequence[tuple[DocumentRecord, ParsedDocument]],
    embedder: Embedder,
    models_: Models | None,
    context_model: str | None,
    expected_chunks: int | None = None,
    rebuild: bool = False,
) -> IndexStats:
    """Index the corpus into eval-<config>, unless a complete copy is already there."""
    name = collection_name(config)
    stats = IndexStats(collection=name, documents=len(documents))
    if collection_exists(client, name) and not rebuild:
        count = int(client.count(name).count)
        if expected_chunks is None or count == expected_chunks:
            recorded = client.get_collection(name).config.metadata or {}
            fields = {
                k: v for k, v in recorded.items() if k in IndexStats.__annotations__
            }
            return replace(stats, **fields) if fields else replace(stats, chunks=count)
    if collection_exists(client, name):
        client.delete_collection(name)
    create_collection(client, name, MODELS[config.embedder].dim)
    started = time.monotonic()
    context_cost = Decimal(0)
    counting = CountingEmbedder(embedder)

    def points_for(
        item: tuple[DocumentRecord, ParsedDocument],
    ) -> tuple[str, list[models.PointStruct], list[Usage]]:
        record, parsed = item
        points, usages = build_points(
            parsed,
            user_id=TENANT,
            doc_id=record.doc_id,
            config=config,
            embedder=counting,
            title=record.title,
            models_=models_,
            context_model=context_model,
            metadata={"corpus_doc_id": record.doc_id, "langfuse_tags": ["index-eval"]},
        )
        return record.doc_id, points, usages

    # Documents in parallel: a document's context calls stay in order, so they share
    # Gemini's cached prefix, and the slow calls of long contracts overlap.
    with ThreadPoolExecutor(max_workers=BUILD_THREADS) as pool:
        built = list(pool.map(points_for, documents))
    for doc_id, points, usages in built:
        replace_document(client, name, TENANT, doc_id, points)
        stats.chunks += len(points)
        stats.context_calls += len(usages)
        stats.paid_calls += sum(not u.cached for u in usages)
        context_cost += sum((u.cost_usd or Decimal(0) for u in usages), Decimal(0))
    stats.seconds = round(time.monotonic() - started, 1)
    stats.context_cost_usd = str(context_cost)
    if MODELS[config.embedder].provider == "gemini":
        tokens = counting.characters // GEMINI_EMBED_CHARS_PER_TOKEN
        stats.embedding_cost_usd = str(
            Decimal("0.20") * tokens / 1_000_000  # Gemini Embedding 2, standard tier
        )
    recorded = asdict(stats) | {"collection": name}
    client.update_collection(
        name, metadata={k: v for k, v in recorded.items() if k != "built"}
    )
    stats.built = True
    return stats


# Runs -----------------------------------------------------------------------------------


@dataclass
class RetrievalRun:
    run_id: str
    variant: str
    description: str
    split: str
    index: dict[str, Any]
    retrieval: dict[str, Any]
    reranker: str | None
    created: str
    index_stats: dict[str, Any]
    results: list[QueryResult] = field(default_factory=list)
    kind: str = "retrieval"
    query_cache: bool = False  # cached query embeddings and scores: latency isn't real

    def summary(self) -> dict[str, Any]:
        return summarise(self.results)

    def save(self, record: bool) -> Path:
        directory = RUNS_DIR if record else SCRATCH_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id}.json"
        payload = {k: v for k, v in asdict(self).items() if k != "results"}
        payload |= {
            "summary": self.summary(),
            "results": [asdict(r) for r in self.results],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return path


def evaluate(
    variant: Variant,
    split: str,
    items: Sequence[GoldenItem],
    retriever: Retriever,
    index_stats: IndexStats,
) -> RetrievalRun:
    created = datetime.now(UTC)
    run = RetrievalRun(
        run_id=f"{created:%Y%m%d-%H%M%S}-{variant.name}-{split}",
        variant=variant.name,
        description=variant.description,
        split=split,
        index=variant.index.model_dump() | {"name": variant.index.name},
        retrieval=asdict(variant.retrieval),
        reranker=variant.reranker,
        created=created.isoformat(timespec="seconds"),
        index_stats=asdict(index_stats),
    )
    for item in items:
        if not item.relevant:
            continue  # nothing to retrieve, e.g. some unanswerable questions
        timings = Timings()
        started = time.monotonic()
        hits = retriever.search(
            item.question, TENANT, variant.retrieval, k=K, timings=timings
        )
        run.results.append(
            QueryResult(
                id=item.id,
                type=item.type,
                language=item.language,
                question=item.question,
                relevant=[(r.doc_id, r.page) for r in item.relevant],
                retrieved=[(h.doc_id, h.pages, round(h.score, 4)) for h in hits],
                metrics=metrics(item, hits),
                seconds=round(time.monotonic() - started, 3),
            )
        )
    return run
