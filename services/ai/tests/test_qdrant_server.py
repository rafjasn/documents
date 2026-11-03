"""Gate checks against a real Qdrant server (payload indexes and aliases as deployed).

Without QDRANT_TEST_URL they use http://localhost:6333 (`docker compose up -d qdrant`)
and are skipped if nothing answers. CI sets QDRANT_TEST_URL for its Qdrant service
container, and then a missing server fails them.
"""

import os
import threading
import time
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient, models

from docai.core.sparse import SparseEncoder
from docai.core.vectorstore import (
    DENSE,
    SPARSE,
    SearchFilters,
    count_document,
    create_collection,
    delete_document,
    point_id,
    replace_document,
    search,
    switch_alias,
)

pytestmark = pytest.mark.qdrant

URL = os.environ.get("QDRANT_TEST_URL") or "http://localhost:6333"
ALIAS, VERSIONS = "test-gate", ("test-gate-v1", "test-gate-v2")
ENCODER = SparseEncoder()
DOCUMENTS = (
    ("user-a", "inv-a", "invoice FV/1 total 100.00", [1.0, 0.0, 0.0]),
    ("user-a", "ctr-a", "contract governing law Ireland", [0.0, 1.0, 0.0]),
    ("user-b", "inv-b", "invoice FV/2 total 999.00 secret", [1.0, 0.0, 0.0]),
)


def cleanup(client: QdrantClient) -> None:
    if any(a.alias_name == ALIAS for a in client.get_aliases().aliases):
        client.update_collection_aliases(
            change_aliases_operations=[
                models.DeleteAliasOperation(
                    delete_alias=models.DeleteAlias(alias_name=ALIAS)
                )
            ]
        )
    for name in VERSIONS:
        if client.collection_exists(name):
            client.delete_collection(name)


@pytest.fixture
def client() -> Iterator[QdrantClient]:
    client = QdrantClient(url=URL, timeout=10)
    try:
        client.get_collections()
    except Exception:
        if os.environ.get("QDRANT_TEST_URL"):
            raise
        pytest.skip(f"No Qdrant server at {URL}")
    cleanup(client)
    for name in VERSIONS:
        create_collection(client, name, 3)
        for user, doc, text, vector in DOCUMENTS:
            point = models.PointStruct(
                id=point_id(user, doc, 0),
                vector={DENSE: vector, SPARSE: ENCODER.document(text)},
                payload={"user_id": user, "doc_id": doc, "chunk_id": 0, "text": text},
            )
            replace_document(client, name, user, doc, [point])
    switch_alias(client, ALIAS, VERSIONS[0])
    yield client
    cleanup(client)


def hybrid(
    client: QdrantClient, user: str, query: str, filters: SearchFilters | None = None
) -> set[str]:
    points = search(
        client,
        ALIAS,
        user,
        dense=[1.0, 0.0, 0.0],
        sparse=ENCODER.query(query),
        limit=10,
        filters=filters,
    )
    return {str((p.payload or {})["doc_id"]) for p in points}


def test_tenants_are_isolated_even_with_crafted_filters(client: QdrantClient) -> None:
    assert hybrid(client, "user-a", "FV/2 999.00 secret") == {"inv-a", "ctr-a"}
    crafted = SearchFilters(doc_ids=("inv-b",), vendors=("anything",))
    assert (
        hybrid(client, "user-a", "secret", SearchFilters(doc_ids=("inv-b",))) == set()
    )
    assert hybrid(client, "user-a", "secret", crafted) == set()
    assert hybrid(client, "user-b", "invoice") == {"inv-b"}
    # Deleting with another user's ID removes nothing.
    delete_document(client, ALIAS, "user-a", "inv-b")
    assert count_document(client, ALIAS, "user-b", "inv-b") == 1


def test_queries_never_fail_while_the_alias_switches(client: QdrantClient) -> None:
    failures: list[BaseException] = []
    answered = 0
    stop = threading.Event()

    def query() -> None:
        nonlocal answered
        while not stop.is_set():
            try:
                assert hybrid(client, "user-a", "invoice FV/1") == {"inv-a", "ctr-a"}
                answered += 1
            except BaseException as error:  # noqa: BLE001 - every failure counts
                failures.append(error)

    threads = [threading.Thread(target=query) for _ in range(4)]
    for thread in threads:
        thread.start()
    for i in range(30):
        switch_alias(client, ALIAS, VERSIONS[(i + 1) % 2])
        time.sleep(0.05)
    stop.set()
    for thread in threads:
        thread.join()

    assert failures == []
    assert answered > 30
