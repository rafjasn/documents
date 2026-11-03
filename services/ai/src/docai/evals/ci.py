"""The evaluation subset CI runs (Stage 06): dev questions asked live against a bundle of
the evaluation tenant's index, with a regression threshold.

The bundle (evals/ci/) holds what the graph reads for the CUAD and synthetic documents;
SROIE stays out, its licence being research-only. It has each chunk's payload and the
documents' extracted fields. CI restores it into a Qdrant service container, embedding
the chunks with a TEI container as the indexer does (context, headings, text), and serves
the fields from the file instead of DynamoDB. Each question then goes through the chat
graph as the tenant `corpus` with live model calls, is scored by rule (the judge,
JUDGE_MODEL, decides what the rules can't; without it those count as failures), and the
job fails below the committed threshold.

With Langfuse prompts the record names each prompt's version and text hash, the
versions `docai prompts promote --evaluation` then moves `production` to, and the
versions `production` had when the run started; a promotion adds its outcome to the
record. The record's path goes to the step output `record` on GitHub Actions.

Only dev questions are used: a CI failure is something to debug, and debugging on test
questions would tune the pipeline to the held-out split.
"""

import gzip
import json
import os
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models

from docai.agent.graph import build_graph
from docai.agent.metadata import Record
from docai.agent.models import AgentModels
from docai.agent.prompt_store import PRODUCTION, PromptStore, production_versions
from docai.agent.records import in_scope
from docai.core.contracts import ChatScope
from docai.core.embeddings import MODELS, Embedder, TeiEmbedder, embed_passages
from docai.core.index_config import IndexConfig
from docai.core.retrieval import Retriever
from docai.core.settings import IndexSettings, Settings
from docai.core.sparse import SparseEncoder
from docai.core.telemetry import CHATS
from docai.core.tracing import Tracing
from docai.core.vectorstore import DENSE, SPARSE, create_collection
from docai.evals.answers import GRAPH_VARIANTS, TENANT, answer_one, strip_citations
from docai.evals.golden import GoldenItem, read_items
from docai.evals.judge import judge
from docai.evals.llm import EVALS_DIR, Models
from docai.evals.runner import RUNS_DIR, SCRATCH_DIR
from docai.evals.scoring import score

BUNDLE = EVALS_DIR / "ci"
COLLECTION = "chunks_ci"
# Questions of each type in the subset, the first by ID among the dev questions about
# the bundle's documents.
QUOTAS = {
    "aggregate": 8,
    "cross_doc": 6,
    "cross_lingual": 5,
    "single_fact": 4,
    "table": 3,
    "image": 2,
    "unanswerable": 2,
}
SLACK = 3  # answers that live sampling moves between runs: one or two (Stage 04)
EMBED_BATCH = 32
# What the graph reads from a point; the rest of the payload stays out of the bundle.
PAYLOAD = (
    "user_id",
    "doc_id",
    "title",
    "lang",
    "category",
    "doc_date",
    "vendor",
    "parties",
    "chunk_id",
    "text",
    "context",
    "headings",
    "modality",
    "pages",
    "page",
    "bbox",
)


class FileRecords:
    """The tenant's extracted fields from the bundle, filtered like RecordStore."""

    def __init__(self, records: list[Record]) -> None:
        self.records = records

    def load(self, user_id: str, scope: ChatScope | None = None) -> list[Record]:
        return in_scope(list(self.records), scope) if user_id == TENANT else []


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as file:
        for row in rows:
            file.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n"
            )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def config(bundle: Path = BUNDLE) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((bundle / "config.json").read_text())
    return result


def choose_questions(
    items: list[GoldenItem], documents: set[str], baseline_run: dict[str, Any]
) -> dict[str, Any]:
    """The subset's questions, and its threshold from how a recorded A1 dev run did on
    them. Any question can be chosen, however it is scored: the judge decides in CI too
    what the rules can't."""
    candidates = sorted(
        (
            item
            for item in items
            if item.split == "dev" and all(r.doc_id in documents for r in item.relevant)
        ),
        key=lambda item: item.id,
    )
    questions = [
        item.id
        for kind, quota in QUOTAS.items()
        for item in [i for i in candidates if i.type == kind][:quota]
    ]
    verdicts = {i["id"]: bool(i["correct"]) for i in baseline_run["items"]}
    correct = sum(verdicts.get(q, False) for q in questions)
    return {
        "questions": questions,
        "baseline": f"{correct}/{len(questions)} in {baseline_run['run_id']}",
        "min_correct": correct - SLACK,
    }


