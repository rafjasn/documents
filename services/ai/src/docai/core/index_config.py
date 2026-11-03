"""What shapes an index version's vectors: chunking, context, embedder and BM25.

A collection built with one config is never mixed with another. Kept apart from the
indexer (docai.ingest) so the AI service can read a version's config without the
ingestion dependencies.
"""

from typing import Literal

from pydantic import BaseModel

from docai.core.sparse import SparseEncoder


class IndexConfig(BaseModel):
    chunker: Literal["fixed", "structure"] = "structure"
    chunk_tokens: int = 400
    context: Literal["none", "header", "llm"] = "none"
    embedder: str = "e5-small"
    stem: int | None = None  # BM25 prefix stemming; None keeps whole words
    parent_tokens: int = 1200

    @property
    def name(self) -> str:
        stem = f"stem{self.stem}" if self.stem else "words"
        return (
            f"{self.chunker}{self.chunk_tokens}-{self.context}-{self.embedder}-{stem}"
        )

    @property
    def sparse(self) -> SparseEncoder:
        return SparseEncoder(stem=self.stem)
