"""Chunkers over a parsed document: fixed-size windows, structure-aware chunks, and
parent groups for parent-child retrieval.

The document is first read as blocks in reading order: paragraphs and list items with
their section headings, tables as Markdown, and figures as their captions. Each block
keeps its pages and a bounding box, so every chunk can cite its pages and the frontend
can highlight them. Token counts use the multilingual-e5 tokenizer whatever the
embedding model, so the same chunks are compared across models.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal

from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DoclingDocument,
    PictureItem,
    ProvenanceItem,
    SectionHeaderItem,
    TableItem,
    TextItem,
    TitleItem,
)
from pydantic import BaseModel
from tokenizers import Tokenizer

from docai.ingest.setup import TOKENIZER_ID

Modality = Literal["text", "table", "figure"]
SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+|\n+")


@cache
def tokenizer() -> Tokenizer:
    return Tokenizer.from_pretrained(TOKENIZER_ID)


def count_tokens(text: str) -> int:
    return len(tokenizer().encode(text, add_special_tokens=False).ids)


@dataclass(frozen=True)
class Block:
    text: str
    kind: Modality
    headings: tuple[str, ...]
    pages: tuple[int, ...]
    bbox: tuple[float, float, float, float] | None  # normalised, top-left origin
    figure: str | None = None  # file name under figures/

    @property
    def tokens(self) -> int:
        return count_tokens(self.text)


class Chunk(BaseModel):
    index: int
    text: str
    modality: Modality
    headings: list[str]
    pages: list[int]
    bbox: list[float] | None
    figure: str | None = None
    tokens: int


def normalised_box(
    document: DoclingDocument, prov: ProvenanceItem
) -> tuple[float, float, float, float] | None:
    page = document.pages.get(prov.page_no)
    if page is None or not page.size.width or not page.size.height:
        return None
    box: BoundingBox = prov.bbox
    if box.coord_origin == CoordOrigin.BOTTOMLEFT:
        box = box.to_top_left_origin(page_height=page.size.height)
    w, h = page.size.width, page.size.height
    return (
        round(max(0.0, box.l / w), 4),
        round(max(0.0, box.t / h), 4),
        round(min(1.0, box.r / w), 4),
        round(min(1.0, box.b / h), 4),
    )


def provenance(
    document: DoclingDocument, provs: Sequence[ProvenanceItem]
) -> tuple[tuple[int, ...], tuple[float, float, float, float] | None]:
    pages = tuple(sorted({p.page_no for p in provs}))
    return pages, normalised_box(document, provs[0]) if provs else None


def read_blocks(document: DoclingDocument) -> list[Block]:
    """The document's content in reading order, with section headings."""
    headings: list[tuple[int, str]] = []
    blocks: list[Block] = []
    for item, _ in document.iterate_items():
        if isinstance(item, (TitleItem, SectionHeaderItem)):
            level = item.level if isinstance(item, SectionHeaderItem) else 0
            headings = [h for h in headings if h[0] < level] + [
                (level, item.text.strip())
            ]
            continue
        path = tuple(text for _, text in headings)
        if isinstance(item, TableItem):
            text = item.export_to_markdown(doc=document).strip()
            if text:
                pages, box = provenance(document, item.prov)
                blocks.append(Block(text, "table", path, pages, box))
        elif isinstance(item, PictureItem):
            description = (
                item.meta.description.text
                if item.meta is not None and item.meta.description is not None
                else None
            )
            if description:
                pages, box = provenance(document, item.prov)
                figure = (
                    Path(str(item.image.uri)).name if item.image is not None else None
                )
                blocks.append(
                    Block(
                        f"[Image] {description.strip()}",
                        "figure",
                        path,
                        pages,
                        box,
                        figure,
                    )
                )
        elif isinstance(item, TextItem) and item.text.strip():
            pages, box = provenance(document, item.prov)
            blocks.append(Block(item.text.strip(), "text", path, pages, box))
    return blocks


def split_text(text: str, max_tokens: int) -> list[str]:
    """Split at sentence and line ends; hard-split sentences longer than max_tokens."""
    pieces: list[str] = []
    current = ""
    for sentence in (s for s in SENTENCE_END.split(text) if s.strip()):
        candidate = f"{current} {sentence}".strip() if current else sentence
        if count_tokens(candidate) <= max_tokens:
            current = candidate
            continue
        if current:
            pieces.append(current)
        if count_tokens(sentence) <= max_tokens:
            current = sentence
        else:
            ids = tokenizer().encode(sentence, add_special_tokens=False)
            offsets = ids.offsets
            for start in range(0, len(offsets), max_tokens):
                window = offsets[start : start + max_tokens]
                pieces.append(sentence[window[0][0] : window[-1][1]])
            current = ""
    if current:
        pieces.append(current)
    return pieces


