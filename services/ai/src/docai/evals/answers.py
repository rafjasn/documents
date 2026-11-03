"""Answer-level evaluation of the chat graph on the golden set.

Each question goes through the same graph the AI service runs, in-process, as the
evaluation tenant `corpus` over the live index and the documents table, with the whole
library in scope. Answers are scored like V0's: rules first, then the judge; citation
markers are removed before scoring, so "2 invoices [3]" can't pass for 3.

Model replies are cached in evals/cache/graph.sqlite (a LangChain cache keyed by the
prompt and model parameters), so a rerun or rescoring makes no calls; costs are still
reported as what the calls cost, cached or not. A run also records each question's
intent, citations and, for aggregates, what the metadata tool matched, so failures can
be put into categories.
"""

import json
import sqlite3
import statistics
import threading
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

from langchain_core.caches import RETURN_VAL_TYPE, BaseCache
from langchain_core.load import dumps, loads

from docai.agent.graph import AgentContext, build_graph, initial_state
from docai.agent.metadata import AggregateResult
from docai.agent.models import AgentModels, Calls, Usage
from docai.agent.prompt_store import PromptStore
from docai.agent.records import Records
from docai.agent.sources import CITATION, cited_numbers
from docai.agent.state import GraphOptions
from docai.core.retrieval import Retriever
from docai.core.telemetry import CHAT_SECONDS, CHATS
from docai.core.tracing import Tracing
from docai.evals.golden import GoldenItem
from docai.evals.judge import judge
from docai.evals.llm import EVALS_DIR, Models
from docai.evals.runner import RUNS_DIR, SCRATCH_DIR
from docai.evals.scoring import score

TENANT = "corpus"
TODAY = date(2026, 10, 12)  # fixed, so prompts (and the cache) don't change by day
GRAPH_CACHE = EVALS_DIR / "cache" / "graph.sqlite"
CITED_CHARS = 1500


@dataclass(frozen=True)
class GraphVariant:
    name: str
    description: str
    options: GraphOptions = GraphOptions()


GRAPH_VARIANTS = {
    v.name: v
    for v in (
        GraphVariant(
            "A1",
            "Chat graph: routing, hybrid search with grading, metadata tool, cited answer, grounding check",
        ),
        GraphVariant(
            "A1-ng",
            "A1 without grading the search results (one search)",
            GraphOptions(grade=False),
        ),
        GraphVariant(
            "A1-nc",
            "A1 without the grounding check",
            GraphOptions(grounding=False),
        ),
    )
}


class GraphCache(BaseCache):
    """LangChain's cache interface over SQLite; counts the calls it couldn't serve."""

    def __init__(self, path: Path = GRAPH_CACHE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS replies (prompt TEXT, llm TEXT, value TEXT,"
            " PRIMARY KEY (prompt, llm))"
        )
        self.misses = 0

    def lookup(self, prompt: str, llm_string: str) -> RETURN_VAL_TYPE | None:
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM replies WHERE prompt = ? AND llm = ?",
                (prompt, llm_string),
            ).fetchone()
            if row is None:
                self.misses += 1
                return None
        return [loads(item, allowed_objects="core") for item in json.loads(row[0])]

    def update(self, prompt: str, llm_string: str, return_val: RETURN_VAL_TYPE) -> None:
        value = json.dumps([dumps(generation) for generation in return_val])
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO replies VALUES (?, ?, ?)",
                (prompt, llm_string, value),
            )
            self.db.commit()

    def clear(self, **kwargs: Any) -> None:
        raise NotImplementedError("Delete evals/cache/graph.sqlite instead")


@cache
def corpus_ids() -> dict[str, str]:
    """The corpus document ID of each document the seed loaded for TENANT."""
    from docai.corpus.manifest import read_manifest
    from docai.ingest.seed import document_id

    return {document_id(TENANT, r.doc_id): r.doc_id for r in read_manifest()}


def corpus_id(doc_id: str) -> str:
    return corpus_ids().get(doc_id, doc_id)


def strip_citations(answer: str) -> str:
    return " ".join(CITATION.sub(" ", answer).split())


@dataclass(frozen=True)
class AnswerResult:
    id: str
    type: str
    language: str
    kind: str
    question: str
    reference: str
    answer: str
    correct: bool
    method: str  # "rule" or "judge"
    intent: str
    citations: list[tuple[str, int | None]]  # (document, page)
    relevant: list[tuple[str, int | None]]
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    llm_calls: int
    seconds: float
    cached: bool  # every model reply came from the cache
    paid_calls: int = 0  # replies not in the cache: the latency is real when all were
    diagnostics: dict[str, Any] = field(default_factory=dict)


