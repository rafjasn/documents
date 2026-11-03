"""State of one chat request as it moves through the graph."""

from dataclasses import dataclass
from typing import Literal, TypedDict

from docai.agent.metadata import AggregatePlan, AggregateResult, Record
from docai.agent.sources import Source
from docai.core.contracts import Citation
from docai.core.retrieval import Hit

Intent = Literal["smalltalk", "retrieve", "aggregate", "mixed"]


@dataclass(frozen=True)
class GraphOptions:
    """Switches for the evaluation's ablation rows; the defaults are the service's."""

    grade: bool = True  # grade the search results, and search again if they fall short
    grounding: bool = True  # check the answer against its sources, regenerate once
    max_searches: int = 2
    k: int = 10  # passages the answer step reads, as in the Stage 03 evaluation
    fields: int = 5  # documents found by search whose extracted fields it also reads
    # Sources inside escaped tags, with rules that they are data and never instructions.
    # Off only for the Stage 06 injection baseline.
    guarded: bool = True


class ChatState(TypedDict, total=False):
    question: str
    history: list[tuple[str, str]]  # (role, content), oldest first
    options: GraphOptions
    intent: Intent
    language: str
    query: str
    queries: list[str]
    searches: int
    hits: list[Hit]
    sufficient: bool
    missing: str
    plan: AggregatePlan
    computed: AggregateResult
    found: list[Record]  # documents the search found, for their extracted fields
    sources: list[Source]
    answer: str
    citations: list[Citation]
    grounded: bool
    feedback: str
    regenerations: int
