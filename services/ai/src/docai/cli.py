"""Command-line entry points for the Python AI services."""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from docai.core.llm import chat_model
from docai.core.logs import configure_logging
from docai.core.settings import Settings
from docai.core.tracing import Tracing, setup_tracing

SMOKE_PROMPT = (
    "Reply with one short sentence confirming that you received this message."
)


def smoke(settings: Settings, tracing: Tracing, role: str) -> int:
    """One real model call, to check the API key, the model name and tracing."""
    spec = settings.chat_model if role == "chat" else settings.fast_model
    reply = chat_model(spec, settings).invoke(
        SMOKE_PROMPT, config={"callbacks": tracing.callbacks, "run_name": "docai-smoke"}
    )
    tracing.flush()
    print(f"model: {spec}")
    print(f"reply: {reply.text.strip()}")
    print(f"trace: {tracing.last_trace_url() or 'tracing off'}")
    return 0


def corpus(action: str) -> int:
    """Build or check the evaluation corpus. Needs no API keys."""
    from docai.corpus.manifest import MANIFEST_PATH, read_manifest, sha256_of
    from docai.corpus.splits import split_problems, split_summary

    if action == "build":
        from docai.corpus.build import build_corpus

        records = build_corpus()
        print(f"Wrote {len(records)} documents to {MANIFEST_PATH}")
    else:
        records = read_manifest()
        problems = split_problems(records)
        problems += [
            f"{r.doc_id}: missing or changed file {r.path}"
            for r in records
            if not r.file.exists() or sha256_of(r.file) != r.sha256
        ]
        for problem in problems:
            print(f"problem: {problem}")
        if problems:
            return 1
        print(f"{len(records)} documents, files match the manifest, no split leakage")
    for source, counts in split_summary(records).items():
        print(
            f"  {source:10} "
            + "  ".join(f"{split}={n}" for split, n in sorted(counts.items()))
        )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Documents AI services")
    commands = result.add_subparsers(dest="command", required=True)
    smoke_parser = commands.add_parser(
        "smoke", help="Make one model call through LangChain"
    )
    smoke_parser.add_argument(
        "--model",
        choices=["fast", "chat"],
        default="fast",
        help="Which configured model to call (FAST_MODEL or CHAT_MODEL)",
    )
    commands.add_parser(
        "graph", help="Print the chat graph as a Mermaid diagram, from the code"
    )
    corpus_parser = commands.add_parser("corpus", help="Evaluation corpus")
    corpus_parser.add_argument(
        "action",
        choices=["build", "check"],
        help="build: download, select and generate documents; check: verify files and splits",
    )
    evals_parser = commands.add_parser("evals", help="Golden set and evaluation runs")
    evals = evals_parser.add_subparsers(dest="action", required=True)
    draft_parser = evals.add_parser("draft", help="Write the golden-set review sheet")
    draft_parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing review sheet"
    )
    evals.add_parser(
        "apply-review", help="Turn the reviewed sheet into evals/golden/v1.jsonl"
    )
    run_parser = evals.add_parser("run", help="Run a variant over a golden split")
    run_parser.add_argument("--variant", default="V0")
    run_parser.add_argument("--split", choices=["dev", "test"], default="dev")
    run_parser.add_argument(
        "--limit", type=int, help="Only the first N items, spread across question types"
    )
    run_parser.add_argument(
        "--ids", help="Only these golden item IDs, comma-separated (scratch runs)"
    )
    run_parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Graph variants: call the models afresh, for real latency (costs a full run)",
    )
    run_parser.add_argument(
        "--dry-run", action="store_true", help="Estimate calls and cost; call nothing"
    )
    run_parser.add_argument(
        "--record", action="store_true", help="Save to evals/runs/ for docs/RESULTS.md"
    )
    run_parser.add_argument("--prompts", help=PROMPTS_HELP)
    evals.add_parser("report", help="Write docs/RESULTS.md from the recorded runs")
    rescore_parser = evals.add_parser(
        "rescore",
        help="Score the recorded answers again after a change to the rules, the judge's"
        " prompt or a golden answer; audit what changes",
    )
    rescore_parser.add_argument("--runs", help="Only these run IDs, comma-separated")
    rescore_mode = rescore_parser.add_mutually_exclusive_group()
    rescore_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Call and write nothing: what the rules change, and the judge calls needed",
    )
    rescore_mode.add_argument(
        "--write", action="store_true", help="Update the runs in evals/runs"
    )
    ci_parser = evals.add_parser(
        "ci",
        help="The CI evaluation subset: restore evals/ci into Qdrant, ask, check the threshold",
    )
    ci_parser.add_argument("--prompts", help=PROMPTS_HELP)
    ci_parser.add_argument(
        "--record", action="store_true", help="Save to evals/runs/ for docs/RESULTS.md"
    )
    evals.add_parser(
        "ci-bundle",
        help="Write evals/ci from the local stack and the latest recorded A1 dev run",
    )
    faith_parser = evals.add_parser(
        "faithfulness",
        help="Judge whether a recorded run's answers are supported by their citations",
    )
    faith_parser.add_argument(
        "run", help="Run ID: a file name in evals/runs without .json"
    )
    retrieval_parser = evals.add_parser(
        "retrieval", help="Score a retrieval variant (V1-V6, E1-E3, R1)"
    )
    retrieval_parser.add_argument("--variant", required=True)
    retrieval_parser.add_argument("--split", choices=["dev", "test"], default="dev")
    retrieval_parser.add_argument(
        "--record", action="store_true", help="Save to evals/runs/ for docs/RESULTS.md"
    )
    retrieval_parser.add_argument(
        "--rebuild", action="store_true", help="Rebuild the variant's collection"
    )
    retrieval_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count chunks and estimate model costs; call nothing",
    )
    retrieval_parser.add_argument(
        "--cache-queries",
        action="store_true",
        help="Reuse cached query embeddings and reranker scores (latency isn't real)",
    )
    extraction_parser = evals.add_parser(
        "extraction", help="Score extracted fields against ground truth"
    )
    extraction_parser.add_argument(
        "--split",
        choices=["dev", "test", "train"],
        default="dev",
        help="Prompts are tuned on dev; train and test are both unseen",
    )
    extraction_parser.add_argument(
        "--limit", type=int, help="Only the first N documents"
    )
    extraction_parser.add_argument(
        "--record", action="store_true", help="Save to evals/runs/ for docs/RESULTS.md"
    )

    ingest_parser = commands.add_parser("ingest", help="The ingestion worker")
    ingest = ingest_parser.add_subparsers(dest="action", required=True)
    ingest.add_parser("worker", help="Process uploads from the SQS queue")
    file_parser = ingest.add_parser(
        "file", help="Run the pipeline on local files, without AWS"
    )
    file_parser.add_argument("paths", nargs="+", type=Path)
    file_parser.add_argument(
        "--out", type=Path, help="Write <name>/document.json and figures/ here"
    )
    setup_parser = ingest.add_parser(
        "setup", help="Download the OCR models and, optionally, Docling's models"
    )
    setup_parser.add_argument(
        "--docling-models",
        type=Path,
        help="Directory for Docling's layout and table models",
    )

    index_parser = commands.add_parser(
        "index", help="Search index versions: Qdrant collections behind the alias"
    )
    index = index_parser.add_subparsers(dest="action", required=True)
    index.add_parser("list", help="Versions with their configs, statuses and sizes")
    build_parser = index.add_parser(
        "build", help="Build a new version from the stored parsed/ artifacts"
    )
    build_parser.add_argument(
        "--version", type=int, help="Version number (default: the next one)"
    )
    build_parser.add_argument("--chunker", choices=["fixed", "structure"])
    build_parser.add_argument("--chunk-tokens", type=int)
    build_parser.add_argument("--context", choices=["none", "header", "llm"])
    build_parser.add_argument("--embedder", help="A TEI model key, e.g. e5-small")
    build_parser.add_argument(
        "--stem", type=int, help="BM25 prefix stemming length; 0 keeps whole words"
    )
    build_parser.add_argument(
        "--tei-url", help="TEI serving the embedder, if not TEI_EMBED_URL"
    )
    build_parser.add_argument(
        "--switch", action="store_true", help="Make the version live once it's built"
    )
    for name, help_text in (
        ("switch", "Point the alias at a built version"),
        ("drop", "Delete a version that isn't live"),
    ):
        index.add_parser(name, help=help_text).add_argument(
            "--version", type=int, required=True
        )

    security_parser = commands.add_parser(
        "security", help="Prompt-injection and cross-tenant attacks (Stage 06)"
    )
    security = security_parser.add_subparsers(dest="action", required=True)
    security.add_parser(
        "build", help="Render the injection fixtures; writes corpus/injection.jsonl"
    )
    security_seed = security.add_parser(
        "seed", help="Load the attacked library and the canary's tenant"
    )
    security_seed.add_argument(
        "--timeout", type=float, default=30, help="Minutes to wait (default: 30)"
    )
    security_seed.add_argument(
        "--reprocess", action="store_true", help="Upload loaded documents again"
    )
    attack_parser = security.add_parser("run", help="Ask the attacks")
    attack_parser.add_argument("--variant", choices=["unguarded", "A1"], required=True)
    attack_parser.add_argument(
        "--repeats", type=int, default=3, help="Trials per attack (default: 3)"
    )
    attack_parser.add_argument("--ids", help="Only these attacks, comma-separated")
    attack_parser.add_argument(
        "--record", action="store_true", help="Save under evals/security/runs"
    )

    prompts_parser = commands.add_parser(
        "prompts", help="The chat prompts' versions and labels in Langfuse (Stage 10a)"
    )
    prompts_actions = prompts_parser.add_subparsers(dest="action", required=True)
    prompts_actions.add_parser(
        "push", help="Label each prompt's code version candidate, creating changed ones"
    )
    prompts_actions.add_parser(
        "promote",
        help="Move production to the versions a passed CI evaluation ran on",
    ).add_argument(
        "--evaluation",
        required=True,
        type=Path,
        help="The record of `docai evals ci --prompts candidate` (evals/scratch/*-ci.json)",
    )
    prompts_actions.add_parser("rollback", help="Move production back to previous")
    prompts_actions.add_parser(
        "status", help="Where production and candidate differ from the repository"
    )

    gateway_parser = commands.add_parser(
        "gateway", help="The LLM gateway's key and budgets (Stage 10b)"
    )
    gateway_actions = gateway_parser.add_subparsers(dest="action", required=True)
    gateway_actions.add_parser(
        "setup",
        help="Create the services' key and the per-user daily budget in the gateway",
    ).add_argument(
        "--url",
        default="http://localhost:4000",
        help="The gateway (default: http://localhost:4000)",
    )

    seed_parser = commands.add_parser(
        "seed", help="Load the corpus into the local stack (S3 and DynamoDB)"
    )
    who = seed_parser.add_mutually_exclusive_group()
    who.add_argument(
        "--user-id",
        help="Owner of the documents (default: corpus, an evaluation tenant)",
    )
    who.add_argument(
        "--email",
        help="Owner by account; the password is read from SEED_PASSWORD",
    )
    seed_parser.add_argument(
        "--register", action="store_true", help="Create the --email account if needed"
    )
    seed_parser.add_argument(
        "--api-url", default="http://localhost", help="For --email (default: nginx)"
    )
    seed_parser.add_argument(
        "--source", action="append", choices=["cuad", "sroie", "synthetic"]
    )
    seed_parser.add_argument(
        "--split", action="append", choices=["train", "dev", "test"]
    )
    seed_parser.add_argument("--limit", type=int, help="Only the first N documents")
    seed_parser.add_argument(
        "--fixture",
        action="append",
        help="Also a prompt-injection fixture by ID (corpus/injection.jsonl), e.g. for the demo",
    )
    seed_parser.add_argument(
        "--wait", action="store_true", help="Wait until every document is processed"
    )
    seed_parser.add_argument(
        "--timeout", type=float, default=60, help="Minutes to wait (default: 60)"
    )
    seed_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be uploaded"
    )
    seed_parser.add_argument(
        "--reprocess",
        action="store_true",
        help="Upload loaded documents again, e.g. after a pipeline change",
    )
    return result


