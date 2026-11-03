"""Stage 10a gate check: traces carry no synthetic tax ID or account number.

Reads back, through Langfuse's API, every observation recorded since `--since` (the
walkthrough and an evaluation run with tracing on, made just before), and searches
their inputs, outputs and metadata for each tax ID and bank account number of the five
synthetic worlds, as written in the documents and without separators. Also counts the
masked markers, and the model calls that name the prompt version they used. The result
is saved in evals/runs/ for docs/RESULTS.md.

From services/ai, with the LANGFUSE_* variables set:
    uv run --env-file ../../.env python evals/masking_gate.py --since 2026-10-22T10:00:00Z
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from docai.core.masking import MARKER
from docai.core.settings import CoreSettings
from docai.core.tracing import setup_tracing
from docai.corpus.synthetic.world import WORLD_COUNT, build_world

RUNS = Path(__file__).parent / "runs"


def identifiers() -> dict[str, str]:
    """Each identifier's written forms -> its kind."""
    found: dict[str, str] = {}
    for index in range(WORLD_COUNT):
        world = build_world(index)
        for party in (world.buyer, *world.vendors):
            for value, kind in (
                (party.tax_id, "tax ID"),
                (party.bank_account, "account"),
            ):
                found[value] = kind
                found[re.sub(r"[\s-]", "", value)] = kind
    return found


def texts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in texts(v)]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in texts(v)]
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--since", required=True, help="ISO time, e.g. 2026-10-22T10:00:00Z"
    )
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))

    tracing = setup_tracing(CoreSettings())
    if tracing.client is None:
        print("needs Langfuse: set the LANGFUSE_* variables")
        return 2
    wanted = identifiers()
    leaks: Counter[str] = Counter()
    markers: Counter[str] = Counter()
    observations = generations = with_prompt = 0
    for observation in tracing.observations(since):
        observations += 1
        if observation.get("type") == "GENERATION":
            generations += 1
            if observation.get("prompt_name"):
                with_prompt += 1
        content = "\n".join(
            text
            for key in ("input", "output", "metadata")
            for raw in [observation.get(key)]
            for text in texts(
                json.loads(raw) if isinstance(raw, str) and raw[:1] in "[{" else raw
            )
        )
        for marker in MARKER.findall(content):
            markers[marker[1:].split(":")[0]] += 1
        for value, kind in wanted.items():
            if value in content:
                leaks[f"{kind} {value}"] += 1

    print(
        f"{observations} observations since {since.isoformat()}, {generations} generations"
    )
    print(f"{with_prompt} generations name their prompt version")
    print(f"masked markers: {dict(markers)}")
    print(
        f"synthetic identifiers searched: {len(wanted)} forms of {len(set(wanted.values()))} kinds"
    )
    passed = bool(observations) and not leaks
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"{stamp}-masking-gate.json"
    record = {
        "kind": "masking-gate",
        "created": stamp,
        "since": since.isoformat(),
        "observations": observations,
        "generations": generations,
        "with_prompt_version": with_prompt,
        "identifier_forms": len(wanted),
        "masked": dict(markers),
        "leaks": dict(leaks),
        "passed": passed,
    }
    path.write_text(json.dumps(record, indent=2) + "\n")
    if leaks:
        print(f"LEAKED: {dict(leaks)}")
    else:
        print("no synthetic tax ID or account number in any trace")
    print(f"{'PASS' if passed else 'FAIL'}; saved {path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