def split_table(markdown: str, max_tokens: int) -> list[str]:
    """Row groups, each with the header rows repeated."""
    lines = markdown.splitlines()
    header, rows = lines[:2], lines[2:]
    pieces: list[str] = []
    current: list[str] = []
    for row in rows:
        if current and count_tokens("\n".join(header + current + [row])) > max_tokens:
            pieces.append("\n".join(header + current))
            current = []
        current.append(row)
    if current or not pieces:
        pieces.append("\n".join(header + current))
    return pieces


def split_block(block: Block, max_tokens: int) -> list[Block]:
    if block.tokens <= max_tokens:
        return [block]
    if block.kind == "table":
        texts = split_table(block.text, max_tokens)
    else:
        texts = split_text(block.text, max_tokens)
    return [
        Block(t, block.kind, block.headings, block.pages, block.bbox, block.figure)
        for t in texts
    ]


@dataclass
class Group:
    blocks: list[Block] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


def first_page_box(blocks: Sequence[Block]) -> list[float] | None:
    """The region a chunk covers on its first page: the union of its blocks' boxes
    there, so a citation highlights the whole passage, not only its first paragraph."""
    pages = [b.pages[0] for b in blocks if b.pages and b.bbox]
    if not pages:
        return None
    boxes = [b.bbox for b in blocks if b.bbox and b.pages and b.pages[0] == min(pages)]
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def to_chunk(index: int, blocks: Sequence[Block]) -> Chunk:
    first = blocks[0]
    text = "\n".join(b.text for b in blocks)
    pages = sorted({p for b in blocks for p in b.pages})
    kinds = {b.kind for b in blocks}
    return Chunk(
        index=index,
        text=text,
        modality=first.kind if len(kinds) == 1 else "text",
        headings=list(first.headings),
        pages=pages,
        bbox=first_page_box(blocks),
        figure=first.figure,
        tokens=count_tokens(text),
    )


def structure_groups(blocks: Iterable[Block], max_tokens: int) -> list[list[Block]]:
    """Paragraphs merged within a section up to max_tokens; tables and figures alone."""
    groups: list[list[Block]] = []
    pending: list[Block] = []
    pending_tokens = 0
    for block in blocks:
        if block.kind != "text":
            if pending:
                groups.append(pending)
                pending, pending_tokens = [], 0
            groups += [[piece] for piece in split_block(block, max_tokens)]
            continue
        for piece in split_block(block, max_tokens):
            tokens = piece.tokens
            same_section = bool(pending) and pending[-1].headings == piece.headings
            if pending and (not same_section or pending_tokens + tokens > max_tokens):
                groups.append(pending)
                pending, pending_tokens = [], 0
            pending.append(piece)
            pending_tokens += tokens + 1
    if pending:
        groups.append(pending)
    return groups


def structure_chunks(document: DoclingDocument, max_tokens: int = 400) -> list[Chunk]:
    groups = structure_groups(read_blocks(document), max_tokens)
    return [to_chunk(i, g) for i, g in enumerate(groups)]


def parent_groups(chunks: Sequence[Chunk], max_tokens: int = 1200) -> list[int]:
    """The parent of each chunk: consecutive chunks of one section, up to max_tokens.

    Parent-child retrieval matches the (small) chunks and returns their parents for
    context. Parents group existing chunks rather than re-chunking, so they add no new
    chunks to embed or contextualise.
    """
    parents: list[int] = []
    current, tokens, section = -1, 0, None
    for chunk in chunks:
        chunk_section = tuple(chunk.headings[:1])
        if (
            current < 0
            or chunk_section != section
            or tokens + chunk.tokens > max_tokens
        ):
            current, tokens, section = current + 1, 0, chunk_section
        parents.append(current)
        tokens += chunk.tokens
    return parents


def fixed_chunks(
    document: DoclingDocument, size: int = 400, overlap: int = 50
) -> list[Chunk]:
    """Token windows over the whole text, ignoring structure: the V1 baseline."""
    blocks = read_blocks(document)
    text = ""
    spans: list[tuple[int, int, Block]] = []
    for block in blocks:
        start = len(text)
        text += block.text + "\n"
        spans.append((start, len(text), block))
    if not text.strip():
        return []
    offsets = tokenizer().encode(text, add_special_tokens=False).offsets
    chunks: list[Chunk] = []
    step = max(size - overlap, 1)
    for start in range(0, len(offsets), step):
        window = offsets[start : start + size]
        begin, end = window[0][0], window[-1][1]
        covered = [b for s, e, b in spans if s < end and e > begin]
        if not covered:
            continue
        piece = text[begin:end].strip()
        chunks.append(
            Chunk(
                index=len(chunks),
                text=piece,
                modality="text",
                headings=[],
                pages=sorted({p for b in covered for p in b.pages}),
                bbox=first_page_box(covered),
                figure=None,
                tokens=len(window),
            )
        )
        if start + size >= len(offsets):
            break
    return chunks
