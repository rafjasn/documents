"""The chat graph.

    route_intent ─► smalltalk ───────────────────────────────────────────────► END
                ├─► retrieve:  hybrid_search → grade_context ─(short, < 2 searches)─►
                │              rewrite_query → hybrid_search … → generate
                ├─► aggregate: build_filter → metadata_tool → generate
                └─► mixed:     build_filter → metadata_tool → hybrid_search → …
    generate → check_grounding ─(unsupported, first time)─► generate ─► check_grounding
                               └─(unsupported again)─► the answer, with a stated caveat

There is no rerank step: the Stage 03 evaluation found no reranker worth its cost on
contextualised chunks. Loops are bounded (2 searches, 1 regeneration, 10 model calls
per request), tools are read-only, the user comes from the runtime context (the JWT),
never from model output, and document text reaches models only inside tags, as data.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from docai.agent import prompts
from docai.agent.metadata import AggregateResult, Record, run_plan
from docai.agent.models import Calls
from docai.agent.prompt_store import PromptStore, code_prompt
from docai.agent.sources import (
    build_sources,
    citations_for,
    render_sources,
    without_unsourced_urls,
)
from docai.agent.state import ChatState, GraphOptions
from docai.core.numbers import Quantity, numbers_in, quantities
from docai.core.retrieval import DEFAULT_RETRIEVAL, Hit, Retriever
from docai.core.telemetry import timed_node
from docai.core.vectorstore import SearchFilters

HISTORY_TURNS = 6
HISTORY_CHARS = 600  # per turn
ANSWER_NODES = ("generate", "smalltalk")  # their tokens stream to the user
LABELS = {
    "route_intent": "Understanding the question",
    "smalltalk": "Replying",
    "build_filter": "Planning the calculation",
    "metadata_tool": "Calculating from the documents' fields",
    "hybrid_search": "Searching your documents",
    "grade_context": "Checking what was found",
    "rewrite_query": "Searching again",
    "generate": "Writing the answer",
    "check_grounding": "Checking the answer against the sources",
}


@dataclass
class AgentContext:
    """Per-request dependencies. `user_id` comes from the validated token only."""

    user_id: str
    calls: Calls
    retriever: Retriever
    load_records: Callable[[], list[Record]]
    filters: SearchFilters | None = None
    today: date = field(default_factory=date.today)
    prompts: PromptStore = field(default_factory=PromptStore)
    _records: list[Record] | None = None

    def records(self) -> list[Record]:
        if self._records is None:
            self._records = self.load_records()
        return self._records


def step(runtime: Runtime[AgentContext], node: str) -> None:
    runtime.stream_writer({"type": "step", "node": node, "label": LABELS[node]})


def conversation(state: ChatState) -> str:
    turns = state.get("history", [])[-HISTORY_TURNS:]
    if not turns:
        return ""
    lines = [f"{role}: {content[:HISTORY_CHARS]}" for role, content in turns]
    return "<conversation>\n" + "\n".join(lines) + "\n</conversation>\n\n"


def ask(state: ChatState) -> str:
    return f"{conversation(state)}Question: {state['question']}"


# Nodes ----------------------------------------------------------------------------------


def route_intent(state: ChatState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    step(runtime, "route_intent")
    ctx = runtime.context
    prompt = ctx.prompts.render("chat-route", today=ctx.today.isoformat())
    route = ctx.calls.structured(
        prompts.Route,
        [SystemMessage(prompt.text), HumanMessage(ask(state))],
        run_name="route_intent",
        max_tokens=300,
        prompt=prompt,
    )
    language = route.language if route.language in ("en", "pl") else "en"
    return {
        "intent": route.intent,
        "language": language,
        "query": route.query or state["question"],
        "queries": [route.query or state["question"]],
        "searches": 0,
        "hits": [],
        "regenerations": 0,
    }


def smalltalk(state: ChatState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    step(runtime, "smalltalk")
    prompt = runtime.context.prompts.render("chat-smalltalk")
    answer = runtime.context.calls.text(
        [SystemMessage(prompt.text), HumanMessage(ask(state))],
        run_name="smalltalk",
        max_tokens=300,
        prompt=prompt,
    )
    return {"answer": answer, "sources": [], "citations": []}


def build_filter(state: ChatState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    step(runtime, "build_filter")
    ctx = runtime.context
    prompt = ctx.prompts.render("chat-plan", today=ctx.today.isoformat())
    plan = ctx.calls.structured(
        prompts.AggregatePlan,
        [SystemMessage(prompt.text), HumanMessage(ask(state))],
        run_name="build_filter",
        max_tokens=800,
        prompt=prompt,
    )
    return {"plan": plan}


def metadata_tool(state: ChatState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    step(runtime, "metadata_tool")
    return {"computed": run_plan(runtime.context.records(), state["plan"])}


def hybrid_search(state: ChatState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    step(runtime, "hybrid_search")
    ctx = runtime.context
    k = state["options"].k
    found = ctx.retriever.search(
        state["query"], ctx.user_id, DEFAULT_RETRIEVAL, k=k, filters=ctx.filters
    )
    # A second search adds its new passages to the best of the first.
    previous = state.get("hits", [])
    seen = {(h.doc_id, h.chunk) for h in found}
    merged = (found + [h for h in previous if (h.doc_id, h.chunk) not in seen])[
        : k + k // 2
    ]
    # The extracted fields of the top documents found: totals, dates, parties, terms.
    documents = list(dict.fromkeys(h.doc_id for h in merged))[: state["options"].fields]
    by_id = {r.doc_id: r for r in ctx.records()}
    return {
        "hits": merged,
        "found": [by_id[d] for d in documents if d in by_id],
        "searches": state.get("searches", 0) + 1,
    }


def grade_context(state: ChatState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    step(runtime, "grade_context")
    computed = state.get("computed")
    sources = build_sources(computed, state.get("hits", []), state.get("found", []))
    if not sources:
        return {"sufficient": False, "missing": "nothing was found"}
    guarded = state["options"].guarded
    prompt = (
        runtime.context.prompts.render("chat-grade")
        if guarded
        else code_prompt("chat-grade-open", prompts.GRADE_OPEN)
    )
    grade = runtime.context.calls.structured(
        prompts.Grade,
        [
            SystemMessage(prompt.text),
            HumanMessage(
                f"{render_sources(computed, sources, guarded)}\n\nQuestion: {state['question']}"
            ),
        ],
        run_name="grade_context",
        max_tokens=200,
        prompt=prompt,
    )
    return {"sufficient": grade.sufficient, "missing": grade.missing}


def rewrite_query(state: ChatState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    step(runtime, "rewrite_query")
    ctx = runtime.context
    tried = "\n".join(f"- {q}" for q in state.get("queries", []))
    prompt = ctx.prompts.render("chat-rewrite", today=ctx.today.isoformat())
    rewritten = ctx.calls.structured(
        prompts.RewrittenQuery,
        [
            SystemMessage(prompt.text),
            HumanMessage(
                f"{ask(state)}\n\nQueries tried:\n{tried}\n\n"
                f"Missing: {state.get('missing') or 'unknown'}"
            ),
        ],
        run_name="rewrite_query",
        max_tokens=200,
        prompt=prompt,
    )
    return {
        "query": rewritten.query,
        "queries": [*state.get("queries", []), rewritten.query],
    }


def generate(state: ChatState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    step(runtime, "generate")
    computed = state.get("computed")
    sources = build_sources(computed, state.get("hits", []), state.get("found", []))
    guarded = state["options"].guarded
    language = state.get("language", "en")
    prompt = (
        runtime.context.prompts.render("chat-answer", language=language)
        if guarded
        else code_prompt("chat-answer-open", prompts.ANSWER_OPEN, language=language)
    )
    messages: list[BaseMessage] = [
        SystemMessage(prompt.text),
        HumanMessage(f"{render_sources(computed, sources, guarded)}\n\n{ask(state)}"),
    ]
    if state.get("feedback"):
        runtime.stream_writer({"type": "reset"})
        messages.append(
            HumanMessage(
                "Your previous answer stated things the sources don't support: "
                f"{state['feedback']}. Answer again, using only the sources."
            )
        )
    answer = runtime.context.calls.text(messages, run_name="generate", prompt=prompt)
    cleaned = without_unsourced_urls(answer, sources)
    if cleaned != answer:
        # Its tokens have streamed already: replace them with the cleaned answer.
        runtime.stream_writer({"type": "reset"})
        runtime.stream_writer({"type": "token", "text": cleaned})
        answer = cleaned
    return {
        "answer": answer,
        "sources": sources,
        "citations": citations_for(answer, sources),
        "feedback": "",
    }


def stated_values(computed: AggregateResult | None) -> list[Decimal]:
    """The values the answer has to state as computed: an expression's result, or the
    one amount a single sum, average, maximum or minimum produced."""
    if computed is None or computed.error:
        return []
    if computed.expression and computed.expression_value is not None:
        return [computed.expression_value]
    if len(computed.results) == 1:
        result = computed.results[0]
        if result.query.operation in ("sum", "average", "max", "min"):
            if result.value is not None:
                return [result.value]
            return list(result.by_currency.values())
    return []


def unstated(computed: AggregateResult | None, answer: str) -> list[Decimal]:
    found = numbers_in(answer)
    return [
        value
        for value in stated_values(computed)
        if not any(abs(v - value) < Decimal("0.005") for v in found)
    ]


def led_by_another(computed: AggregateResult | None, answer: str) -> Quantity | None:
    """The amount an answer leads with when it isn't the computed result: "8000 PLN. …
    wynosi 8023,29 PLN" states the result but answers with its own rounding. The
    answer step leads with the answer, so addends and caps after it are fine."""
    values = stated_values(computed)
    first = next((q for q in quantities(answer) if q.amount and not q.negated), None)
    if not values or first is None or any(first.equals(v) for v in values):
        return None
    return first


def uncertain(state: ChatState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    note = prompts.UNCERTAIN.get(state.get("language", "en"), prompts.UNCERTAIN["en"])
    runtime.stream_writer({"type": "token", "text": note})
    return {"grounded": False, "answer": state["answer"] + note}


def check_grounding(state: ChatState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    step(runtime, "check_grounding")
    computed, sources = state.get("computed"), state.get("sources", [])
    # In code first: the answer leads with the computed result, not arithmetic of its own.
    feedback = None
    if missing := unstated(computed, state["answer"]):
        values = ", ".join(f"{v:.2f}" for v in missing)
        feedback = (
            f"it doesn't state the computed result {values}; state it exactly as"
            " computed and do no arithmetic of your own"
        )
    elif other := led_by_another(computed, state["answer"]):
        values = ", ".join(f"{v:.2f}" for v in stated_values(computed))
        feedback = (
            f"it leads with {other.text} rather than the computed result {values};"
            " lead with the computed result exactly as computed and do no arithmetic"
            " of your own"
        )
    if feedback:
        if state.get("regenerations", 0) == 0:
            return {"grounded": False, "feedback": feedback, "regenerations": 1}
        return uncertain(state, runtime)
    prompt = runtime.context.prompts.render("chat-grounding")
    verdict = runtime.context.calls.structured(
        prompts.Grounding,
        [
            SystemMessage(prompt.text),
            HumanMessage(
                f"{render_sources(computed, sources)}\n\n"
                f"Question: {state['question']}\n\n<answer>\n{state['answer']}\n</answer>"
            ),
        ],
        run_name="check_grounding",
        max_tokens=300,
        prompt=prompt,
    )
    if verdict.grounded:
        return {"grounded": True}
    if state.get("regenerations", 0) == 0:
        claims = "; ".join(verdict.unsupported) or "unsupported claims"
        return {"grounded": False, "feedback": claims, "regenerations": 1}
    return uncertain(state, runtime)


# Edges ----------------------------------------------------------------------------------


def after_route(state: ChatState) -> str:
    return {
        "smalltalk": "smalltalk",
        "retrieve": "hybrid_search",
        "aggregate": "build_filter",
        "mixed": "build_filter",
    }[state["intent"]]


def after_metadata(state: ChatState) -> str:
    return "hybrid_search" if state["intent"] == "mixed" else "generate"


def after_search(state: ChatState) -> str:
    return "grade_context" if state["options"].grade else "generate"


def after_grade(state: ChatState) -> str:
    if (
        not state.get("sufficient", True)
        and state.get("searches", 0) < state["options"].max_searches
    ):
        return "rewrite_query"
    return "generate"


def after_generate(state: ChatState) -> str:
    return "check_grounding" if state["options"].grounding else END


def after_grounding(state: ChatState) -> str:
    return "generate" if state.get("feedback") else END


def build_graph() -> CompiledStateGraph[ChatState, AgentContext, ChatState, ChatState]:
    graph = StateGraph(ChatState, context_schema=AgentContext)
    for node in (
        route_intent,
        smalltalk,
        build_filter,
        metadata_tool,
        hybrid_search,
        grade_context,
        rewrite_query,
        generate,
        check_grounding,
    ):
        graph.add_node(node.__name__, timed_node(node))
    graph.add_edge(START, "route_intent")
    graph.add_conditional_edges(
        "route_intent", after_route, ["smalltalk", "hybrid_search", "build_filter"]
    )
    graph.add_edge("smalltalk", END)
    graph.add_edge("build_filter", "metadata_tool")
    graph.add_conditional_edges(
        "metadata_tool", after_metadata, ["hybrid_search", "generate"]
    )
    graph.add_conditional_edges(
        "hybrid_search", after_search, ["grade_context", "generate"]
    )
    graph.add_conditional_edges(
        "grade_context", after_grade, ["rewrite_query", "generate"]
    )
    graph.add_edge("rewrite_query", "hybrid_search")
    graph.add_conditional_edges("generate", after_generate, ["check_grounding", END])
    graph.add_conditional_edges("check_grounding", after_grounding, ["generate", END])
    return graph.compile()


def initial_state(
    question: str,
    history: Sequence[tuple[str, str]] = (),
    options: GraphOptions | None = None,
) -> ChatState:
    return {
        "question": question,
        "history": list(history),
        "options": options or GraphOptions(),
    }


__all__ = ["ANSWER_NODES", "AgentContext", "Hit", "build_graph", "initial_state"]
