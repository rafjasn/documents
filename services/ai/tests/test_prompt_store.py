"""Prompts in Langfuse (Stage 10a): served by label with the code as the fallback, and
moved between versions only by push, promote and rollback. Evaluations pin versions,
and a promotion moves `production` to exactly the versions evaluated."""

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from docai import cli
from docai.agent import prompt_store as store
from docai.agent.graph import AgentContext, build_graph, initial_state
from docai.agent.models import Calls
from docai.agent.prompt_store import CODE, PromptStore, Rendered, render
from docai.agent.prompts import Route
from tests.service_support import FakeCalls, FakeRetriever


@dataclass
class FakePrompt:
    name: str
    version: int
    prompt: str
    labels: list[str] = field(default_factory=list)
    is_fallback: bool = False


class NotFound(Exception):
    status_code = 404


class FakeLangfuse:
    """Prompt versions and labels as Langfuse keeps them: each label on one version."""

    def __init__(self) -> None:
        self.prompts: dict[str, list[FakePrompt]] = {}
        self.fetches = 0
        self.down = False

    def get_prompt(
        self,
        name: str,
        *,
        label: str | None = None,
        version: int | None = None,
        fallback: str | None = None,
        **_: Any,
    ) -> FakePrompt:
        self.fetches += 1
        if self.down:
            raise ConnectionError("Langfuse is unreachable")
        versions = self.prompts.get(name, [])
        if version is not None:
            found = versions[version - 1] if 0 < version <= len(versions) else None
        elif label == "latest":
            found = versions[-1] if versions else None
        else:
            found = next((v for v in versions if label in v.labels), None)
        if found is None:
            if fallback is not None:
                return FakePrompt(name, 0, fallback, is_fallback=True)
            raise NotFound(name)
        return found

    def _take(self, name: str, labels: list[str]) -> None:
        for version in self.prompts.get(name, []):
            version.labels = [label for label in version.labels if label not in labels]

    def create_prompt(
        self, *, name: str, prompt: str, labels: list[str], **_: Any
    ) -> FakePrompt:
        self._take(name, labels)
        versions = self.prompts.setdefault(name, [])
        created = FakePrompt(name, len(versions) + 1, prompt, list(labels))
        versions.append(created)
        return created

    def update_prompt(self, *, name: str, version: int, new_labels: list[str]) -> None:
        self._take(name, new_labels)
        self.prompts[name][version - 1].labels = list(new_labels)

    def labelled(self, name: str, label: str) -> int | None:
        found = next((v for v in self.prompts.get(name, []) if label in v.labels), None)
        return found.version if found else None


VARIABLES = {"chat-route": {"today": "2026-10-12"}, "chat-plan": {"today": "2026-10-12"},
             "chat-rewrite": {"today": "2026-10-12"}, "chat-answer": {"language": "pl"}}  # fmt: skip


@pytest.mark.parametrize("name", sorted(CODE))
def test_every_prompt_renders_with_the_variables_the_graph_passes(name: str) -> None:
    rendered = PromptStore().render(name, **VARIABLES.get(name, {}))
    assert "{{" not in rendered.text and rendered.version is None
    assert rendered.label == f"{name}@code"


def test_a_missing_variable_is_an_error() -> None:
    with pytest.raises(KeyError):
        render("Today is {{today}}.")


def test_the_label_serves_its_langfuse_version() -> None:
    langfuse = FakeLangfuse()
    langfuse.create_prompt(
        name="chat-route",
        prompt="New routing. Today is {{today}}.",
        labels=["production"],
    )

    rendered = PromptStore(langfuse, "production").render(
        "chat-route", today="2026-10-12"
    )

    assert rendered.text == "New routing. Today is 2026-10-12."
    assert rendered.version == 1 and rendered.source is not None
    assert rendered.label == "chat-route@v1"


def test_without_the_label_or_langfuse_the_code_serves_and_isnt_asked_again() -> None:
    langfuse = FakeLangfuse()
    prompts = PromptStore(langfuse, "production")

    first = prompts.render("chat-grade")
    fetches = langfuse.fetches
    prompts.render("chat-grade")

    assert first.text == CODE["chat-grade"] and first.version is None
    assert langfuse.fetches == fetches  # the miss is remembered for the cache period

    langfuse.down = True
    assert (
        PromptStore(langfuse, "production").render("chat-grade").text
        == CODE["chat-grade"]
    )
    assert PromptStore(None, "production").render("chat-grade").version is None


