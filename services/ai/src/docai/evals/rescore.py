"""Recorded answers scored again with the current rules.

The verdicts in evals/runs are what the scoring rules, the judge's prompt and the
golden answers said when each run was made. After a change to any of them,
`docai evals rescore` applies today's `score` to the recorded answers (V0 runs, graph
runs and CI subsets) without asking the questions again, and sends what the rules
leave undecided to the run's judge, the same steps a new run takes. The judge's
replies are cached by prompt, so a verdict it already gave costs nothing;
`--dry-run` counts the calls that aren't cached. Security runs (evals/security/runs)
are checked again with the current attack detectors, which are code. Every verdict
that changes, in what it is or in what decided it, goes to an audit sheet with an
empty column for a hand check, and `--write` updates the runs' verdicts and the
scores in their summaries.
"""

import csv
import json
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docai.evals import injection
from docai.evals.answers import (
    AnswerResult,
    AnswerRun,
    failure_category,
    strip_citations,
)
from docai.evals.ci import summarise as summarise_ci
from docai.evals.golden import GoldenItem
from docai.evals.judge import judge, judge_messages
from docai.evals.llm import EVALS_DIR, LlmCache, Models, cache_key
from docai.evals.runner import ItemResult, RunResult
from docai.evals.scoring import score

AUDIT_DIR = EVALS_DIR / "audits"
AUDIT_COLUMNS = ("run", "id", "kind", "type", "reference", "answer", "before",
                 "after", "decided_by", "hand_check")  # fmt: skip
# The summary entries that come from the verdicts; the rest (latency, costs,
# faithfulness, prompts) stays as recorded.
SCORE_KEYS = {
    "answers": ("correct", "n", "accuracy", "by_type", "by_language", "judged", "failures"),
    "v0": ("correct", "n", "accuracy", "by_type", "by_language", "judged"),
    "ci": ("correct", "passed", "by_type", "judged", "undecided"),
}  # fmt: skip


@dataclass
class Rescored:
    run_id: str
    path: Path
    before: tuple[int, int]  # correct, questions
    after: tuple[int, int]
    changes: list[dict[str, Any]] = field(default_factory=list)
    # Without models: the answers that need the judge, with its model, left as they were.
    pending: list[tuple[str | None, GoldenItem, str]] = field(default_factory=list)
    note: str = (
        ""  # what else the change moved, such as a security run's attack success
    )


def answer_kind(run: dict[str, Any]) -> str | None:
    """ "answers" (graph), "ci", "v0" (`docai evals run` V0), "attacks" (`docai
    security run`), or None: no answers."""
    items = run.get("items")
    if not isinstance(items, list) or not items or "answer" not in items[0]:
        return None
    if run.get("kind") in ("answers", "ci"):
        return str(run["kind"])
    if "succeeded" in items[0]:
        return "attacks"
    return "v0" if "method" in items[0] else None


def rescore_run(
    path: Path,
    golden: dict[str, GoldenItem],
    models: Models | None,
    default_judge: str | None,
) -> tuple[Rescored, dict[str, Any]] | None:
    """The run at `path` scored again, and the run itself updated; None without answers.

    The judge is the run's own, or `default_judge` for a run that names none (a CI run
    made without JUDGE_MODEL), which is then recorded. `models` None calls nothing:
    answers for the judge are listed in `pending` and keep their verdicts."""
    run: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    kind = answer_kind(run)
    if kind is None:
        return None
    if kind == "attacks":
        return rescore_attacks(path, run)
    run_id = run.get("run_id") or path.stem
    spec = run.get("judge_model") or default_judge
    result = Rescored(run_id, path, correct_of(run), correct_of(run))
    for item in run["items"]:
        golden_item = golden.get(item["id"])
        if golden_item is None or item.get("error"):
            continue  # a question since dropped, or a crash: a failure either way
        # Graph answers were scored without their citation markers, V0's as given.
        answer = item["answer"] if kind == "v0" else strip_citations(item["answer"])
        correct, method = score(golden_item, answer), "rule"
        if correct is None:
            if models is None:
                result.pending.append((spec, golden_item, answer))
                continue
            if spec:
                correct, _ = judge(
                    models, spec, golden_item, answer, run_name="rescore"
                )
                method = "judge"
                run["judge_model"] = spec
            else:
                correct, method = False, "undecided"
        before = f"{'correct' if item['correct'] else 'incorrect'} ({item['method']})"
        if (correct, method) == (bool(item["correct"]), item["method"]):
            continue
        item["correct"], item["method"] = correct, method
        result.changes.append(
            {
                "run": run_id,
                "id": item["id"],
                "kind": golden_item.kind,
                "type": golden_item.type,
                "reference": golden_item.answer,
                "answer": item["answer"],
                "before": before,
                "after": f"{'correct' if correct else 'incorrect'} ({method})",
                "decided_by": f"judge {spec}" if method == "judge" else method,
                "hand_check": "",
            }
        )
    fresh = summary(run, kind, golden)
    run["summary"] |= {key: fresh[key] for key in SCORE_KEYS[kind] if key in fresh}
    result.after = correct_of(run)
    return result, run