def evaluate(args: argparse.Namespace) -> int:
    """`docai evals ...`. Only `run` needs model settings."""
    if args.action == "draft":
        from docai.evals.review import draft_review

        count = draft_review(force=args.force)
        print(f"Wrote {count} draft questions to evals/golden/review-v1.csv")
        return 0
    if args.action == "apply-review":
        from docai.evals.review import finish_review

        print(
            f"Wrote {len(finish_review())} reviewed questions to evals/golden/v1.jsonl"
        )
        return 0
    if args.action == "report":
        from docai.evals.report import write_report

        print(f"Wrote {write_report()}")
        return 0
    if args.action == "extraction":
        return evaluate_extraction(args)
    if args.action == "rescore":
        return rescore_runs(args)
    if args.action == "ci":
        from docai.core.settings import IndexSettings
        from docai.evals.ci import BUNDLE, run_ci
        from docai.evals.llm import EvalModels, LlmCache

        settings = Settings()
        tracing = setup_tracing(settings)
        store = prompt_store(args.prompts, tracing)
        if store is None:
            return 2
        if settings.judge_model is None:
            print(
                "JUDGE_MODEL is not set: answers the rules can't decide count as failures"
            )
        try:
            return run_ci(
                BUNDLE,
                settings,
                IndexSettings(),
                tracing,
                store,
                EvalModels(settings, tracing, LlmCache()),
                record=args.record,
            )
        finally:
            tracing.flush()
    if args.action == "ci-bundle":
        return write_ci_bundle()
    if args.action == "faithfulness":
        from docai.evals.faithfulness import add_faithfulness
        from docai.evals.llm import EvalModels, LlmCache
        from docai.evals.runner import RUNS_DIR

        settings = Settings()
        if settings.judge_model is None:
            print("JUDGE_MODEL is not set")
            return 2
        tracing = setup_tracing(settings)
        rate = add_faithfulness(
            RUNS_DIR / f"{args.run}.json",
            EvalModels(settings, tracing, LlmCache()),
            settings.judge_model,
        )
        tracing.flush()
        print(f"{args.run}: faithfulness {rate}")
        return 0
    if args.action == "retrieval":
        return evaluate_retrieval(args)

    from docai.evals.answers import GRAPH_VARIANTS

    if args.variant in GRAPH_VARIANTS:
        return evaluate_graph(args)

    from docai.evals.golden import read_items
    from docai.evals.llm import EvalModels, LlmCache
    from docai.evals.runner import (
        GOLDEN_DATASET,
        estimate,
        experiment_records,
        run,
        subset,
    )
    from docai.evals.variants import VARIANTS

    settings = Settings()
    if settings.judge_model is None:
        print("JUDGE_MODEL is not set")
        return 2
    tracing = setup_tracing(settings)
    variant = VARIANTS[args.variant]
    items = subset([i for i in read_items() if i.split == args.split], args.limit)
    models = EvalModels(settings, tracing, LlmCache())
    plan = estimate(models, variant, items, settings.chat_model, settings.judge_model)
    cost = (
        "unknown"
        if plan.estimated_cost_usd is None
        else f"${plan.estimated_cost_usd:.4f}"
    )
    print(
        f"{variant.name} on {args.split}: {plan.items} questions, {plan.uncached_answer_calls} answer calls not cached"
        f" (~{plan.estimated_input_tokens:,} input tokens, ~{cost} with {settings.chat_model}),"
        f" at most {plan.judge_calls_at_most} judge calls with {plan.judge_model}"
    )
    if args.dry_run:
        return 0
    result = run(
        models, variant, args.split, items, settings.chat_model, settings.judge_model
    )
    tracing.flush()
    summary = result.summary()
    path = result.save(record=args.record)
    if args.record and tracing.enabled:
        logged = tracing.log_experiment(
            dataset=GOLDEN_DATASET,
            run_name=result.run_id,
            description=variant.description,
            records=experiment_records(result, items),
            metadata={
                "variant": variant.name,
                "split": args.split,
                "answer_model": settings.chat_model,
            },
        )
        print(
            f"Langfuse experiment {'logged' if logged else 'not logged (see warning)'}: {GOLDEN_DATASET} / {result.run_id}"
        )
    print(
        f"accuracy {summary['accuracy']} ({summary['correct']}/{summary['n']}), judged {summary['judged']}, saved to {path}"
    )
    for name, part in summary["by_type"].items():  # type: ignore[attr-defined]
        print(f"  {name:14} {part['correct']}/{part['n']}")
    return 0


