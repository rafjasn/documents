"""Stage 10c gate: stopping TEI fires the error-rate alert, which clears after TEI
restarts.

Stops the TEI container, asks questions through the API as account B
(evals/budget_gate.py) until Grafana's `error-rate` rule fires, starts TEI again, and
keeps asking until the rule is back to normal. Each question with TEI down costs only its
routing call. Grafana is read anonymously, as a viewer.

From services/ai, with the stack and the ops profile up:
    set -a; source ../../.env; set +a
    uv run python evals/alert_drill.py
"""

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from budget_gate import outcome, session
from chat_e2e import ask

GRAFANA = "http://localhost:3030"
RULE = "error-rate"
REPO = Path(__file__).resolve().parents[3]
RUNS = Path(__file__).parent / "runs"
QUESTION = "What does invoice BG say about its total and who issued it?"
LIMIT_SECONDS = 15 * 60


def rule_state() -> str:
    """The rule's state in Grafana: inactive, pending or firing."""
    rules = httpx.get(
        f"{GRAFANA}/api/prometheus/grafana/api/v1/rules", timeout=10
    ).json()
    for group in rules["data"]["groups"]:
        for rule in group["rules"]:
            if (rule.get("labels") or {}).get("__alert_rule_uid__") == RULE or rule.get(
                "name", ""
            ).startswith("Chat and upload error rate"):
                return str(rule.get("state", "unknown"))
    return "missing"


def compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", *args], cwd=REPO, check=True, capture_output=True
    )


def wait_for(state: str, log: list[dict[str, Any]]) -> float:
    """Ask a question every 20 s until the rule reaches `state`; seconds taken. Each
    question logs in again: Keycloak's access tokens last 5 minutes."""
    started = time.monotonic()
    while time.monotonic() - started < LIMIT_SECONDS:
        api, _ = session("B")
        conversation = api.post("/ai/conversations", json={}).json()["id"]
        result = outcome(ask(api, conversation, QUESTION))
        current = rule_state()
        log.append(
            {"at": datetime.now(UTC).isoformat(), "asked": result[:60], "rule": current}
        )
        print(f"  {result[:60]!r} -> rule {current}", flush=True)
        if current == state:
            return round(time.monotonic() - started)
        time.sleep(20)
    raise TimeoutError(f"the rule didn't reach {state} in {LIMIT_SECONDS} s")


def main() -> int:
    print(f"rule {RULE} is {rule_state()}; stopping TEI")
    log: list[dict[str, Any]] = []
    compose("stop", "tei-embed")
    try:
        fired = wait_for("firing", log)
        print(f"fired after {fired}s; starting TEI")
    finally:
        compose("start", "tei-embed")
    time.sleep(30)  # TEI loads its model
    cleared = wait_for("inactive", log)
    print(f"cleared {cleared}s after TEI restarted")
    RUNS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    path = RUNS / f"{stamp}-alert-drill.json"
    record = {
        "kind": "alert-drill",
        "rule": RULE,
        "fired_after_seconds": fired,
        "cleared_after_seconds": cleared,
        "log": log,
        "passed": True,
    }
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"PASS; saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