def evaluated(langfuse: FakeLangfuse) -> dict[str, Any]:
    """What a CI evaluation of `candidate` records about the prompts it ran on."""
    return PromptStore.pinned(langfuse, "candidate").describe()


def test_push_promote_rollback_and_status() -> None:
    langfuse = FakeLangfuse()

    assert all("created" in line for line in store.push(langfuse, "test"))
    assert store.status(langfuse)[1] is False  # nothing in production yet
    store.promote(langfuse, evaluated(langfuse))
    assert store.status(langfuse)[1] is True
    assert store.push(langfuse, "test")[0].endswith("already the candidate")

    changed = dict(CODE, **{"chat-answer": CODE["chat-answer"] + "\n- Be brief."})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "CODE", changed)
        lines = store.push(langfuse, "test")
        assert [line for line in lines if "created" in line] == [
            "chat-answer: v2 created, labelled candidate"
        ]
        assert store.status(langfuse)[1] is False  # production still has v1
        store.promote(langfuse, evaluated(langfuse))
        assert langfuse.labelled("chat-answer", "production") == 2
        assert langfuse.labelled("chat-answer", "previous") == 1
        assert langfuse.labelled("chat-route", "production") == 1  # unchanged

    store.rollback(langfuse)
    assert langfuse.labelled("chat-answer", "production") == 1
    assert store.status(langfuse)[1] is True


def test_the_graph_sends_the_served_versions_and_names_them() -> None:
    langfuse = FakeLangfuse()
    langfuse.create_prompt(
        name="chat-smalltalk", prompt="Be cheerful.", labels=["production"]
    )
    calls = FakeCalls(
        {
            "route_intent": [Route(intent="smalltalk", language="en", query="hi")],
            "smalltalk": ["Hello!"],
        }
    )
    context = AgentContext(
        user_id="user-a",
        calls=calls,
        retriever=FakeRetriever(),
        load_records=list,
        today=date(2026, 10, 12),
        prompts=PromptStore(langfuse, "production"),
    )

    build_graph().invoke(initial_state("hi"), context=context)

    assert calls.prompts("smalltalk")[0].startswith("Be cheerful.")
    used = [p for p in calls.used_prompts if isinstance(p, Rendered)]
    assert [p.label for p in used] == ["chat-route@code", "chat-smalltalk@v1"]


class FakeModels:
    def model(self, role: str, max_tokens: int) -> GenericFakeChatModel:
        return GenericFakeChatModel(
            messages=iter([AIMessage(content="Total 22 212,00 PLN")])
        )


class PromptRuns(BaseCallbackHandler):
    def __init__(self) -> None:
        self.prompt_runs: list[UUID] = []

    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        if metadata and "langfuse_prompt" in metadata:
            self.prompt_runs.append(run_id)


class OneNode(TypedDict):
    answer: str


def test_a_linked_call_carries_its_prompt_and_still_streams_tokens() -> None:
    source = FakePrompt("chat-answer", 3, "text")
    prompt = Rendered("chat-answer", "Answer.", 3, source)

    def generate(state: OneNode) -> dict[str, str]:
        calls = Calls(FakeModels())  # type: ignore[arg-type]
        answer = calls.text(
            [SystemMessage(prompt.text), HumanMessage("Total?")],
            run_name="generate",
            prompt=prompt,
        )
        return {"answer": answer}

    graph = StateGraph(OneNode)
    graph.add_node("generate", generate)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", END)
    runs = PromptRuns()
    tokens = [
        getattr(chunk, "content", "")
        for chunk, _ in graph.compile().stream(
            {"answer": ""}, {"callbacks": [runs]}, stream_mode="messages"
        )
    ]

    assert "".join(str(t) for t in tokens) == "Total 22 212,00 PLN"
    assert len(runs.prompt_runs) == 1


def test_a_revert_reuses_the_version_with_the_same_text() -> None:
    langfuse = FakeLangfuse()
    store.push(langfuse, "test")
    worse = dict(CODE, **{"chat-answer": "Round every amount."})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "CODE", worse)
        store.push(langfuse, "test")
    assert langfuse.labelled("chat-answer", "candidate") == 2

    lines = store.push(langfuse, "test")

    assert "chat-answer: v1 matches the code, labelled candidate" in lines
    assert len(langfuse.prompts["chat-answer"]) == 2


