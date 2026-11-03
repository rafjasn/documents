import json
import re
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic import BaseModel
from qdrant_client import QdrantClient, models

from docai.core.retrieval import Hit, RetrievalConfig, Retriever, expand_to_parents
from docai.core.sparse import SparseEncoder, amount
from docai.core.vectorstore import (
    DENSE,
    SPARSE,
    SearchFilters,
    aliased_collection,
    count_document,
    create_collection,
    delete_document,
    point_id,
    replace_document,
    switch_alias,
    tenant_filter,
)
from docai.evals.golden import GoldenItem, Relevant
from docai.evals.retrieval import metrics
from docai.ingest.chunking import (
    Block,
    Chunk,
    parent_groups,
    split_table,
    split_text,
    to_chunk,
)
from docai.ingest.models import Reply, Usage


def test_bm25_tokens_keep_identifiers_and_normalise_amounts() -> None:
    tokens = SparseEncoder().tokens("Faktura FV/2026/01/319, do zapłaty 3 564,54 zł")
    assert "fv/2026/01/319" in tokens and "3564.54" in tokens
    assert "zaplaty" in tokens  # accents folded
    assert amount("1,234.56") == amount("1.234,56") == "1234.56"
    stemmed = SparseEncoder(stem=6).tokens("faktury fakturze płatności")
    assert stemmed == ["faktur", "faktur", "platno"]


def test_bm25_weights_saturate_with_term_frequency() -> None:
    encoder = SparseEncoder()
    once = encoder.document("faktura")
    many = encoder.document("faktura faktura faktura faktura")
    assert once.indices == many.indices
    assert once.values[0] < many.values[0] < 2.2  # k1 + 1


def test_the_tenant_is_always_a_must_condition() -> None:
    query_filter = tenant_filter(
        "user-a", SearchFilters(doc_ids=("d1",), categories=("INVOICE",))
    )
    assert isinstance(query_filter.must, list)
    first = query_filter.must[0]
    assert isinstance(first, models.FieldCondition)
    assert (first.key, first.match) == ("user_id", models.MatchValue(value="user-a"))
    with pytest.raises(ValueError):
        tenant_filter("")


class FakeEmbedder:
    def __init__(self) -> None:
        from docai.core.embeddings import MODELS

        self.model = MODELS["e5-small"]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # Two fixed directions: "invoice" questions and passages versus the rest.
        return [[1.0, 0.0, 0.0] if "invoice" in t else [0.0, 1.0, 0.0] for t in texts]


@pytest.fixture
def store() -> QdrantClient:
    client = QdrantClient(location=":memory:")
    create_collection(client, "chunks_v1", 3)
    encoder = SparseEncoder()
    for user, doc, text in (
        ("user-a", "inv-1", "invoice FV/1 total 100.00"),
        ("user-a", "ctr-1", "contract governing law Ireland"),
        ("user-b", "inv-2", "invoice FV/2 total 999.00 secret"),
    ):
        vector = [1.0, 0.0, 0.0] if "invoice" in text else [0.0, 1.0, 0.0]
        replace_document(
            client,
            "chunks_v1",
            user,
            doc,
            [
                models.PointStruct(
                    id=point_id(user, doc, 0),
                    vector={DENSE: vector, SPARSE: encoder.document(text)},
                    payload={
                        "user_id": user,
                        "doc_id": doc,
                        "chunk_id": 0,
                        "text": text,
                        "pages": [1],
                    },
                )
            ],
        )
    return client


def test_users_never_see_each_others_chunks(store: QdrantClient) -> None:
    retriever = Retriever(store, "chunks_v1", FakeEmbedder(), SparseEncoder())
    hybrid = RetrievalConfig(sparse=True)
    hits = retriever.search("invoice FV/2 999.00 secret", "user-a", hybrid)
    assert {h.doc_id for h in hits} <= {"inv-1", "ctr-1"}
    # A filter naming the other user's document narrows within the tenant: nothing.
    crafted = SearchFilters(doc_ids=("inv-2",))
    assert retriever.search("invoice", "user-a", hybrid, filters=crafted) == []
    assert [h.doc_id for h in retriever.search("invoice", "user-b", hybrid)] == [
        "inv-2"
    ]


def test_deleting_a_document_removes_its_points_only(store: QdrantClient) -> None:
    delete_document(store, "chunks_v1", "user-a", "inv-1")
    assert count_document(store, "chunks_v1", "user-a", "inv-1") == 0
    assert count_document(store, "chunks_v1", "user-b", "inv-2") == 1
    # Deleting by another user's ID removes nothing.
    delete_document(store, "chunks_v1", "user-a", "inv-2")
    assert count_document(store, "chunks_v1", "user-b", "inv-2") == 1


def test_the_alias_switches_to_a_new_version(store: QdrantClient) -> None:
    switch_alias(store, "chunks", "chunks_v1")
    create_collection(store, "chunks_v2", 3)
    switch_alias(store, "chunks", "chunks_v2")
    assert aliased_collection(store, "chunks") == "chunks_v2"


