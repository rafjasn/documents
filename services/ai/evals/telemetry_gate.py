"""Stage 10c gate: one trace per question, spanning the API, the graph, Qdrant, TEI, the
gateway and Gemini; and the spans per question, which decide whether evaluation runs can
afford service spans on the Langfuse plan.

    trace <id>...      Summarise these traces (e.g. a `done` event's traceId).
    chats --since T    Every chat trace started by the API since T, e.g. a walkthrough's.
    uploads --since T  Every upload trace since T: spans per new upload.

Each trace is read back through Langfuse's API once it stops growing, and its spans are
counted by service and kind. A trace passes when it has the API's chat span, the
ai-service's request span and graph nodes, Qdrant, the gateway's request and model call, and a
generation, and, when the graph searched, a TEI embedding (a question answered from
extracted fields embeds nothing).

From services/ai, with the LANGFUSE_* variables set:
    uv run --env-file ../../.env python evals/telemetry_gate.py chats --since 2026-10-23T15:00:00Z
"""

import argparse
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docai.core.settings import CoreSettings
from docai.core.tracing import setup_tracing

RUNS = Path(__file__).parent / "runs"
CHAT_ROOT = "POST /api/ai/conversations/:id/stream"
NODES = {"route_intent", "hybrid_search", "generate", "smalltalk", "metadata_tool"}
# The Langfuse Hobby plan's monthly units (traces, spans, generations, scores).
PLAN_UNITS = 50_000
DEV_QUESTIONS = 72


def meta(observation: dict[str, Any]) -> dict[str, Any]:
    raw = observation.get("metadata") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def part(observation: dict[str, Any]) -> str:
    """What the span shows: the service and the step."""
    m = meta(observation)
    service = m.get("resourceAttributes.service.name") or "ai-service"
    scope = str(m.get("scope.name") or "")
    url = str(m.get("attributes.http.url") or m.get("attributes.url.full") or "")
    name = str(observation.get("name") or "")
    if service == "documents-api":
        return "api: chat request" if name == CHAT_ROOT else "api: ai-service call"
    if service == "llm-gateway":
        return "gateway: " + (
            "request" if name.startswith("Received") else "model call"
        )
    if observation.get("type") == "GENERATION":
        return "gemini: generation"
    if scope.endswith("fastapi"):
        return "ai-service: request"
    if scope.endswith("httpx"):
        if ":6333" in url or "qdrant" in url:
            return "qdrant"
        if "tei" in url or ":8081" in url or "/embed" in url:
            return "tei"
        return "http: " + url.split("/")[2] if url.count("/") > 2 else "http"
    if "botocore" in scope:
        return "dynamodb" if "dynamodb" in scope else "aws"
    if name in NODES or meta(observation).get("langgraph_node") == name:
        return "graph: node"
    return "graph: other"


def read(trace_ids: list[str], since: datetime) -> dict[str, list[dict[str, Any]]]:
    """The traces' observations, once two reads a little apart agree (Langfuse lags)."""
    tracing = setup_tracing(CoreSettings(), service_spans=False)
    if tracing.client is None:
        raise SystemExit("needs Langfuse: set the LANGFUSE_* variables")
    wanted, previous = set(trace_ids), -1
    found: dict[str, list[dict[str, Any]]] = {}
    for _ in range(20):
        found = {t: [] for t in wanted}
        for o in tracing.observations(since, fields="core,basic,metadata,model"):
            if o.get("trace_id") in wanted:
                found[o["trace_id"]].append(o)
        total = sum(len(v) for v in found.values())
        if total and total == previous:
            break
        previous = total
        time.sleep(20)
    return found


def summary(trace_id: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    parts = Counter(part(o) for o in observations)
    needed = {
        "api: chat request",
        "api: ai-service call",
        "ai-service: request",
        "graph: node",
        "qdrant",
        "gateway: request",
        "gateway: model call",
        "gemini: generation",
    }
    if any(o.get("name") == "hybrid_search" for o in observations):
        needed.add("tei")  # a question answered from extracted fields embeds nothing
    missing = sorted(n for n in needed if not parts.get(n))
    services = Counter(
        meta(o).get("resourceAttributes.service.name") or "ai-service"
        for o in observations
    )
    return {
        "trace_id": trace_id,
        "spans": len(observations),
        "by_service": dict(services),
        "by_part": dict(sorted(parts.items())),
        "missing": missing,
        "passed": not missing,
    }


def traces_rooted_at(name: str, since: datetime) -> list[str]:
    tracing = setup_tracing(CoreSettings(), service_spans=False)
    if tracing.client is None:
        raise SystemExit("needs Langfuse: set the LANGFUSE_* variables")
    return sorted(
        {
            str(o["trace_id"])
            for o in tracing.observations(since, fields="core,basic")
            if o.get("name") == name and not o.get("parent_observation_id")
        }
    )


def upload_summary(trace_id: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    parts = Counter(
        "generation" if o.get("type") == "GENERATION" else part(o) for o in observations
    )
    return {"trace_id": trace_id, "spans": len(observations), "by_part": dict(parts)}


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("trace")
    one.add_argument("ids", nargs="+")
    one.add_argument("--since", default="2026-10-23T00:00:00Z")
    many = commands.add_parser("chats")
    many.add_argument("--since", required=True)
    uploads = commands.add_parser("uploads")
    uploads.add_argument("--since", required=True)
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))

    if args.command == "uploads":
        found = read(traces_rooted_at("ingest", since), since)
        rows = [upload_summary(t, o) for t, o in found.items()]
        for row in rows:
            print(f"{row['trace_id']}: {row['spans']} spans {row['by_part']}")
        if rows:
            print(
                f"{len(rows)} uploads, {sum(r['spans'] for r in rows) / len(rows):.0f} spans each"
            )
        return 0 if rows else 1
    ids = args.ids if args.command == "trace" else traces_rooted_at(CHAT_ROOT, since)
    if not ids:
        print("no chat traces since", since.isoformat())
        return 1
    traces = [summary(t, o) for t, o in read(ids, since).items()]
    for t in traces:
        print(f"{t['trace_id']}: {t['spans']} spans {t['by_service']}")
        print(
            f"  {t['by_part']}" + (f"  MISSING {t['missing']}" if t["missing"] else "")
        )
    per_question = sum(t["spans"] for t in traces) / len(traces)
    # A dev run through the services would cost about this many units a month's plan.
    dev_run = per_question * DEV_QUESTIONS
    print(
        f"{len(traces)} traces, {per_question:.0f} spans a question; a {DEV_QUESTIONS}-question"
        f" dev run with service spans would be {dev_run:,.0f} units,"
        f" {100 * dev_run / PLAN_UNITS:.0f}% of the plan's {PLAN_UNITS:,} a month"
    )
    passed = all(t["passed"] for t in traces)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"{stamp}-telemetry-traces.json"
    record = {
        "kind": "telemetry-traces",
        "created": stamp,
        "since": since.isoformat(),
        "spans_per_question": round(per_question, 1),
        "traces": traces,
        "passed": passed,
    }
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"{'PASS' if passed else 'FAIL'}; saved {path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