def test_promotion_moves_production_to_the_evaluated_versions_not_the_label() -> None:
    """The review's sequence: evaluate candidate v1, someone moves candidate to v2,
    promote. Production must get v1, which passed, not the unevaluated v2."""
    langfuse = FakeLangfuse()
    store.push(langfuse, "test")
    passed = evaluated(langfuse)  # the evaluation ran on v1 of every prompt

    worse = dict(CODE, **{"chat-answer": "Round every amount."})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "CODE", worse)
        store.push(langfuse, "test")
    assert langfuse.labelled("chat-answer", "candidate") == 2

    lines = store.promote(langfuse, passed)

    assert langfuse.labelled("chat-answer", "production") == 1
    assert langfuse.labelled("chat-answer", "candidate") == 2  # left for its own run
    assert (
        "chat-answer: production none -> v1 (candidate is now v2, not evaluated)"
        in lines
    )


def test_a_pinned_store_serves_its_versions_while_the_label_moves() -> None:
    langfuse = FakeLangfuse()
    store.push(langfuse, "test")
    pinned = PromptStore.pinned(langfuse, "candidate")
    fetches = langfuse.fetches

    langfuse.create_prompt(name="chat-answer", prompt="Round.", labels=["candidate"])

    assert pinned.render("chat-answer", language="en").version == 1
    assert pinned.describe()["versions"]["chat-answer"] == 1
    assert langfuse.fetches == fetches  # nothing asked again during the evaluation
    assert pinned.describe()["sha256"]["chat-answer"] == store.text_hash(
        CODE["chat-answer"]
    )


def test_evaluating_a_label_fails_rather_than_falling_back_to_the_code() -> None:
    langfuse = FakeLangfuse()
    store.push(langfuse, "test")
    langfuse.update_prompt(name="chat-grade", version=1, new_labels=[])

    with pytest.raises(LookupError, match="no candidate version of chat-grade"):
        PromptStore.pinned(langfuse, "candidate")

    langfuse.down = True
    with pytest.raises(ConnectionError):
        PromptStore.pinned(langfuse, "candidate")


def test_promotion_refuses_versions_it_cannot_match_to_the_evaluation() -> None:
    langfuse = FakeLangfuse()
    store.push(langfuse, "test")
    passed = evaluated(langfuse)

    langfuse.prompts["chat-route"][0].prompt = "Edited since."  # not what was evaluated
    with pytest.raises(ValueError, match="chat-route v1 isn't the text evaluated"):
        store.promote(langfuse, passed)
    with pytest.raises(ValueError, match="pinned Langfuse versions"):
        store.promote(langfuse, PromptStore().describe())  # the code's prompts

    assert all(langfuse.labelled(name, "production") is None for name in CODE)


def test_only_a_passed_ci_evaluation_can_be_promoted() -> None:
    prompts = {"label": "candidate", "versions": {}, "sha256": {}}
    record = {"kind": "ci", "summary": {"passed": True, "prompts": prompts}}

    assert store.evaluated_prompts(record) == prompts
    with pytest.raises(ValueError):
        store.evaluated_prompts(
            record | {"summary": {"passed": False, "prompts": prompts}}
        )
    with pytest.raises(ValueError):
        store.evaluated_prompts(record | {"kind": "answers"})


class Tracing:
    def __init__(self, client: FakeLangfuse) -> None:
        self.client = client

    def flush(self) -> None:
        pass


def test_the_promote_command_records_its_outcome_in_the_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    langfuse = FakeLangfuse()
    store.push(langfuse, "test")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "setup_tracing", lambda settings: Tracing(langfuse))
    summary: dict[str, Any] = {"passed": True, "prompts": evaluated(langfuse)}
    passed, failed = tmp_path / "passed-ci.json", tmp_path / "failed-ci.json"
    passed.write_text(json.dumps({"kind": "ci", "summary": summary}))
    failed.write_text(
        json.dumps({"kind": "ci", "summary": summary | {"passed": False}})
    )

    assert cli.main(["prompts", "promote", "--evaluation", str(failed)]) == 1
    assert "promotion" not in json.loads(failed.read_text())
    assert cli.main(["prompts", "promote", "--evaluation", str(passed)]) == 0

    promotion = json.loads(passed.read_text())["promotion"]
    assert promotion["production"] == summary["prompts"]["versions"]
    assert all(langfuse.labelled(name, "production") == 1 for name in CODE)