def citation_precision(result: AnswerResult) -> float | None:
    """Share of citations that point to a labelled document (and page, where both
    the label and the citation have one)."""
    if not result.citations or not result.relevant:
        return None
    hits = 0
    for doc, page in result.citations:
        for label_doc, label_page in result.relevant:
            if doc == label_doc and (
                page is None or label_page is None or page == label_page
            ):
                hits += 1
                break
    return hits / len(result.citations)


def aggregate_diagnostics(
    item: GoldenItem, intent: str, computed: AggregateResult | None
) -> dict[str, Any]:
    """What the metadata tool matched, against the labelled documents."""
    if computed is None:
        return {"intent": intent, "computed": None}
    matched = sorted({corpus_id(r.doc_id) for r in computed.records})
    values = [str(r.value) if r.value is not None else None for r in computed.results]
    return {
        "intent": intent,
        "queries": [r.query.model_dump(exclude_none=True) for r in computed.results],
        "values": values,
        "expression": computed.expression,
        "expression_value": None
        if computed.expression_value is None
        else str(computed.expression_value),
        "error": computed.error,
        "matched": matched,
        "labelled": sorted({r.doc_id for r in item.relevant}),
    }


def failure_category(item: GoldenItem, result: AnswerResult) -> str | None:
    """Why an aggregate or cross-document answer failed, from its diagnostics."""
    if result.correct or item.type not in ("aggregate", "cross_doc"):
        return None
    d = result.diagnostics
    if d.get("intent") not in ("aggregate", "mixed"):
        return "routed to search"
    if d.get("computed") is None and not d.get("queries"):
        return "no computation"
    expected = (
        {str(Decimal(item.value).quantize(Decimal("0.01")))}
        if item.kind == "number"
        else set()
    )
    computed = {
        str(Decimal(v).quantize(Decimal("0.01")))
        for v in [*d.get("values", []), d.get("expression_value")]
        if v is not None
    }
    if expected and expected & computed:
        return "answer step (the computed value was right)"
    if set(d.get("labelled", [])) <= set(d.get("matched", [])):
        return "filters matched extra documents, or the wrong operation"
    return "filters missed documents"


@dataclass
class AnswerRun:
    run_id: str
    variant: str
    description: str
    split: str
    answer_model: str
    judge_model: str
    created: str
    options: dict[str, Any]
    items: list[AnswerResult] = field(default_factory=list)
    kind: str = "answers"
    paid_calls: int = 0
    # Which prompts answered: "code", or a Langfuse label with each prompt's version.
    prompts: dict[str, Any] = field(default_factory=lambda: {"label": "code"})
    # How the graph's model calls reached Gemini: "direct" or "gateway" (Stage 10b).
    route: str = "direct"

    def summary(self) -> dict[str, Any]:
        def accuracy(results: Sequence[AnswerResult]) -> dict[str, Any]:
            right = sum(r.correct for r in results)
            return {
                "correct": right,
                "n": len(results),
                "accuracy": round(right / len(results), 3) if results else None,
            }

        by_type: dict[str, list[AnswerResult]] = defaultdict(list)
        by_language: dict[str, list[AnswerResult]] = defaultdict(list)
        for r in self.items:
            by_type[r.type].append(r)
            by_language[r.language].append(r)
        precisions = [p for r in self.items if (p := citation_precision(r)) is not None]
        costs = [r.cost_usd for r in self.items]
        # Only questions whose every model call was made now have a real latency.
        live = [
            r.seconds for r in self.items if r.llm_calls and r.paid_calls == r.llm_calls
        ]
        categories: dict[str, int] = defaultdict(int)
        for r in self.items:
            if r.diagnostics.get("failure"):
                categories[r.diagnostics["failure"]] += 1
        answerable = [r for r in self.items if r.type != "unanswerable"]
        return {
            **accuracy(self.items),
            "by_type": {k: accuracy(v) for k, v in sorted(by_type.items())},
            "by_language": {k: accuracy(v) for k, v in sorted(by_language.items())},
            "judged": sum(r.method == "judge" for r in self.items),
            "citation_precision": round(statistics.fmean(precisions), 3)
            if precisions
            else None,
            "cited_answers": round(
                sum(bool(r.citations) for r in answerable) / len(answerable), 3
            )
            if answerable
            else None,
            "intents": {
                intent: sum(r.intent == intent for r in self.items)
                for intent in sorted({r.intent for r in self.items})
            },
            "failures": dict(sorted(categories.items())),
            "llm_calls_per_query": round(
                statistics.fmean(r.llm_calls for r in self.items), 2
            )
            if self.items
            else None,
            "cost_per_query_usd": None
            if not costs or any(c is None for c in costs)
            else round(statistics.fmean(c for c in costs if c is not None), 6),
            "latency_seconds": {
                "p50": round(statistics.median(live), 2) if live else None,
                "p95": round(statistics.quantiles(live, n=20)[-1], 2)
                if len(live) >= 2
                else None,
                "measured_on": len(live),
            },
            "paid_calls": self.paid_calls,
        }

    def save(self, record: bool) -> Path:
        directory = RUNS_DIR if record else SCRATCH_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id}.json"
        payload = {k: v for k, v in asdict(self).items() if k != "items"}
        payload |= {"summary": self.summary(), "items": [asdict(r) for r in self.items]}
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        return path


