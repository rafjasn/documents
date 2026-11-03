"""Run a variant over a golden split: answer, score with rules, judge the rest, summarise.

Runs are written to evals/scratch/ (git-ignored) unless recorded, which writes them to
evals/runs/ for docs/RESULTS.md. A dry run makes no calls and estimates the cost.
"""

import json
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from docai.core.prices import cost
from docai.core.tracing import ExperimentRecord
from docai.evals.golden import GoldenItem
from docai.evals.judge import judge
from docai.evals.llm import EVALS_DIR, Messages, Models
from docai.evals.scoring import score

RUNS_DIR = EVALS_DIR / "runs"
SCRATCH_DIR = EVALS_DIR / "scratch"
ESTIMATED_OUTPUT_TOKENS = 150
JUDGED_KINDS = ("number", "text", "date", "boolean", "refusal")


class Variant(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    def messages(self, item: GoldenItem) -> Messages: ...

    @property
    def params(self) -> dict[str, int]: ...


@dataclass(frozen=True)
class ItemResult:
    id: str
    type: str
    language: str
    kind: str
    question: str
    reference: str
    answer: str
    correct: bool
    method: str  # "rule" or "judge"
    input_tokens: int
    output_tokens: int
    seconds: float
    cached: bool


@dataclass
class RunResult:
    run_id: str
    variant: str
    description: str
    split: str
    answer_model: str
    judge_model: str
    created: str
    items: list[ItemResult] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        def accuracy(results: list[ItemResult]) -> dict[str, object]:
            right = sum(r.correct for r in results)
            return {
                "correct": right,
                "n": len(results),
                "accuracy": round(right / len(results), 3) if results else None,
            }

        by_type: dict[str, list[ItemResult]] = defaultdict(list)
        by_language: dict[str, list[ItemResult]] = defaultdict(list)
        for result in self.items:
            by_type[result.type].append(result)
            by_language[result.language].append(result)
        input_tokens = sum(r.input_tokens for r in self.items)
        output_tokens = sum(r.output_tokens for r in self.items)
        total_cost = cost(self.answer_model, input_tokens, output_tokens)
        live = [r.seconds for r in self.items if not r.cached] or [
            r.seconds for r in self.items
        ]
        return {
            **accuracy(self.items),
            "by_type": {
                name: accuracy(results) for name, results in sorted(by_type.items())
            },
            "by_language": {
                name: accuracy(results) for name, results in sorted(by_language.items())
            },
            "judged": sum(r.method == "judge" for r in self.items),
            "answer_tokens": {"input": input_tokens, "output": output_tokens},
            "cost_per_query_usd": float(total_cost / len(self.items))
            if total_cost is not None and self.items
            else None,
            "latency_seconds": {
                "p50": round(statistics.median(live), 2) if live else None,
                "p95": round(statistics.quantiles(live, n=20)[-1], 2)
                if len(live) >= 2
                else None,
            },
            "uncached_answer_calls": sum(not r.cached for r in self.items),
        }

    def save(self, record: bool) -> Path:
        directory = RUNS_DIR if record else SCRATCH_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id}.json"
        payload = {k: v for k, v in asdict(self).items() if k != "items"}
        payload |= {"summary": self.summary(), "items": [asdict(r) for r in self.items]}
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return path


def subset(items: list[GoldenItem], limit: int | None) -> list[GoldenItem]:
    """The first `limit` items, taken round-robin across question types for coverage."""
    ordered = sorted(items, key=lambda i: i.id)
    if limit is None or limit >= len(ordered):
        return ordered
    by_type: dict[str, list[GoldenItem]] = defaultdict(list)
    for item in ordered:
        by_type[item.type].append(item)
    chosen: list[GoldenItem] = []
    while len(chosen) < limit:
        for queue in by_type.values():
            if queue and len(chosen) < limit:
                chosen.append(queue.pop(0))
    return chosen


@dataclass(frozen=True)
class Estimate:
    items: int
    uncached_answer_calls: int
    estimated_input_tokens: int
    estimated_cost_usd: Decimal | None
    judge_calls_at_most: int
    judge_model: str


def estimate(
    models: Models,
    variant: Variant,
    items: list[GoldenItem],
    answer_model: str,
    judge_model: str,
) -> Estimate:
    uncached = [
        i
        for i in items
        if not models.is_cached(answer_model, variant.messages(i), **variant.params)
    ]
    input_tokens = (
        sum(len(text) for i in uncached for _, text in variant.messages(i)) // 4
    )
    return Estimate(
        items=len(items),
        uncached_answer_calls=len(uncached),
        estimated_input_tokens=input_tokens,
        estimated_cost_usd=cost(
            answer_model, input_tokens, ESTIMATED_OUTPUT_TOKENS * len(uncached)
        ),
        judge_calls_at_most=sum(i.kind in JUDGED_KINDS for i in items),
        judge_model=judge_model,
    )


def run(
    models: Models,
    variant: Variant,
    split: str,
    items: list[GoldenItem],
    answer_model: str,
    judge_model: str,
) -> RunResult:
    created = datetime.now(UTC)
    result = RunResult(
        run_id=f"{created:%Y%m%d-%H%M%S}-{variant.name}-{split}",
        variant=variant.name,
        description=variant.description,
        split=split,
        answer_model=answer_model,
        judge_model=judge_model,
        created=created.isoformat(timespec="seconds"),
    )
    for item in items:
        reply = models.call(
            answer_model,
            variant.messages(item),
            run_name=f"eval-{variant.name}",
            metadata={"item_id": item.id, "variant": variant.name, "split": split},
            **variant.params,
        )
        correct = score(item, reply.text)
        method = "rule"
        if correct is None:
            correct, _ = judge(
                models, judge_model, item, reply.text, run_name=f"eval-{variant.name}"
            )
            method = "judge"
        result.items.append(
            ItemResult(
                id=item.id,
                type=item.type,
                language=item.language,
                kind=item.kind,
                question=item.question,
                reference=item.answer,
                answer=reply.text,
                correct=correct,
                method=method,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                seconds=reply.seconds,
                cached=reply.cached,
            )
        )
    return result


GOLDEN_DATASET = "documents-golden-v1"


def experiment_records(
    result: RunResult, items: list[GoldenItem]
) -> list[ExperimentRecord]:
    """A finished run as Langfuse experiment records (golden item plus answer and score)."""
    by_id = {item.id: item for item in items}
    return [
        ExperimentRecord(
            id=r.id,
            input={
                "question": by_id[r.id].question,
                "language": r.language,
                "type": r.type,
            },
            expected_output={
                "answer": by_id[r.id].answer,
                "value": by_id[r.id].value,
                "kind": r.kind,
            },
            metadata={
                "split": by_id[r.id].split,
                "type": r.type,
                "language": r.language,
                "documents": [d.doc_id for d in by_id[r.id].relevant],
            },
            output=r.answer,
            score=1.0 if r.correct else 0.0,
            comment=f"decided by {r.method}",
        )
        for r in result.items
    ]