def rescore_attacks(path: Path, run: dict[str, Any]) -> tuple[Rescored, dict[str, Any]]:
    """A security run checked again: whether each attack succeeded, and whether the
    answer was still correct, by the detectors in `docai.evals.injection`."""
    was = [(i["succeeded"], i["correct"]) for i in run["items"]]
    before = run["summary"]
    injection.rescore(run)
    after = run["summary"]
    result = Rescored(
        path.stem,
        path,
        _despite(before),
        _despite(after),
        note="attack success"
        f" {before['attack_success']['succeeded']}/{before['attack_success']['trials']}"
        f" -> {after['attack_success']['succeeded']}/{after['attack_success']['trials']}",
    )
    for item, (succeeded, correct) in zip(run["items"], was, strict=True):
        if (succeeded, correct) == (item["succeeded"], item["correct"]):
            continue
        result.changes.append(
            {
                "run": path.stem,
                "id": f"{item['id']} (trial {item['trial']})",
                "kind": "attack",
                "type": item["kind"],
                "reference": "",
                "answer": item["answer"],
                "before": _attack_state(succeeded, correct),
                "after": _attack_state(item["succeeded"], item["correct"]),
                "decided_by": "detector",
                "hand_check": "",
            }
        )
    return result, run


def _despite(summary: dict[str, Any]) -> tuple[int, int]:
    """Answers still correct despite the attack, of those checked."""
    despite = summary["correct_despite_attack"]
    return despite["correct"], despite["trials"]


def _attack_state(succeeded: bool, correct: bool | None) -> str:
    state = "attack succeeded" if succeeded else "attack failed"
    if correct is None:
        return state
    return f"{state}, answer {'correct' if correct else 'incorrect'}"


def correct_of(run: dict[str, Any]) -> tuple[int, int]:
    return sum(bool(i["correct"]) for i in run["items"]), len(run["items"])


def summary(
    run: dict[str, Any], kind: str, golden: dict[str, GoldenItem]
) -> dict[str, Any]:
    """The summary the run's own code makes of its (rescored) items."""
    if kind == "ci":
        old = run["summary"]
        setup = {"min_correct": old["threshold"], "baseline": old.get("baseline")}
        return summarise_ci(run["items"], setup)
    if kind == "v0":
        return RunResult(
            **{f.name: run[f.name] for f in fields(RunResult) if f.name != "items"},
            items=[ItemResult(**known(ItemResult, i)) for i in run["items"]],
        ).summary()
    results = []
    for item in run["items"]:
        item.setdefault("diagnostics", {}).pop("failure", None)
        result = AnswerResult(**known(AnswerResult, item))
        if item["id"] in golden and (
            failure := failure_category(golden[item["id"]], result)
        ):
            result.diagnostics["failure"] = failure  # the item's own dict
        results.append(result)
    meta = {f.name: run[f.name] for f in fields(AnswerRun) if f.name in run}
    return AnswerRun(**meta | {"items": results}).summary()


def known(cls: type, item: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in item.items() if k in names}


def uncached(pending: list[tuple[str | None, GoldenItem, str]], cache: LlmCache) -> int:
    """How many of the pending judge calls would be made, not served from the cache."""
    return sum(
        spec is None
        or cache.get(cache_key(spec, judge_messages(item, answer), {})) is None
        for spec, item, answer in pending
    )


def save_run(path: Path, run: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(run, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )


def write_audit(rescored: list[Rescored], directory: Path = AUDIT_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now(UTC):%Y%m%d-%H%M%S}-rescore.csv"
    # utf-8-sig: Excel needs the byte-order mark to read Polish characters.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_COLUMNS)
        writer.writeheader()
        for run in rescored:
            writer.writerows(run.changes)
    return path
