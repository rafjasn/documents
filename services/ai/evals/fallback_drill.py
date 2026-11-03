"""Stage 10b gate: a failure of the primary model is answered by the fallback, and the
trace and the recorded cost show which model answered.

Asks one question through the API as account B (evals/budget_gate.py) with the gateway
on its drill config, whose primary model always answers as rate-limited. Then reads the
question's trace back from Langfuse and checks that every generation names the fallback
model, and that the cost the chat reported is priced as the fallback.

From services/ai, with the services pointed at the gateway and the gateway on the drill
config (LLM_GATEWAY_CONFIG=drill-primary-down.yaml docker compose --profile ops up -d
llm-gateway), and the LANGFUSE_* variables set:
    uv run --env-file ../../.env python evals/fallback_drill.py
"""

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from budget_gate import session
from chat_e2e import ask

from docai.core.prices import cost
from docai.core.settings import CoreSettings
from docai.core.tracing import setup_tracing

PRIMARY, FALLBACK = "gemini-3.1-flash-lite", "gemini-3.5-flash-lite"
QUESTION = "What is the gross total of invoice BG?"
RUNS = Path(__file__).parent / "runs"


def generations(trace_id: str, since: datetime, calls: int) -> list[dict[str, Any]]:
    """The trace's generations, once Langfuse has all `calls` (ingestion lags)."""
    tracing = setup_tracing(CoreSettings())
    found: list[dict[str, Any]] = []
    for _ in range(12):
        time.sleep(10)
        found = [
            o
            for o in tracing.observations(since, fields="core,basic,usage,model")
            if o.get("trace_id") == trace_id and o.get("type") == "GENERATION"
        ]
        if len(found) >= calls and all(o.get("end_time") for o in found):
            break
    return found


def main() -> int:
    started = datetime.now(UTC) - timedelta(seconds=5)
    api, _ = session("B")
    conversation = api.post("/ai/conversations", json={}).json()["id"]
    events = ask(api, conversation, QUESTION)
    [done] = [e for e in events if e["type"] == "done"]
    usage, trace_id = done["usage"], done["traceId"]
    answer = "".join(e["text"] for e in events if e["type"] == "token")
    print(f"answer: {answer[:100]!r}")
    print(f"usage: {usage}, trace {trace_id}")

    found = generations(trace_id, started, usage["llmCalls"])
    models = [str(o.get("model")) for o in found]
    for o, model in zip(found, models):
        print(f"  {o.get('name')}: {model}")
    tokens = (usage["inputTokens"], usage["outputTokens"])
    priced = {m: float(cost(m, *tokens) or 0) for m in (PRIMARY, FALLBACK)}
    reported = float(usage["costUsd"])
    print(f"cost reported ${reported:.6f}; at {FALLBACK} ${priced[FALLBACK]:.6f}, "
          f"at {PRIMARY} ${priced[PRIMARY]:.6f}")  # fmt: skip
    passed = (
        len(found) == usage["llmCalls"]
        and all(FALLBACK in m for m in models)
        # Cached input tokens make the reported cost a little lower than this estimate.
        and abs(reported - priced[FALLBACK]) < 0.1 * priced[FALLBACK]
        and abs(reported - priced[FALLBACK]) < abs(reported - priced[PRIMARY])
    )
    RUNS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    path = RUNS / f"{stamp}-fallback-drill.json"
    record = {
        "kind": "fallback-drill",
        "created": stamp,
        "trace_id": trace_id,
        "usage": usage,
        "generations": [
            {"name": o.get("name"), "model": m} for o, m in zip(found, models)
        ],
        "priced": priced,
        "passed": passed,
    }
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"{'PASS' if passed else 'FAIL'}; saved {path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