def export_bundle(
    client: QdrantClient,
    alias: str,
    records: list[Record],
    allowed: set[str],
    bundle: Path = BUNDLE,
) -> tuple[int, int]:
    """Write the chunks and fields of the documents in `allowed` (IDs in the index)."""
    collection = client.get_aliases()
    target = next(
        a.collection_name for a in collection.aliases if a.alias_name == alias
    )
    metadata = client.get_collection(target).config.metadata or {}
    rows: list[dict[str, Any]] = []
    offset = None
    while True:
        batch, offset = client.scroll(
            alias,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="user_id", match=models.MatchValue(value=TENANT)
                    )
                ]
            ),
            with_payload=True,
            limit=256,
            offset=offset,
        )
        for point in batch:
            payload = point.payload or {}
            if payload.get("doc_id") in allowed:
                kept_payload = {k: payload[k] for k in PAYLOAD if k in payload}
                rows.append({"id": str(point.id), **kept_payload})
        if offset is None:
            break
    bundle.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda row: (row["doc_id"], row["chunk_id"]))
    write_jsonl(bundle / "chunks.jsonl.gz", rows)
    kept = sorted((r for r in records if r.doc_id in allowed), key=lambda r: r.doc_id)
    write_jsonl(bundle / "records.jsonl.gz", [asdict(r) for r in kept])
    index = bundle / "index.json"
    index.write_text(json.dumps(metadata.get("config") or {}, indent=1) + "\n")
    return len(rows), len(kept)


def restore(
    client: QdrantClient, bundle: Path, embedder: Embedder, encoder: SparseEncoder
) -> int:
    """Index the bundle's chunks into COLLECTION, from scratch."""
    rows = read_jsonl(bundle / "chunks.jsonl.gz")
    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    create_collection(client, COLLECTION, embedder.model.dim)
    for start in range(0, len(rows), EMBED_BATCH):
        batch = rows[start : start + EMBED_BATCH]
        texts = [
            "\n".join(
                part
                for part in (
                    r.get("context", ""),
                    " > ".join(r.get("headings") or []),
                    r["text"],
                )
                if part
            )
            for r in batch
        ]
        vectors = embed_passages(embedder, texts)
        client.upsert(
            COLLECTION,
            [
                models.PointStruct(
                    id=r["id"],
                    vector={DENSE: vector, SPARSE: encoder.document(text)},
                    payload={k: v for k, v in r.items() if k != "id"},
                )
                for r, text, vector in zip(batch, texts, vectors, strict=True)
            ],
        )
    return len(rows)


def run_ci(
    bundle: Path,
    settings: Settings,
    index: IndexSettings,
    tracing: Tracing | None = None,
    prompts: PromptStore | None = None,
    judge_models: Models | None = None,
    record: bool = False,
) -> int:
    """Restore the bundle, ask the subset, write a report; non-zero below the threshold.

    `prompts` picks the prompt versions to evaluate: the code's by default, or the
    versions pinned from a Langfuse label such as `candidate` before a promotion.
    `judge_models` with `settings.judge_model` decide the answers the rules can't.
    `record` saves to evals/runs, for docs/RESULTS.md, instead of evals/scratch."""
    setup = config(bundle)
    index_config = IndexConfig.model_validate(
        json.loads((bundle / "index.json").read_text())
    )
    client = QdrantClient(url=index.qdrant_url)
    embedder = TeiEmbedder(index.tei_embed_url, MODELS[index_config.embedder])
    encoder = SparseEncoder(stem=index_config.stem)
    started = time.monotonic()
    restored = restore(client, bundle, embedder, encoder)
    print(
        f"Indexed {restored} chunks into {COLLECTION} in {time.monotonic() - started:.0f}s"
    )

    records = FileRecords(
        [Record(**r) for r in read_jsonl(bundle / "records.jsonl.gz")]
    )
    retriever = Retriever(client, COLLECTION, embedder, encoder)
    by_id = {i.id: i for i in read_items()}
    items: list[GoldenItem] = [by_id[i] for i in setup["questions"]]
    graph, variant = build_graph(), GRAPH_VARIANTS["A1"]
    tracing = tracing or Tracing()
    prompts = prompts or PromptStore()
    used = prompts.describe()
    if prompts.remote and prompts.label != PRODUCTION:
        used["production"] = production_versions(prompts.client)
    print(f"Prompts: {used['label']} {used['versions']}")
    models_ = AgentModels(settings)
    results = []
    for item in items:
        try:
            final, usage, seconds, _ = answer_one(
                graph,
                item,
                variant,
                models_,
                retriever,
                records,
                tracing,
                None,
                prompts,
            )
            answer = str(final.get("answer", ""))
            plain = strip_citations(answer)
            verdict, method = score(item, plain), "rule"
            if verdict is None and judge_models and settings.judge_model:
                verdict, _ = judge(
                    judge_models, settings.judge_model, item, plain, run_name="eval-ci"
                )
                method = "judge"
            error = None
        except Exception as exception:  # a crash is a failure, not a broken job
            CHATS.labels("failed").inc()
            answer, verdict, method, error, seconds, usage = (
                "",
                False,
                "rule",
                repr(exception),
                0.0,
                None,
            )
        if verdict is None:
            method = "undecided"  # counted as a failure, listed for a hand check
        results.append(
            {
                "id": item.id,
                "type": item.type,
                "language": item.language,
                "question": item.question,
                "reference": item.answer,
                "answer": answer,
                "correct": verdict is True,
                "method": method,
                "error": error,
                "seconds": seconds,
                "cost_usd": usage.cost if usage else None,
            }
        )
        print(f"  {item.id}: {'ok' if verdict is True else 'FAILED'}")
    report = summarise(results, setup) | {"prompts": used}
    folder = RUNS_DIR if record else SCRATCH_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    saved = {
        "kind": "ci",
        "created": stamp,
        "route": "gateway" if settings.llm_gateway_url else "direct",
        "judge_model": settings.judge_model if judge_models else None,
        "summary": report,
        "items": results,
    }
    path = folder / f"{stamp}-ci.json"
    path.write_text(json.dumps(saved, ensure_ascii=False, indent=1))
    markdown = render(report, results)
    print(markdown)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as file:
            file.write(markdown + "\n")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as file:
            file.write(f"record={path}\n")
    return 0 if report["passed"] else 1