def answer_one(
    graph: Any,
    item: GoldenItem,
    variant: GraphVariant,
    models: AgentModels,
    retriever: Retriever,
    records: Records,
    tracing: Tracing,
    cache: GraphCache | None,
    prompts: PromptStore | None = None,
) -> tuple[dict[str, Any], Usage, float, int]:
    usage = Usage()
    misses = cache.misses if cache else 0
    context = AgentContext(
        user_id=TENANT,
        calls=Calls(models),
        retriever=retriever,
        load_records=lambda: records.load(TENANT),
        today=TODAY,
        prompts=prompts or PromptStore(),
    )
    started = time.monotonic()
    final = graph.invoke(
        initial_state(item.question, options=variant.options),
        {
            "callbacks": [usage, *tracing.callbacks],
            "run_name": f"eval-{variant.name}",
            "recursion_limit": 30,
            "metadata": {
                "item_id": item.id,
                "variant": variant.name,
                "langfuse_tags": ["eval", variant.name],
            },
        },
        context=context,
    )
    seconds = round(time.monotonic() - started, 3)
    paid = cache.misses - misses if cache else usage.calls
    # Counted like the service's questions, for the dashboard (job evals).
    CHATS.labels("answered").inc()
    CHAT_SECONDS.observe(seconds)
    return final, usage, seconds, paid


def evaluate_answers(
    variant: GraphVariant,
    split: str,
    items: Sequence[GoldenItem],
    models: AgentModels,
    cache: GraphCache | None,
    retriever: Retriever,
    records: Records,
    judge_models: Models,
    judge_model: str,
    tracing: Tracing,
    prompts: PromptStore | None = None,
) -> AnswerRun:
    created = datetime.now(UTC)
    prompts = prompts or PromptStore()
    route = "gateway" if models.settings.llm_gateway_url else "direct"
    run = AnswerRun(
        # Gateway runs are kept apart from the direct runs that the CI bundle reads.
        run_id=f"{created:%Y%m%d-%H%M%S}-{variant.name}-{split}"
        + ("-gateway" if route == "gateway" else ""),
        variant=variant.name,
        description=variant.description,
        split=split,
        answer_model=models.specs["chat"],
        judge_model=judge_model,
        created=created.isoformat(timespec="seconds"),
        options=asdict(variant.options),
        prompts=prompts.describe(),
        route=route,
    )
    graph = build_graph()
    for item in items:
        final, usage, seconds, paid = answer_one(
            graph, item, variant, models, retriever, records, tracing, cache, prompts
        )
        answer = str(final.get("answer", ""))
        plain = strip_citations(answer)
        correct = score(item, plain)
        method = "rule"
        if correct is None:
            correct, _ = judge(
                judge_models, judge_model, item, plain, run_name=f"eval-{variant.name}"
            )
            method = "judge"
        intent = str(final.get("intent", ""))
        result = AnswerResult(
            id=item.id,
            type=item.type,
            language=item.language,
            kind=item.kind,
            question=item.question,
            reference=item.answer,
            answer=answer,
            correct=correct,
            method=method,
            intent=intent,
            citations=[
                (corpus_id(c.doc_id), c.page) for c in final.get("citations", [])
            ],
            relevant=[(r.doc_id, r.page) for r in item.relevant],
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=None if usage.cost is None else round(usage.cost, 6),
            llm_calls=usage.calls,
            seconds=seconds,
            cached=paid == 0,
            paid_calls=paid,
            diagnostics=aggregate_diagnostics(item, intent, final.get("computed"))
            | {
                "searches": final.get("searches", 0),
                "queries_tried": final.get("queries", []),
                "grounded": final.get("grounded"),
                "hits": [
                    (corpus_id(h.doc_id), h.chunk, h.pages)
                    for h in final.get("hits", [])
                ],
                # What the answer cites, for the faithfulness judge.
                "cited": [
                    {"n": s.number, "document": s.title, "text": s.text[:CITED_CHARS]}
                    for s in final.get("sources", [])
                    if s.number in cited_numbers(answer)
                ],
            },
        )
        failure = failure_category(item, result)
        if failure:
            result.diagnostics["failure"] = failure
        run.items.append(result)
    run.paid_calls = sum(r.paid_calls for r in run.items)
    return run