def evaluate_graph(args: argparse.Namespace) -> int:
    """`docai evals run --variant A1`: the chat graph over a golden split."""
    from docai.agent.models import AgentModels
    from docai.agent.records import RecordStore
    from docai.api.app import retriever_factory
    from docai.core.settings import AwsSettings, IndexSettings
    from docai.evals.answers import (
        GRAPH_VARIANTS,
        GraphCache,
        evaluate_answers,
    )
    from docai.evals.golden import read_items
    from docai.evals.llm import EvalModels, LlmCache
    from docai.evals.runner import RUNS_DIR, subset

    settings = Settings()
    if settings.judge_model is None:
        print("JUDGE_MODEL is not set")
        return 2
    variant = GRAPH_VARIANTS[args.variant]
    items = subset([i for i in read_items() if i.split == args.split], args.limit)
    if args.ids:
        wanted = set(args.ids.split(","))
        items = [i for i in items if i.id in wanted]
        if args.record:
            print("--ids runs are for diagnosis and can't be recorded")
            return 2
    measured = [
        run["summary"]["cost_per_query_usd"]
        for path in sorted(RUNS_DIR.glob("*.json"))
        for run in [json.loads(path.read_text())]
        if run.get("variant", "").startswith("A1")
        and run["summary"].get("cost_per_query_usd")
    ]
    per_query = measured[-1] if measured else 0.004
    print(
        f"{variant.name} on {args.split}: {len(items)} questions, at most about"
        f" ${per_query * len(items):.2f} with {settings.chat_model} at"
        f" ${per_query:.4f} per question ({'measured' if measured else 'assumed'});"
        " cached replies cost nothing"
    )
    if args.dry_run:
        return 0
    tracing = setup_tracing(settings)
    store = prompt_store(args.prompts, tracing)
    if store is None:
        return 2
    cache = None if args.no_cache else GraphCache()
    run = evaluate_answers(
        variant,
        args.split,
        items,
        AgentModels(settings, cache),
        cache,
        retriever_factory(IndexSettings())(),
        RecordStore(AwsSettings()),
        EvalModels(settings, tracing, LlmCache()),
        settings.judge_model,
        tracing,
        store,
    )
    tracing.flush()
    path = run.save(record=args.record)
    s = run.summary()
    print(
        f"accuracy {s['accuracy']} ({s['correct']}/{s['n']}), judged {s['judged']},"
        f" citation precision {s['citation_precision']}, {s['llm_calls_per_query']}"
        f" calls and ${s['cost_per_query_usd']} per question, p50"
        f" {s['latency_seconds']['p50']}s; {s['paid_calls']} calls paid now; saved to {path}"
    )
    for name, part in s["by_type"].items():
        print(f"  {name:14} {part['correct']}/{part['n']}")
    for name, count in s["failures"].items():
        print(f"  failure: {name}: {count}")
    return 0


