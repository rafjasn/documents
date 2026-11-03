from pathlib import Path
from typing import Any

import pytest

from docai.evals import report, runner, variants
from docai.evals.golden import AnswerKind, GoldenItem, QuestionType, Relevant
from docai.evals.judge import judge_messages, verdict
from docai.evals.llm import LlmCache, Messages, Reply, cache_key
from docai.evals.runner import estimate, run, subset


def golden(
    id_: str,
    type_: QuestionType,
    kind: AnswerKind,
    value: str,
    docs: tuple[str, ...] = ("doc-1",),
) -> GoldenItem:
    return GoldenItem(
        id=id_,
        split="dev",
        type=type_,
        language="en",
        question=f"Question {id_}?",
        answer=value,
        kind=kind,
        value=value,
        accepted=(value,) if kind == "text" else (),
        relevant=tuple(Relevant(doc_id=d) for d in docs),
        source="test",
    )


class FakeModels:
    """Answers from a script; the judge always says CORRECT."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def is_cached(self, spec: str, messages: Messages, **params: Any) -> bool:
        return False

    def call(
        self,
        spec: str,
        messages: Messages,
        *,
        run_name: str,
        metadata: dict[str, str],
        **params: Any,
    ) -> Reply:
        self.calls.append(spec)
        text = "CORRECT" if spec == "judge" else self.answers[metadata["item_id"]]
        return Reply(
            text=text, input_tokens=1000, output_tokens=100, seconds=0.5, cached=False
        )


@pytest.fixture
def legacy_texts(monkeypatch: pytest.MonkeyPatch) -> None:
    variants.legacy_text.cache_clear()
    monkeypatch.setattr(variants, "legacy_text", lambda doc_id: "x" * 9000)


def test_v0_uses_the_first_document_cut_to_8000_characters(legacy_texts: None) -> None:
    messages = variants.TruncationBaseline().messages(
        golden("a", "cross_doc", "boolean", "yes", ("d1", "d2"))
    )
    assert messages[0][1].endswith("x" * 8000) and "x" * 8001 not in messages[0][1]
    assert messages[1] == ("human", "Question a?")
    empty = variants.TruncationBaseline().messages(
        golden("b", "unanswerable", "refusal", "refusal", ())
    )
    assert empty[0][1].endswith("content:\n\n")


def test_run_scores_with_rules_and_judges_the_rest(legacy_texts: None) -> None:
    items = [
        golden("n", "aggregate", "number", "42.00"),
        golden("t", "single_fact", "text", "Texas"),
        golden("r", "unanswerable", "refusal", "refusal"),
    ]
    models = FakeModels(
        {"n": "It was 42,00 PLN.", "t": "Delaware law applies.", "r": "The rate is 8%."}
    )
    result = run(
        models,
        variants.TruncationBaseline(),
        "dev",
        items,
        "google_genai:gemini-3.1-flash-lite",
        "judge",
    )

    methods = {r.id: (r.method, r.correct) for r in result.items}
    assert methods == {"n": ("rule", True), "t": ("judge", True), "r": ("judge", True)}
    assert models.calls.count("judge") == 2
    summary = result.summary()
    assert summary["accuracy"] == 1.0 and summary["judged"] == 2
    assert summary["cost_per_query_usd"] == pytest.approx(
        (0.25 * 1000 + 1.5 * 100) / 1_000_000
    )


def test_estimate_counts_uncached_calls(legacy_texts: None) -> None:
    items = [
        golden("a", "aggregate", "number", "1"),
        golden("b", "single_fact", "date", "2026-01-01"),
    ]
    plan = estimate(
        FakeModels({}),
        variants.TruncationBaseline(),
        items,
        "google_genai:gemini-3.1-flash-lite",
        "judge",
    )
    assert plan.uncached_answer_calls == 2 and plan.judge_calls_at_most == 2
    assert plan.estimated_cost_usd is not None and plan.estimated_cost_usd > 0


def test_subset_spreads_across_types() -> None:
    items = [
        golden(f"{t}-{n}", t, "number", "1")
        for t in ("table", "image", "aggregate")
        for n in range(3)
    ]
    assert {i.type for i in subset(items, 3)} == {"table", "image", "aggregate"}
    assert len(subset(items, None)) == 9


def test_cache_round_trip(tmp_path: Path) -> None:
    cache = LlmCache(tmp_path / "cache.sqlite")
    key = cache_key("m", [("human", "hi")], {"max_tokens": 5})
    assert key == cache_key("m", [("human", "hi")], {"max_tokens": 5})
    assert cache.get(key) is None
    cache.put(key, "m", Reply("hello", 3, 1, 0.2, cached=False))
    assert cache.get(key) == Reply("hello", 3, 1, 0.2, cached=True)


def test_judge_prompt_and_verdict() -> None:
    item = golden("x", "single_fact", "text", "British Columbia, Canada")
    item = item.model_copy(
        update={"accepted": ("British Columbia, Canada", "British Columbia")}
    )
    prompt = judge_messages(item, "BC law")[0][1]
    assert "also acceptable: British Columbia" in prompt and "BC law" in prompt
    assert (
        verdict("INCORRECT") is False
        and verdict("correct.") is True
        and verdict("unsure") is None
    )


def test_report_renders_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_texts: None
) -> None:
    items = [golden("n", "aggregate", "number", "42.00")]
    result = run(
        FakeModels({"n": "42"}),
        variants.TruncationBaseline(),
        "dev",
        items,
        "google_genai:gemini-3.1-flash-lite",
        "judge",
    )
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(report, "RESULTS_PATH", tmp_path / "RESULTS.md")
    result.save(record=True)
    text = report.write_report().read_text(encoding="utf-8")
    assert (
        "## Dev split" in text and "| V0 | gemini-3.1-flash-lite | 100% (1/1) |" in text
    )
    lines = text.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("| Variant"))
    # GitHub only renders a table whose header, divider and rows have equal cell counts.
    assert len({lines[header + k].count("|") for k in range(3)}) == 1


def gate(file: str, prompts: dict[str, Any], **extra: Any) -> dict[str, Any]:
    passed = extra.pop("passed", True)
    summary = {"questions": 30, "correct": 29 if passed else 19, "threshold": 26,
               "passed": passed, "prompts": prompts}  # fmt: skip
    return {"kind": "ci", "file": file, "summary": summary, **extra}


def test_prompt_gates_render_from_their_records() -> None:
    production = {"chat-answer": 1, "chat-plan": 1}
    candidate = {"label": "candidate", "production": production}
    runs = [
        gate("a-ci", candidate | {"versions": {"chat-answer": 2, "chat-plan": 1}}, passed=False),
        gate("b-ci", candidate | {"versions": {"chat-answer": 3, "chat-plan": 4}}),
        gate("c-ci", candidate | {"versions": {"chat-answer": 3, "chat-plan": 1}},
             promotion={"at": "2026-11-02T10:00:00+00:00", "production": {}}),
        gate("d-ci", {"label": "code", "versions": {}}),  # the repository's: no gate
    ]  # fmt: skip

    text = "\n".join(report.render_operations(runs))

    assert (
        "Prompt gate: `chat-answer` v2 as `candidate` on the CI subset (threshold 26/30) | 19/30, not promoted | [JSON](../services/ai/evals/runs/a-ci.json)"
        in text
    )
    assert "`chat-answer` v3, `chat-plan` v4 as `candidate`" in text
    assert (
        "29/30, passed, not promoted | [JSON](../services/ai/evals/runs/b-ci.json)"
        in text
    )
    assert "29/30, promoted | [JSON](../services/ai/evals/runs/c-ci.json)" in text
    assert "d-ci.json" not in text