def summarise(results: list[dict[str, Any]], setup: dict[str, Any]) -> dict[str, Any]:
    correct = sum(r["correct"] for r in results)
    by_type: dict[str, list[int]] = {}
    for r in results:
        by_type.setdefault(r["type"], [0, 0])
        by_type[r["type"]][0] += r["correct"]
        by_type[r["type"]][1] += 1
    costs = [r["cost_usd"] for r in results if r["cost_usd"] is not None]
    return {
        "questions": len(results),
        "correct": correct,
        "threshold": setup["min_correct"],
        "baseline": setup.get("baseline"),
        "passed": correct >= setup["min_correct"],
        "by_type": {t: f"{c}/{n}" for t, (c, n) in sorted(by_type.items())},
        "judged": sum(r.get("method") == "judge" for r in results),
        "undecided": sum(r.get("method") == "undecided" for r in results),
        "errors": Counter(r["error"].split("(")[0] for r in results if r["error"]),
        "cost_usd": round(sum(costs), 4) if costs else None,
    }


def prompts_line(prompts: dict[str, Any] | None) -> str:
    if not prompts or prompts.get("label") == "code":
        return "the repository's"
    versions = ", ".join(
        f"{name} {'code' if version is None else f'v{version}'}"
        for name, version in prompts.get("versions", {}).items()
    )
    return f"Langfuse `{prompts['label']}` ({versions})"


def render(report: dict[str, Any], results: list[dict[str, Any]]) -> str:
    verdict = "passed" if report["passed"] else "FAILED"
    lines = [
        f"## Evaluation subset: {verdict}",
        "",
        f"{report['correct']}/{report['questions']} correct; the threshold is"
        f" {report['threshold']} (baseline {report['baseline']}). Cost ${report['cost_usd']}.",
        "",
        f"Decided by the judge: {report.get('judged', 0)}; undecided, counted as"
        f" failures: {report.get('undecided', 0)}.",
        "",
        f"Prompts: {prompts_line(report.get('prompts'))}.",
        "",
        "| Type | Correct |",
        "| --- | --- |",
        *[f"| {t} | {v} |" for t, v in report["by_type"].items()],
    ]
    failed = [r for r in results if not r["correct"]]
    if failed:
        lines += ["", "| Failed question | Expected | Answer |", "| --- | --- | --- |"]
        for r in failed:
            answer = (
                (r["error"] or r["answer"]).replace("|", "\\|").replace("\n", " ")[:200]
            )
            undecided = " (undecided)" if r.get("method") == "undecided" else ""
            lines.append(f"| {r['id']}{undecided} | {r['reference']} | {answer} |")
    return "\n".join(lines)
