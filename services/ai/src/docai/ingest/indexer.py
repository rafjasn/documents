"""From a parsed document to Qdrant points: chunk, add context, embed, BM25.

An IndexConfig fixes everything that shapes the stored vectors, so a collection built
with one config is never mixed with another; its `name` keys evaluation collections,
and versioned production collections record it in the index registry.
"""

from collections.abc import Sequence
from typing import Any

from docling_core.types.doc import DoclingDocument
from qdrant_client import models

from docai.core.embeddings import Embedder, embed_passages
from docai.core.index_config import IndexConfig
from docai.core.vectorstore import DENSE, SPARSE, point_id
from docai.ingest.chunking import (
    Chunk,
    fixed_chunks,
    parent_groups,
    structure_chunks,
)
from docai.ingest.context import header_context, llm_contexts
from docai.ingest.extract import ContractFields, InvoiceFields, ReceiptFields
from docai.ingest.models import Models, Usage
from docai.ingest.pipeline import ParsedDocument


def chunk_document(parsed: ParsedDocument, config: IndexConfig) -> list[Chunk]:
    document = DoclingDocument.model_validate(parsed.document)
    if config.chunker == "fixed":
        return fixed_chunks(document, size=config.chunk_tokens)
    return structure_chunks(document, max_tokens=config.chunk_tokens)


def search_text(chunk: Chunk, context: str) -> str:
    """What is embedded and matched: context, section headings, then the chunk."""
    headings = " > ".join(chunk.headings)
    return "\n".join(part for part in (context, headings, chunk.text) if part)


def document_fields(parsed: ParsedDocument) -> dict[str, Any]:
    """Filterable fields from extraction: date, vendor or merchant, parties."""
    fields = parsed.extraction.fields
    result: dict[str, Any] = {}
    if isinstance(fields, InvoiceFields):
        result |= {"doc_date": fields.issue_date, "vendor": fields.vendor.name}
    elif isinstance(fields, ReceiptFields):
        result |= {"doc_date": fields.date, "vendor": fields.merchant.name}
    elif isinstance(fields, ContractFields):
        result |= {
            "doc_date": fields.effective_date,
            "parties": [p.name for p in fields.parties],
        }
    return {k: v for k, v in result.items() if v}


def figure_key(user_id: str, doc_id: str, figure: str | None) -> str | None:
    """Where the worker stores the figure a caption chunk describes."""
    return f"parsed/{user_id}/{doc_id}/figures/{figure}" if figure else None


def contexts_for(
    parsed: ParsedDocument,
    chunks: Sequence[Chunk],
    config: IndexConfig,
    models_: Models | None,
    spec: str | None,
    metadata: dict[str, Any],
) -> tuple[dict[int, str], list[Usage]]:
    if config.context == "header":
        header = header_context(parsed)
        return {c.index: header for c in chunks}, []
    if config.context == "llm":
        if models_ is None or spec is None:
            raise ValueError("LLM contexts need a model")
        return llm_contexts(models_, spec, parsed, chunks, metadata)
    return {}, []


def build_points(
    parsed: ParsedDocument,
    *,
    user_id: str,
    doc_id: str,
    config: IndexConfig,
    embedder: Embedder,
    title: str,
    models_: Models | None = None,
    context_model: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[list[models.PointStruct], list[Usage]]:
    chunks = chunk_document(parsed, config)
    if not chunks:
        return [], []
    contexts, usages = contexts_for(
        parsed, chunks, config, models_, context_model, metadata or {}
    )
    texts = [search_text(c, contexts.get(c.index, "")) for c in chunks]
    vectors = embed_passages(embedder, texts)
    sparse = config.sparse
    parents = parent_groups(chunks, config.parent_tokens)
    parent_texts: dict[int, list[Chunk]] = {}
    for chunk, parent in zip(chunks, parents):
        parent_texts.setdefault(parent, []).append(chunk)
    shared = {
        "user_id": user_id,
        "doc_id": doc_id,
        "title": title,
        "lang": parsed.language,
        "category": parsed.extraction.category,
        **document_fields(parsed),
    }
    points = []
    for chunk, text, vector, parent in zip(chunks, texts, vectors, parents):
        siblings = parent_texts[parent]
        points.append(
            models.PointStruct(
                id=point_id(user_id, doc_id, chunk.index),
                vector={DENSE: vector, SPARSE: sparse.document(text)},
                payload={
                    **shared,
                    "chunk_id": chunk.index,
                    "text": chunk.text,
                    "context": contexts.get(chunk.index, ""),
                    "headings": chunk.headings,
                    "modality": chunk.modality,
                    "pages": chunk.pages,
                    "page": chunk.pages[0] if chunk.pages else None,
                    "bbox": chunk.bbox,
                    "image_s3_key": figure_key(user_id, doc_id, chunk.figure),
                    "parent_id": parent,
                    "parent_text": "\n".join(c.text for c in siblings),
                    "parent_pages": sorted({p for c in siblings for p in c.pages}),
                },
            )
        )
    return points, usages