def rescore_runs(args: argparse.Namespace) -> int:
    """`docai evals rescore`: the recorded runs' answers scored with today's rules."""
    from docai.evals import injection
    from docai.evals.golden import read_items
    from docai.evals.llm import EvalModels, LlmCache
    from docai.evals.rescore import rescore_run, save_run, uncached, write_audit
    from docai.evals.runner import RUNS_DIR

    cache = LlmCache()
    if args.dry_run:
        tracing, models = Tracing(), None
        default_judge = os.environ.get("JUDGE_MODEL")
    else:
        settings = Settings()
        tracing = setup_tracing(settings)
        models = EvalModels(settings, tracing, cache)
        default_judge = settings.judge_model
    golden = {i.id: i for i in read_items()}
    wanted = set(args.runs.split(",")) if args.runs else None
    rescored = []
    paths = [
        *sorted(RUNS_DIR.glob("*.json")),
        *sorted(injection.RUNS_DIR.glob("*.json")),
    ]
    for path in paths:
        if wanted is not None and path.stem not in wanted:
            continue
        outcome = rescore_run(path, golden, models, default_judge)
        if outcome is None:
            continue
        result, run = outcome
        rescored.append(result)
        if args.write and result.changes:
            save_run(path, run)
        (b, n), (a, _) = result.before, result.after
        line = f"{result.run_id}: {b}/{n} -> {a}/{n}, {len(result.changes)} verdicts changed"
        if result.note:
            line += f" ({result.note})"
        print(line + (f", {len(result.pending)} for the judge" if args.dry_run else ""))
    tracing.flush()
    if args.dry_run:
        pending = [p for r in rescored for p in r.pending]
        print(
            f"Judge calls: {len(pending)}, {uncached(pending, cache)} not cached;"
            " nothing called or written"
        )
        return 0
    if any(r.changes for r in rescored):
        print(f"Audit of the changes: {write_audit(rescored)}")
    else:
        print("No verdict changed")
    if not args.write:
        print("Nothing written to evals/runs; --write updates the runs")
    return 0


