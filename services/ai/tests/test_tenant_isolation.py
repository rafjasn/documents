"""Tenant isolation (Stage 06): one user's request never reaches another user's data.

Gathered here with the earlier guarantees they complete:
- the user is the token's subject only, and a user in the body is rejected
  (test_service.py);
- every search carries the user as a `must` filter, and crafted filters narrow within it
  (test_retrieval.py, and test_qdrant_server.py on a real server);
- the API checks scoped documents and conversations against the user
  (services/api conversations specs).

The prompt-level attempts (asking for another user's invoice, a scope naming its ID, a
document telling the assistant to search all accounts) are in the injection evaluation,
`docai security run`, which also checks nothing of the other tenant reaches the graph.
"""

import itertools
from datetime import date
from typing import Any

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models

from docai.agent.prompts import Grounding, RewrittenQuery, Route
from docai.agent.records import RecordStore
from docai.api.app import create_app
from docai.core.contracts import ChatScope
from docai.core.retrieval import RetrievalConfig, Retriever
from docai.core.settings import AwsSettings
from docai.core.sparse import SparseEncoder
from docai.core.vectorstore import (
    DENSE,
    SPARSE,
    SearchFilters,
    create_collection,
    point_id,
    replace_document,
)
from tests.service_support import (
    FakeCalls,
    FakeRecords,
    FakeRetriever,
    fake_services,
    token,
)
from tests.test_metadata import RECORDS
from tests.test_retrieval import FakeEmbedder


def test_a_scope_naming_another_users_document_reaches_nothing_of_theirs() -> None:
    retriever, records = FakeRetriever(), FakeRecords(list(RECORDS))
    calls = FakeCalls(
        {
            "route_intent": [Route(intent="retrieve", language="en", query="total")],
            # Nothing is found, so the query is rewritten once and searched again.
            "rewrite_query": [RewrittenQuery(query="invoice total")],
            "generate": ["The documents don't state it."],
            "check_grounding": [Grounding(grounded=True)],
        }
    )
    with TestClient(
        create_app(services=fake_services(calls, retriever, records))
    ) as client:
        response = client.post(
            "/v1/chat/stream",
            json={
                "conversationId": "conv-1",
                "message": "What is the total of this invoice?",
                "history": [],
                "scope": {"documentIds": ["doc-of-user-b"]},
            },
            headers={"Authorization": f"Bearer {token('user-a')}"},
        )

    # Searched as the token's user, the foreign ID only narrowing within it.
    assert response.status_code == 200 and '"type":"done"' in response.text
    assert len(retriever.searches) == 2
    for _, user, filters in retriever.searches:
        assert user == "user-a"
        assert filters is not None and filters.doc_ids == ("doc-of-user-b",)
    assert [(u, s.document_ids if s else None) for u, s in records.loads] == [
        ("user-a", ["doc-of-user-b"])
    ]


class RecordingDynamo:
    def __init__(self) -> None:
        self.queries: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        return {"Items": []}


def test_the_record_store_reads_only_the_users_partition() -> None:
    client = RecordingDynamo()
    store = RecordStore(AwsSettings(), client)  # type: ignore[arg-type]

    store.load("user-a", ChatScope(document_ids=["doc-of-user-b"]))

    [query] = client.queries
    assert query["KeyConditionExpression"].startswith("PK = :pk")
    assert query["ExpressionAttributeValues"][":pk"] == {"S": "USER#user-a"}


def test_no_combination_of_filters_reaches_another_users_chunks() -> None:
    client = QdrantClient(location=":memory:")
    create_collection(client, "chunks_v1", 3)
    encoder = SparseEncoder()
    for user, doc, text, category, lang, day in (
        ("user-a", "a-inv", "invoice total 100.00", "INVOICE", "pl", "2026-03-01"),
        (
            "user-b",
            "b-inv",
            "invoice total 999.00 secret",
            "INVOICE",
            "pl",
            "2026-03-02",
        ),
        (
            "user-b",
            "b-ctr",
            "contract governing law secret",
            "CONTRACT",
            "en",
            "2026-05-01",
        ),
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
                        "category": category,
                        "lang": lang,
                        "doc_date": day,
                    },
                )
            ],
        )
    retriever = Retriever(client, "chunks_v1", FakeEmbedder(), SparseEncoder())
    doc_ids: list[tuple[str, ...]] = [(), ("b-inv",), ("a-inv", "b-ctr")]
    categories: list[tuple[str, ...]] = [(), ("INVOICE",), ("CONTRACT",)]
    languages: list[tuple[str, ...]] = [(), ("pl",), ("en",)]
    dates: list[tuple[date | None, date | None]] = [
        (None, None),
        (date(2026, 1, 1), date(2026, 12, 31)),
    ]
    found_own = 0
    for ids, kinds, langs, (start, end) in itertools.product(
        doc_ids, categories, languages, dates
    ):
        filters = SearchFilters(
            doc_ids=ids,
            categories=kinds,
            languages=langs,
            date_from=start,
            date_to=end,
        )
        for query in ("invoice total secret", "contract governing law secret"):
            hits = retriever.search(
                query, "user-a", RetrievalConfig(sparse=True), filters=filters
            )
            assert {h.doc_id for h in hits} <= {"a-inv"}, filters
            found_own += bool(hits)
    assert found_own > 0  # the user's own document is found where the filters allow it
