"""Numbered sources for the answer step, and the citations an answer makes to them.

Sources are documents behind a computed result (their extracted fields) and retrieved
passages. The model reads them inside <source> tags as data, and cites them as [n];
citations are recovered from those markers, so the answer streams as plain text.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from xml.sax.saxutils import escape, quoteattr

from docai.agent.metadata import (
    AggregateResult,
    Record,
    describe_result,
    describe_row,
)
from docai.core.contracts import Citation
from docai.core.retrieval import Hit

SNIPPET_CHARS = 300
CITATION = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")


@dataclass(frozen=True)
class Source:
    number: int
    doc_id: str
    title: str
    text: str  # what the model reads
    page: int | None
    pages: tuple[int, ...]
    bbox: tuple[float, float, float, float] | None
    chunk_id: int | None

    def citation(self) -> Citation:
        return Citation(
            citation_id=self.number,
            doc_id=self.doc_id,
            title=self.title,
            page=self.page,
            pages=list(self.pages),
            bbox=self.bbox,
            chunk_id=self.chunk_id,
            snippet=snippet(self.text),
        )


def snippet(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= SNIPPET_CHARS else flat[: SNIPPET_CHARS - 1] + "…"


def passage_text(hit: Hit) -> str:
    headings = " > ".join(hit.payload.get("headings") or [])
    return "\n".join(part for part in (headings, hit.text) if part)


def build_sources(
    computed: AggregateResult | None,
    hits: Sequence[Hit],
    found: Sequence[Record] = (),
) -> list[Source]:
    """Documents behind computed results, then the extracted fields of documents the
    search found, then the passages."""
    sources: list[Source] = []
    records = list(computed.records) if computed else []
    seen = {r.doc_id for r in records}
    records += [r for r in found if r.doc_id not in seen and r.fields]
    for record in records:
        sources.append(
            Source(
                number=len(sources) + 1,
                doc_id=record.doc_id,
                title=record.title,
                text=describe_row(record),
                page=None,
                pages=(),
                bbox=None,
                chunk_id=None,
            )
        )
    for hit in hits:
        bbox = hit.payload.get("bbox")
        sources.append(
            Source(
                number=len(sources) + 1,
                doc_id=hit.doc_id,
                title=str(hit.payload.get("title") or hit.doc_id),
                text=passage_text(hit),
                page=hit.pages[0] if hit.pages else None,
                pages=tuple(hit.pages),
                bbox=tuple(bbox) if bbox else None,
                chunk_id=hit.chunk,
            )
        )
    return sources


def render_sources(
    computed: AggregateResult | None, sources: Sequence[Source], guarded: bool = True
) -> str:
    """Sources for a prompt: inside tags, with document text escaped so it can't close
    them. `guarded=False` is the Stage 06 injection baseline: plain, undelimited text."""
    if not guarded:
        return render_open(computed, sources)
    parts: list[str] = []
    if computed is not None:
        parts.append(
            "<computed>\n" + "\n".join(computed_lines(computed)) + "\n</computed>"
        )
    for source in sources:
        where = (
            f" pages={quoteattr(', '.join(map(str, source.pages)))}"
            if source.pages
            else ' kind="extracted fields"'
        )
        parts.append(
            f'<source id="{source.number}" document={quoteattr(source.title)}{where}>\n'
            f"{escape(source.text)}\n</source>"
        )
    return "\n\n".join(parts) if parts else "(no sources found)"


def cited_numbers(answer: str) -> list[int]:
    """Source numbers in [n], [n, m] or [n][m] markers, in order of first use."""
    numbers: list[int] = []
    for match in CITATION.finditer(answer):
        for part in re.split(r"[,;]", match.group(1)):
            number = int(part)
            if number not in numbers:
                numbers.append(number)
    return numbers


def citations_for(answer: str, sources: Sequence[Source]) -> list[Citation]:
    by_number = {s.number: s for s in sources}
    return [by_number[n].citation() for n in cited_numbers(answer) if n in by_number]


def computed_lines(computed: AggregateResult) -> list[str]:
    lines = [describe_result(r) for r in computed.results]
    if computed.expression and computed.expression_value is not None:
        lines.append(f"{computed.expression} = {computed.expression_value:.2f}")
    if computed.error:
        lines.append(f"could not compute {computed.error}")
    return lines


def render_open(computed: AggregateResult | None, sources: Sequence[Source]) -> str:
    parts: list[str] = []
    if computed is not None:
        parts.append("Computed results:\n" + "\n".join(computed_lines(computed)))
    for source in sources:
        where = (
            f", pages {', '.join(map(str, source.pages))}"
            if source.pages
            else " (extracted fields)"
        )
        parts.append(f"Source {source.number}: {source.title}{where}\n{source.text}")
    return "\n\n".join(parts) if parts else "(no sources found)"


# A markdown image or link (any target), or a bare URL.
URL = re.compile(
    r"(!?)\[([^\]]*)\]\(([^)]*)\)|https?://[^\s<>()\[\]\"']+", re.IGNORECASE
)


def without_unsourced_urls(answer: str, sources: Sequence[Source]) -> str:
    """The answer with every URL its sources don't state verbatim removed, and every
    markdown image. A URL the model built, such as an address planted in a document with
    the user's data appended, can't leave in an answer (Stage 06). A line left with no
    words, such as the appended data, goes with it."""
    stated = "\n".join(source.text for source in sources)

    def keep(match: re.Match[str]) -> str:
        if match.group(3) is not None:  # markdown: ![alt](target) or [label](target)
            target = match.group(3).strip()
            if match.group(1):
                return ""
            if target.lower().startswith("http") and target not in stated:
                return match.group(2)
            return match.group(0)
        url = match.group(0).rstrip(".,;:")
        return match.group(0) if url in stated else match.group(0)[len(url) :]

    lines = []
    for line in answer.split("\n"):
        cleaned = URL.sub(keep, line)
        if cleaned != line:
            if not re.search(r"[^\W\d_]", cleaned):
                continue
            cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).rstrip()
        lines.append(cleaned)
    result = "\n".join(lines)
    return answer if result == answer else re.sub(r"\n{3,}", "\n\n", result).strip()