def chunk(index: int, tokens: int, section: str) -> Chunk:
    return Chunk(
        index=index,
        text=f"chunk {index}",
        modality="text",
        headings=[section],
        pages=[index + 1],
        bbox=None,
        tokens=tokens,
    )


def test_parents_group_consecutive_chunks_of_a_section() -> None:
    chunks = [
        chunk(0, 300, "1. Terms"),
        chunk(1, 300, "1. Terms"),
        chunk(2, 700, "1. Terms"),
        chunk(3, 100, "2. Fees"),
    ]
    assert parent_groups(chunks, max_tokens=1000) == [0, 0, 1, 2]


def test_long_text_and_tables_split_under_the_token_limit() -> None:
    from docai.ingest.chunking import count_tokens

    text = " ".join(f"Sentence number {i} about the agreement." for i in range(60))
    pieces = split_text(text, 50)
    assert len(pieces) > 1 and all(count_tokens(p) <= 50 for p in pieces)
    table = "| a | b |\n|---|---|\n" + "\n".join(
        f"| {i} | value {i} |" for i in range(40)
    )
    parts = split_table(table, 60)
    assert len(parts) > 1 and all(p.startswith("| a | b |\n|---|---|") for p in parts)


def test_a_chunk_box_covers_its_blocks_on_the_first_page() -> None:
    blocks = [
        Block("Payment terms", "text", ("5. Fees",), (2,), (0.1, 0.40, 0.5, 0.45)),
        Block(
            "Invoices are due in 30 days.",
            "text",
            ("5. Fees",),
            (2,),
            (0.1, 0.46, 0.9, 0.60),
        ),
        Block(
            "Late payment interest.", "text", ("5. Fees",), (3,), (0.1, 0.05, 0.9, 0.20)
        ),
    ]
    chunk = to_chunk(0, blocks)
    assert chunk.pages == [2, 3]
    assert chunk.bbox == [0.1, 0.40, 0.9, 0.60]
    assert to_chunk(0, [Block("x", "text", (), (), None)]).bbox is None


def hit(doc_id: str, pages: list[int], parent: int = 0) -> Hit:
    return Hit(doc_id, 0, pages, "", 1.0, {"parent_id": parent, "parent_pages": pages})


def test_retrieval_metrics_follow_labelled_documents_and_pages() -> None:
    item = GoldenItem(
        id="q",
        split="dev",
        type="cross_doc",
        language="en",
        question="?",
        answer="",
        kind="text",
        value="x",
        accepted=("x",),
        relevant=(Relevant(doc_id="a", page=2), Relevant(doc_id="b")),
        source="test",
    )
    hits = [hit("c", [1]), hit("a", [1]), hit("a", [2]), hit("b", [5])]
    result = metrics(item, hits)
    assert result["recall@10"] == 1.0
    assert result["mrr@10"] == pytest.approx(1 / 3)  # a, page 1 isn't the labelled page
    assert result["doc_recall@10"] == 1.0
    assert 0 < result["ndcg@10"] < 1


def test_parent_expansion_keeps_the_best_hit_per_parent() -> None:
    hits = [hit("a", [1], parent=0), hit("a", [2], parent=0), hit("a", [3], parent=1)]
    assert [h.pages for h in expand_to_parents(hits)] == [[1], [3]]


class ContextModels:
    """Answers context calls with one context per chunk id, plus a stray id."""

    def __init__(self) -> None:
        self.batches: list[list[int]] = []
        self.prompts: list[str] = []

    def call(
        self,
        spec: str,
        messages: list[Any],
        *,
        max_tokens: int,
        run_name: str,
        metadata: dict[str, Any],
        schema: type[BaseModel] | None = None,
    ) -> Reply:
        prompt = str(messages[1].content)
        ids = [int(i) for i in re.findall(r'<chunk id="(\d+)">', prompt)]
        self.batches.append(ids)
        self.prompts.append(prompt)
        contexts = [{"chunk": i, "context": f" context {i} "} for i in ids]
        contexts.append({"chunk": 999, "context": "stray"})
        reply = json.dumps({"contexts": contexts})
        return Reply(reply, Usage(spec, 1000, 100, 0.1, cached=False))


def test_llm_contexts_batch_chunks_after_the_whole_document() -> None:
    from docai.ingest.context import CHUNKS_PER_CALL, llm_contexts
    from docai.ingest.pipeline import Pipeline
    from tests.ingest_support import FakeModels, pipeline_settings
    from tests.test_pipeline import FakeParser

    pipeline = Pipeline(pipeline_settings(), FakeModels(), parser=FakeParser())
    parsed, _ = pipeline.process(b"%PDF", "application/pdf", "fv.pdf", {})
    chunks = [chunk(i, 10, "1. Terms") for i in range(CHUNKS_PER_CALL + 2)]
    models_ = ContextModels()

    contexts, usages = llm_contexts(models_, "model", parsed, chunks, {})

    assert models_.batches == [list(range(CHUNKS_PER_CALL)), [12, 13]]
    assert contexts == {i: f"context {i}" for i in range(14)}  # stray id ignored
    assert len(usages) == 2
    # The document comes first, so a document's calls share a cacheable prefix.
    assert all(p.startswith("<document>\nFaktura VAT nr FV/1") for p in models_.prompts)
