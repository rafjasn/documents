from collections.abc import Sequence

import pytest
from qdrant_client import QdrantClient

from docai.core.embeddings import MODELS
from docai.core.index_config import IndexConfig
from docai.core.vectorstore import aliased_collection, count_document
from docai.ingest.pipeline import ParsedDocument, Pipeline
from docai.ingest.search_index import (
    DEFAULT_INDEX,
    SearchIndex,
    parsed_key,
)
from tests.aws_support import USER, Aws
from tests.ingest_support import FakeModels
from tests.test_pipeline import FakeParser
from tests.test_retrieval import ContextModels


class FakeEmbedder:
    model = MODELS["e5-small"]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (self.model.dim - 1) for _ in texts]


@pytest.fixture
def index(aws: Aws) -> SearchIndex:
    search_index = SearchIndex(
        aws.settings,
        aws.store,
        ContextModels(),
        "context-model",
        client=QdrantClient(location=":memory:"),
    )
    search_index._embedders["e5-small"] = FakeEmbedder()
    return search_index


@pytest.fixture
def parsed(aws: Aws) -> ParsedDocument:
    pipeline = Pipeline(aws.settings, FakeModels(), parser=FakeParser())
    return pipeline.process(b"%PDF", "application/pdf", "fv.pdf", {})[0]


def stored(aws: Aws, parsed: ParsedDocument, doc_id: str, status: str) -> None:
    """A processed document, as the worker leaves it: the item and parsed/."""
    aws.store.put_document(
        {
            "PK": f"USER#{USER}",
            "SK": f"DOC#{doc_id}",
            "id": doc_id,
            "userId": USER,
            "status": status,
            "fileName": f"{doc_id}.pdf",
        }
    )
    aws.store.put(
        parsed_key(USER, doc_id), parsed.model_dump_json().encode(), "application/json"
    )


def test_a_fresh_stack_gets_version_1_behind_the_alias(index: SearchIndex) -> None:
    live = index.live()
    assert (live.version, live.status, live.config) == (1, "live", DEFAULT_INDEX)
    assert aliased_collection(index.client, "chunks") == "chunks_v1"
    # The config lives in the collection's metadata.
    assert index.registry.get(1) == live


def test_uploads_during_a_build_go_to_the_live_and_the_new_version(
    index: SearchIndex, parsed: ParsedDocument
) -> None:
    index.live()
    index.registry.create(2, IndexConfig(), "building")

    info = index.index(parsed, USER, "d1", "fv.pdf", {})

    assert (info["collection"], info["chunkCount"]) == ("chunks_v1", 3)
    points, _ = index.client.scroll("chunks_v1", with_payload=["chunk_id", "context"])
    # The default index writes a Gemini context for every chunk.
    assert sorted((p.payload or {})["context"] for p in points) == [
        "context 0",
        "context 1",
        "context 2",
    ]
    for collection in ("chunks_v1", "chunks_v2"):
        assert count_document(index.client, collection, USER, "d1") == 3
    assert index.delete(USER, "d1") == 2
    for collection in ("chunks_v1", "chunks_v2"):
        assert count_document(index.client, collection, USER, "d1") == 0


def test_a_rebuild_reads_parsed_artifacts_and_goes_live_by_switching(
    aws: Aws, index: SearchIndex, parsed: ParsedDocument
) -> None:
    stored(aws, parsed, "d1", "COMPLETED")
    stored(aws, parsed, "d2", "DELETED")  # deleted, its ObjectRemoved event pending
    index.live()

    stats = index.build(2, IndexConfig(stem=6))

    assert (stats.documents, stats.chunks, stats.skipped) == (1, 3, ["d2"])
    assert index.registry.get(2).status == "ready"  # type: ignore[union-attr]
    assert index.indexed_documents(2) == {(USER, "d1")}
    assert [v.version for v in index.targets()] == [1, 2]  # the worker writes both

    assert index.switch(2) == 1
    assert aliased_collection(index.client, "chunks") == "chunks_v2"
    assert index.registry.get(1).status == "retired"  # type: ignore[union-attr]
    assert [v.version for v in index.targets()] == [2]
    with pytest.raises(ValueError, match="live"):
        index.drop(2)
    index.drop(1)
    assert index.registry.get(1) is None


def test_the_build_catches_up_on_uploads_and_deletions_during_it(
    aws: Aws,
    index: SearchIndex,
    parsed: ParsedDocument,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for doc_id in ("d1", "d3", "d4"):
        stored(aws, parsed, doc_id, "COMPLETED")
    # The first listing sees d1 and d4; by the second, d3 was uploaded and d4 deleted.
    listings = iter([{(USER, "d1"), (USER, "d4")}, {(USER, "d1"), (USER, "d3")}])
    monkeypatch.setattr(index, "stored_documents", lambda: next(listings))

    stats = index.build(1, IndexConfig())

    assert (stats.caught_up, stats.removed) == (1, 1)
    assert index.indexed_documents(1) == {(USER, "d1"), (USER, "d3")}


def test_a_version_still_building_cant_go_live(index: SearchIndex) -> None:
    index.live()
    index.registry.create(2, IndexConfig(), "building")
    with pytest.raises(ValueError, match="building"):
        index.switch(2)
    with pytest.raises(ValueError, match="exists"):
        index.registry.create(2, IndexConfig(), "building")