def evaluate_retrieval(args: argparse.Namespace) -> int:
    """`docai evals retrieval`: index the corpus for a variant and score it."""
    from decimal import Decimal

    from qdrant_client import QdrantClient

    from docai.core.embeddings import MODELS, VectorCache, embedder
    from docai.core.prices import cost
    from docai.core.rerank import RERANKERS, CachedReranker, Reranker, TeiReranker
    from docai.core.retrieval import Retriever
    from docai.core.settings import IndexSettings, PipelineSettings
    from docai.corpus.manifest import read_manifest
    from docai.evals.golden import read_items
    from docai.evals.llm import EVALS_DIR
    from docai.evals.retrieval import VARIANTS, build_index, evaluate
    from docai.ingest.cache import DiskCache
    from docai.ingest.chunking import count_tokens
    from docai.ingest.context import CHUNKS_PER_CALL
    from docai.ingest.indexer import chunk_document
    from docai.ingest.models import IngestModels
    from docai.ingest.pipeline import Pipeline

    variant = VARIANTS.get(args.variant)
    if variant is None:
        print(f"Unknown variant {args.variant}; known: {', '.join(VARIANTS)}")
        return 2
    settings, index_settings = PipelineSettings(), IndexSettings()
    tracing = setup_tracing(settings)
    cache = DiskCache(settings.ingest_cache_dir) if settings.ingest_cache_dir else None
    models_ = IngestModels(settings, tracing, cache)
    pipeline = Pipeline(settings, models_, cache)
    documents = [
        (
            r,
            pipeline.process(
                r.file.read_bytes(),
                r.mime_type,
                Path(r.path).name,
                {"corpus_doc_id": r.doc_id},
            )[0],
        )
        for r in read_manifest()
    ]
    chunk_counts = [
        len(chunk_document(parsed, variant.index)) for _, parsed in documents
    ]
    context_model = index_settings.context_model or settings.extract_model
    if args.dry_run:
        print(
            f"{variant.name}: {sum(chunk_counts)} chunks from {len(documents)} documents"
        )
        if variant.index.context == "llm":
            calls, tokens_in = 0, 0
            for (_, parsed), n in zip(documents, chunk_counts):
                batches = -(-n // CHUNKS_PER_CALL)
                document_tokens = count_tokens(parsed.extraction.summary) + sum(
                    c.tokens for c in chunk_document(parsed, variant.index)
                )
                calls += batches
                tokens_in += batches * (document_tokens + 300) + document_tokens
            tokens_out = 60 * sum(chunk_counts)
            estimate = cost(context_model, tokens_in, tokens_out) or Decimal(0)
            print(
                f"  contexts: {calls} calls, about {tokens_in:,} input and {tokens_out:,}"
                f" output tokens, at most ${estimate:.2f} with {context_model}"
                " (before cache hits and prefix caching)"
            )
        if MODELS[variant.index.embedder].provider == "gemini":
            tokens = sum(
                sum(c.tokens for c in chunk_document(parsed, variant.index))
                for _, parsed in documents
            )
            print(f"  embeddings: about {tokens:,} tokens, ${0.20 * tokens / 1e6:.3f}")
        return 0
    vectors = VectorCache(EVALS_DIR / "cache" / "vectors.sqlite")
    dense = embedder(
        variant.index.embedder, settings, index_settings.tei_embed_url, vectors
    )
    client = QdrantClient(
        url=index_settings.qdrant_url,
        api_key=index_settings.qdrant_api_key.get_secret_value()
        if index_settings.qdrant_api_key
        else None,
        timeout=120,
    )
    stats = build_index(
        client,
        variant.index,
        documents,
        dense,
        models_,
        context_model,
        expected_chunks=sum(chunk_counts),
        rebuild=args.rebuild,
    )
    if stats.built:
        print(
            f"Built {stats.collection}: {stats.chunks} chunks in {stats.seconds}s;"
            f" {stats.context_calls} context calls, {stats.paid_calls} of them paid now;"
            f" from scratch: contexts ${stats.context_cost_usd},"
            f" embeddings ${stats.embedding_cost_usd}"
        )
    # The index is built from cached embeddings, but queries are embedded and reranked
    # afresh, so latencies are real, unless --cache-queries.
    reranker: Reranker | None = None
    if variant.reranker:
        reranker = TeiReranker(
            index_settings.tei_rerank_url, RERANKERS[variant.reranker]
        )
        if args.cache_queries:
            reranker = CachedReranker(reranker, vectors)
    query_embedder = dense if args.cache_queries else getattr(dense, "inner", dense)
    retriever = Retriever(
        client,
        stats.collection,
        query_embedder if variant.retrieval.dense else None,
        variant.index.sparse,
        reranker,
    )
    items = [i for i in read_items() if i.split == args.split]
    run = evaluate(variant, args.split, items, retriever, stats)
    run.query_cache = args.cache_queries
    tracing.flush()
    path = run.save(record=args.record)
    summary = run.summary()
    overall = summary["overall"]
    print(
        f"{variant.name} on {args.split} ({overall['n']} questions): recall@5"
        f" {overall['recall@5']}, recall@10 {overall['recall@10']}, MRR@10"
        f" {overall['mrr@10']}, nDCG@10 {overall['ndcg@10']}, document recall@10"
        f" {overall['doc_recall@10']}, p50 {summary['latency_seconds']['p50']}s;"
        f" saved to {path}"
    )
    for name, part in summary["by_type"].items():
        print(
            f"  {name:14} recall@10 {part['recall@10']}  MRR {part['mrr@10']}"
            f"  nDCG {part['ndcg@10']}  (n={part['n']})"
        )
    return 0


def evaluate_extraction(args: argparse.Namespace) -> int:
    """`docai evals extraction`: run the pipeline on a split and score its fields.

    Uses INGEST_CACHE_DIR, so documents the worker or an earlier run processed make
    no model calls.
    """
    from docai.core.settings import PipelineSettings
    from docai.corpus.manifest import read_manifest
    from docai.corpus.sroie import KEYS
    from docai.evals.extraction import (
        DocumentResult,
        Truth,
        evaluable,
        new_run,
        score_document,
    )
    from docai.ingest.cache import DiskCache
    from docai.ingest.models import IngestModels
    from docai.ingest.pipeline import Pipeline

    settings = PipelineSettings()
    truth = Truth.load(KEYS)
    records = evaluable([r for r in read_manifest() if r.split == args.split], truth)[
        : args.limit
    ]
    tracing = setup_tracing(settings)
    cache = DiskCache(settings.ingest_cache_dir) if settings.ingest_cache_dir else None
    pipeline = Pipeline(settings, IngestModels(settings, tracing, cache), cache)
    run = new_run(args.split, settings.extract_model, settings.vision_model)
    for number, record in enumerate(records, 1):
        parsed, _ = pipeline.process(
            record.file.read_bytes(),
            record.mime_type,
            Path(record.path).name,
            {"corpus_doc_id": record.doc_id},
        )
        fields, expected, predicted, matched = score_document(
            record, parsed.extraction, truth
        )
        run.documents.append(
            DocumentResult(
                doc_id=record.doc_id,
                source=record.source,
                language=record.language,
                format=record.format,
                category=record.category,
                predicted_category=parsed.extraction.category,
                fields=fields,
                lines_expected=expected,
                lines_predicted=predicted,
                lines_matched=matched,
                cost_usd=str(parsed.cost_usd),
                cached=all(call.cached for call in parsed.calls),
            )
        )
        wrong = [
            f.field for f in fields if f.outcome in ("wrong", "missed", "spurious")
        ]
        print(
            f"{number}/{len(records)} {record.doc_id}: {', '.join(wrong) or 'all fields right'}"
        )
    tracing.flush()
    summary = run.summary()
    path = run.save(record=args.record)
    overall = summary["overall"]
    print(
        f"F1 {overall['f1']} (precision {overall['precision']}, recall {overall['recall']},"
        f" {overall['correct']}/{overall['fields']} fields), category accuracy"
        f" {summary['category_accuracy']}, line items F1 {summary['line_items']['f1']},"
        f" cost ${summary['cost_usd']}; saved to {path}"
    )
    for name, part in summary["by_category"].items():
        print(f"  {name:10} F1 {part['f1']} ({part['correct']}/{part['fields']})")
    return 0


def ingest(args: argparse.Namespace) -> int:
    """`docai ingest ...`. `setup` needs no keys."""
    if args.action == "setup":
        from docai.core.settings import PipelineSettings
        from docai.ingest.setup import (
            download_docling_models,
            download_tokenizer,
            ensure_tessdata,
        )

        tessdata = Path(
            os.environ.get(
                "TESSDATA_DIR", PipelineSettings.model_fields["tessdata_dir"].default
            )
        )
        print(f"OCR models in {ensure_tessdata(tessdata)}")
        print(f"Chunking tokenizer at {download_tokenizer()}")
        if args.docling_models:
            print(f"Docling models in {download_docling_models(args.docling_models)}")
        return 0

    import asyncio

    from docai.core.settings import PipelineSettings, WorkerSettings
    from docai.ingest.cache import DiskCache
    from docai.ingest.models import IngestModels
    from docai.ingest.pipeline import Pipeline

    settings: PipelineSettings = (
        WorkerSettings() if args.action == "worker" else PipelineSettings()
    )
    tracing = setup_tracing(settings)
    cache = DiskCache(settings.ingest_cache_dir) if settings.ingest_cache_dir else None
    pipeline = Pipeline(settings, IngestModels(settings, tracing, cache), cache)
    if isinstance(settings, WorkerSettings):
        from docai.core.telemetry import instrument, serve_metrics
        from docai.ingest.search_index import SearchIndex
        from docai.ingest.store import DocumentStore
        from docai.ingest.worker import Worker, serve

        instrument()
        if settings.metrics_port:
            serve_metrics(settings.metrics_port)
        store = DocumentStore(settings)
        search_index = SearchIndex(
            settings,
            store,
            pipeline.models,
            settings.context_model or settings.extract_model,
        )
        try:
            asyncio.run(serve(Worker(settings, store, pipeline, search_index)))
        finally:
            tracing.flush()
        return 0

    import mimetypes

    for path in args.paths:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parsed, figures = pipeline.process(
            path.read_bytes(), mime_type, path.name, {"file": path.name}
        )
        extraction = parsed.extraction
        print(
            f"{path.name}: {extraction.category} ({extraction.confidence}), {parsed.language},"
            f" {parsed.page_count} pages, {len(parsed.figures)} figures, parser {parsed.parser},"
            f" ${parsed.cost_usd:.5f}, {parsed.timings}"
        )
        print(f"  {extraction.summary}")
        if extraction.fields is not None:
            print(f"  {extraction.fields.model_dump_json(exclude_none=True)}")
        for warning in parsed.warnings:
            print(f"  warning: {warning}")
        if args.out:
            target = args.out / path.stem
            target.mkdir(parents=True, exist_ok=True)
            (target / "document.json").write_text(parsed.model_dump_json(indent=1))
            for figure in figures:
                (target / "figures").mkdir(exist_ok=True)
                (target / "figures" / figure.file_name).write_bytes(figure.data)
    tracing.flush()
    return 0


def index_versions(args: argparse.Namespace) -> int:
    """`docai index ...`: only `build` needs model settings (it runs the worker's)."""
    from docai.core.index_config import IndexConfig
    from docai.core.settings import AwsSettings, IndexSettings, WorkerSettings
    from docai.ingest.search_index import DEFAULT_INDEX, SearchIndex
    from docai.ingest.store import DocumentStore

    if args.action != "build":
        settings = IndexSettings()
        search_index = SearchIndex(settings, DocumentStore(AwsSettings()), None, None)
        if args.action == "switch":
            old = search_index.switch(args.version)
            print(
                f"{settings.qdrant_alias} -> chunks_v{args.version}"
                + (f" (was chunks_v{old}, now retired)" if old else "")
            )
        elif args.action == "drop":
            search_index.drop(args.version)
            print(f"Dropped chunks_v{args.version}")
        else:
            versions = search_index.registry.versions()
            if not versions:
                print("No index versions yet; the worker creates version 1")
            for v in versions:
                points, documents = search_index.counts(v.version)
                print(
                    f"v{v.version}  {v.status:8} {points:7} points {documents:5} documents"
                    f"  {v.config.name}  created {v.metadata.get('createdAt')}"
                )
        return 0

    from docai.ingest.cache import DiskCache
    from docai.ingest.models import IngestModels

    worker_settings = WorkerSettings()
    tracing = setup_tracing(worker_settings)
    cache = (
        DiskCache(worker_settings.ingest_cache_dir)
        if worker_settings.ingest_cache_dir
        else None
    )
    search_index = SearchIndex(
        worker_settings,
        DocumentStore(worker_settings),
        IngestModels(worker_settings, tracing, cache),
        worker_settings.context_model or worker_settings.extract_model,
        tei_embed_url=args.tei_url,
    )
    given = {
        "chunker": args.chunker,
        "chunk_tokens": args.chunk_tokens,
        "context": args.context,
        "embedder": args.embedder,
        "stem": (args.stem or None) if args.stem is not None else DEFAULT_INDEX.stem,
    }
    config = IndexConfig.model_validate(
        DEFAULT_INDEX.model_dump()
        | {k: v for k, v in given.items() if v is not None}
        | {"stem": given["stem"]}
    )
    existing = [v.version for v in search_index.registry.versions()]
    version = args.version or max(existing, default=0) + 1

    def progress(done: int, total: int) -> None:
        if done % 25 == 0 or done == total:
            print(f"  {done}/{total} documents")

    print(f"Building chunks_v{version}: {config.name}")
    stats = search_index.build(version, config, progress)
    tracing.flush()
    print(
        f"Built chunks_v{version} in {stats.seconds}s: {stats.documents} documents,"
        f" {stats.chunks} chunks; {len(stats.skipped)} skipped as deleted,"
        f" {stats.caught_up} caught up, {stats.removed} removed during the build"
    )
    if args.switch:
        old = search_index.switch(version)
        print(
            f"{worker_settings.qdrant_alias} -> chunks_v{version}"
            + (f" (was chunks_v{old}, now retired)" if old else "")
        )
    return 0


def seed_corpus(args: argparse.Namespace) -> int:
    """`docai seed`: needs AWS settings (LocalStack), not model keys."""
    from docai.core.settings import AwsSettings
    from docai.corpus.manifest import read_manifest
    from docai.ingest.seed import DEFAULT_USER, login, seed, wait
    from docai.ingest.store import DocumentStore

    records = [
        r
        for r in read_manifest()
        if (not args.source or r.source in args.source)
        and (not args.split or r.split in args.split)
    ][: args.limit]
    if args.fixture:
        from docai.corpus.injection import MANIFEST_PATH

        fixtures = {r.doc_id: r for r in read_manifest(MANIFEST_PATH)}
        unknown = sorted(set(args.fixture) - set(fixtures))
        if unknown:
            print(f"No such fixtures: {', '.join(unknown)}")
            return 2
        records += [fixtures[f] for f in args.fixture]
    missing = [r.doc_id for r in records if not r.file.exists()]
    if missing:
        print(
            f"{len(missing)} corpus files are missing; run `docai corpus build` first"
        )
        return 1
    if args.email:
        password = os.environ.get("SEED_PASSWORD")
        if not password:
            print("Set SEED_PASSWORD to the account's password")
            return 2
        user_id = login(args.api_url, args.email, password, args.register)
    else:
        user_id = args.user_id or DEFAULT_USER
    store = DocumentStore(AwsSettings())
    result = seed(
        store, records, user_id, dry_run=args.dry_run, reprocess=args.reprocess
    )
    verb = "Would upload" if args.dry_run else "Uploaded"
    print(
        f"{verb} {len(result.uploaded)} documents for user {user_id};"
        f" {len(result.skipped)} already loaded"
    )
    if args.wait and not args.dry_run:
        ids = list(result.document_ids.values())
        counts = wait(store, user_id, ids, args.timeout * 60)
        print("Status: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        return 0 if counts.get("COMPLETED", 0) == len(ids) else 1
    return 0


def write_ci_bundle() -> int:
    """`docai evals ci-bundle`: the CUAD and synthetic part of the evaluation tenant, and
    dev questions about it, with the latest recorded A1 dev run as the baseline."""
    from docai.agent.records import RecordStore
    from docai.core.settings import AwsSettings, IndexSettings
    from docai.core.vectorstore import qdrant_client
    from docai.corpus.manifest import read_manifest
    from docai.evals.answers import TENANT
    from docai.evals.ci import BUNDLE, choose_questions, export_bundle
    from docai.evals.golden import read_items
    from docai.evals.runner import RUNS_DIR
    from docai.ingest.seed import document_id

    corpus = {r.doc_id: r for r in read_manifest() if r.source != "sroie"}
    allowed = {document_id(TENANT, doc) for doc in corpus}
    index = IndexSettings()
    chunks, records = export_bundle(
        qdrant_client(index),
        index.qdrant_alias,
        RecordStore(AwsSettings()).load(TENANT),
        allowed,
    )
    [latest] = sorted(RUNS_DIR.glob("*-A1-dev.json"))[-1:]
    setup = choose_questions(read_items(), set(corpus), json.loads(latest.read_text()))
    (BUNDLE / "config.json").write_text(json.dumps(setup, indent=1) + "\n")
    print(
        f"{chunks} chunks and {records} documents' fields; {len(setup['questions'])}"
        f" questions, {setup['baseline']}; threshold {setup['min_correct']}"
    )
    return 0


def security_attacks(args: argparse.Namespace) -> int:
    """`docai security build|seed|run`."""
    from docai.corpus import injection
    from docai.corpus.manifest import read_manifest, write_manifest

    if args.action == "build":
        records = injection.build_injection()
        write_manifest(records, injection.MANIFEST_PATH)
        print(f"Rendered {len(records)} fixtures into {injection.INJECTION_DIR}")
        return 0

    from docai.core.settings import AwsSettings

    if args.action == "seed":
        from docai.ingest.seed import seed, wait
        from docai.ingest.store import DocumentStore

        fixtures = read_manifest(injection.MANIFEST_PATH)
        w3 = [r for r in read_manifest() if r.doc_id.startswith("syn-w3-")]
        store = DocumentStore(AwsSettings())
        ok = True
        for tenant, records in (
            (
                injection.ATTACKED,
                [*w3, *(r for r in fixtures if r.group == injection.ATTACKED)],
            ),
            (injection.OTHER, [r for r in fixtures if r.group == injection.OTHER]),
        ):
            result = seed(store, records, tenant, reprocess=args.reprocess)
            counts = wait(
                store, tenant, list(result.document_ids.values()), args.timeout * 60
            )
            print(
                f"{tenant}: uploaded {len(result.uploaded)}, {len(result.skipped)} already"
                " loaded; " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            )
            ok = ok and counts.get("COMPLETED", 0) == len(records)
        return 0 if ok else 1

    from docai.agent.models import AgentModels
    from docai.agent.records import RecordStore
    from docai.api.app import retriever_factory
    from docai.core.settings import IndexSettings
    from docai.evals.injection import (
        ATTACKS,
        VARIANTS,
        extraction_check,
        quarantined_documents,
        run_attacks,
        save,
    )

    settings = Settings()
    variant = VARIANTS[args.variant]
    ids = set(args.ids.split(",")) if args.ids else None
    count = sum(1 for a in ATTACKS if not ids or a.id in ids) * args.repeats
    print(f"{variant.name}: {count} questions, at most about ${count * 0.004:.2f}")
    tracing = setup_tracing(settings)
    record_store = RecordStore(AwsSettings())
    run = run_attacks(
        variant,
        AgentModels(settings),
        retriever_factory(IndexSettings())(),
        record_store,
        tracing,
        repeats=args.repeats,
        ids=ids,
    )
    run["extraction"] = extraction_check(record_store)
    run["quarantined_documents"] = quarantined_documents(AwsSettings())
    tracing.flush()
    path = save(run, record=args.record and not ids)
    s = run["summary"]
    print(f"Attack success: {s['attack_success']}")
    for kind, rate in s["by_kind"].items():
        print(f"  {kind}: {rate['succeeded']}/{rate['trials']}")
    print(f"Cross-tenant leaks: {s['cross_tenant_leaks']}")
    print(
        f"Correct despite the attack: {s['correct_despite_attack']}; cost ${s['cost_usd']}"
    )
    unchanged = sum(r["unchanged"] for r in run["extraction"])
    print(
        f"Extraction unchanged by the injected text: {unchanged}/{len(run['extraction'])}"
    )
    print(f"Saved {path}")
    return 0


PROMPTS_HELP = (
    "Langfuse label of the prompts to use, e.g. candidate (needs the LANGFUSE_* "
    "variables); default: the repository's prompts"
)


def prompt_store(label: str | None, tracing: Tracing) -> Any:
    """The prompts an evaluation uses, a label's versions pinned; None, after a
    message, if Langfuse is needed but off or lacks a version of a prompt."""
    from docai.agent.prompt_store import PromptStore

    if label in (None, "code"):
        return PromptStore()
    if tracing.client is None:
        print(f"--prompts {label} needs Langfuse: set the LANGFUSE_* variables")
        return None
    try:
        return PromptStore.pinned(tracing.client, label)
    except Exception as error:
        print(f"--prompts {label}: {error}")
        return None


def manage_prompts(args: argparse.Namespace) -> int:
    """`docai prompts ...`: versions and labels of the chat prompts in Langfuse."""
    from datetime import UTC, datetime

    from docai.agent import prompt_store as store
    from docai.core.settings import CoreSettings

    action = args.action

    tracing = setup_tracing(CoreSettings())
    if tracing.client is None:
        print("docai prompts needs Langfuse: set the LANGFUSE_* variables")
        return 2
    client = tracing.client
    in_sync = True
    if action == "push":
        lines = store.push(client, f"docai prompts push ({git_revision()})")
    elif action == "promote":
        try:
            record = json.loads(args.evaluation.read_text())
            evaluated = store.evaluated_prompts(record)
            lines = store.promote(client, evaluated)
        except (OSError, ValueError) as error:
            print(f"Nothing promoted: {error}")
            return 1
        # The evaluation's record says what it led to, for docs/RESULTS.md.
        record["promotion"] = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "production": evaluated["versions"],
        }
        args.evaluation.write_text(json.dumps(record, ensure_ascii=False, indent=1))
    elif action == "rollback":
        lines = store.rollback(client)
    else:
        lines, in_sync = store.status(client)
    for line in lines:
        print(line)
    tracing.flush()
    if action == "status" and not in_sync:
        print("production differs from the repository")
        return 1
    return 0


def git_revision() -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        dirty = subprocess.run(["git", "diff", "--quiet"], check=False).returncode
        return result.stdout.strip() + (" with local changes" if dirty else "")
    except OSError, subprocess.CalledProcessError:
        return "unknown revision"


def setup_gateway(url: str) -> int:
    """`docai gateway setup`: the services' key and the per-user budget, in the gateway's
    database. Needs LITELLM_MASTER_KEY and LLM_GATEWAY_KEY."""
    from docai.core.gateway import setup
    from docai.core.settings import GatewaySettings

    settings = GatewaySettings()
    for line in setup(
        url,
        settings.litellm_master_key.get_secret_value(),
        settings.llm_gateway_key.get_secret_value(),
        settings.llm_gateway_user_daily_budget,
    ):
        print(line)
    return 0


def chat_graph() -> int:
    """The compiled chat graph as Mermaid, which GitHub renders in a ```mermaid block.
    Dotted edges are conditional. Needs no keys or services."""
    from docai.agent.graph import build_graph

    print(build_graph().get_graph().draw_mermaid())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    configure_logging()
    if args.command == "graph":
        return chat_graph()
    if args.command == "prompts":
        return manage_prompts(args)
    if args.command == "gateway":
        return setup_gateway(args.url)
    if args.command == "corpus":
        return corpus(args.action)
    if args.command == "evals":
        from docai.core.telemetry import evaluation_metrics

        with evaluation_metrics():
            return evaluate(args)
    if args.command == "ingest":
        return ingest(args)
    if args.command == "seed":
        return seed_corpus(args)
    if args.command == "index":
        return index_versions(args)
    if args.command == "security":
        return security_attacks(args)
    settings = Settings()
    tracing = setup_tracing(settings)
    if args.command == "smoke":
        return smoke(settings, tracing, args.model)
    return 2


if __name__ == "__main__":
    sys.exit(main())
