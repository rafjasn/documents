import json
from typing import Any

import jsonschema
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from docai.agent.prompts import Grade, Grounding, Route
from docai.api.app import create_app
from docai.core.gateway import next_reset
from tests.service_support import (
    FakeCalls,
    FakeRecords,
    FakeRetriever,
    fake_services,
    hit,
    token,
)
from tests.test_contracts import CHAT_EVENT_SCHEMA
from tests.test_gateway import litellm_error
from tests.test_metadata import RECORDS

REQUEST = {
    "conversationId": "conv-1",
    "message": "What is the total on invoice FV/1?",
    "history": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello!"},
    ],
    "scope": {"documentIds": ["doc-1"]},
}


def events(body: str) -> list[dict[str, Any]]:
    """Parse server-sent events; each has an event name and a JSON payload."""
    parsed = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        payload = json.loads(lines["data"])
        assert lines["event"] == payload["type"]
        parsed.append(payload)
    return parsed


def answering_calls() -> FakeCalls:
    return FakeCalls(
        {
            "route_intent": [
                Route(intent="retrieve", language="en", query="FV/1 total")
            ],
            "grade_context": [Grade(sufficient=True)],
            "generate": ["The total is 1,230.00 PLN [1]."],
            "check_grounding": [Grounding(grounded=True)],
        }
    )


def test_a_chat_streams_steps_tokens_citations_and_usage() -> None:
    retriever = FakeRetriever(
        results=[[hit("doc-1", 2, "Gross total 1,230.00 PLN", title="FV 1.pdf")]]
    )
    services = fake_services(answering_calls(), retriever)

    with TestClient(create_app(services=services)) as client:
        response = client.post(
            "/v1/chat/stream",
            json=REQUEST,
            headers={"Authorization": f"Bearer {token('user-a')}"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    stream = events(response.text)
    for event in stream:
        jsonschema.validate(event, CHAT_EVENT_SCHEMA)
    tokens = "".join(e["text"] for e in stream if e["type"] == "token")
    assert tokens == "The total is 1,230.00 PLN [1]."
    assert [e["type"] for e in stream][-2:] == ["citations", "done"]
    [citation] = stream[-2]["citations"]
    assert (citation["docId"], citation["chunkId"], citation["page"]) == ("doc-1", 2, 1)
    assert stream[-1]["intent"] == "retrieve" and stream[-1]["traceId"] is None
    assert [e["node"] for e in stream if e["type"] == "step"][0] == "route_intent"
    # The user is the token's subject, and the scope narrows the search within it.
    [(_, user, filters)] = retriever.searches
    assert user == "user-a" and filters is not None and filters.doc_ids == ("doc-1",)


def test_the_metadata_tool_reads_only_the_token_users_documents() -> None:
    records = FakeRecords(list(RECORDS))
    from docai.agent.metadata import AggregatePlan, MetadataQuery

    calls = FakeCalls(
        {
            "route_intent": [Route(intent="aggregate", language="en", query="count")],
            "build_filter": [
                AggregatePlan(
                    queries=[
                        MetadataQuery(name="n", category="invoice", operation="count")
                    ]
                )
            ],
            "generate": ["5 invoices [1]."],
            "check_grounding": [Grounding(grounded=True)],
        }
    )
    with TestClient(
        create_app(services=fake_services(calls, records=records))
    ) as client:
        client.post(
            "/v1/chat/stream",
            json=REQUEST | {"scope": {}},
            headers={"Authorization": f"Bearer {token('user-b')}"},
        )

    assert [user for user, _ in records.loads] == ["user-b"]


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Basic abc",
        f"Bearer {token(expires_in=-60)}",
        f"Bearer {token(audience='another-client')}",
        f"Bearer {token(issuer='http://evil.test/realms/documents')}",
        f"Bearer {token(key=rsa.generate_private_key(public_exponent=65537, key_size=2048))}",
    ],
    ids=[
        "missing",
        "not-bearer",
        "expired",
        "wrong-audience",
        "wrong-issuer",
        "wrong-key",
    ],
)
def test_requests_without_a_valid_token_are_rejected(authorization: str | None) -> None:
    retriever = FakeRetriever()
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(create_app(services=fake_services(retriever=retriever))) as client:
        chat = client.post("/v1/chat/stream", json=REQUEST, headers=headers)
        search = client.post("/v1/search", json={"query": "x"}, headers=headers)

    assert chat.status_code == search.status_code == 401
    assert retriever.searches == []


def test_a_user_in_the_body_is_rejected() -> None:
    with TestClient(create_app(services=fake_services())) as client:
        response = client.post(
            "/v1/chat/stream",
            json=REQUEST | {"userId": "user-b"},
            headers={"Authorization": f"Bearer {token('user-a')}"},
        )
    assert response.status_code == 422


def test_a_failure_ends_the_stream_with_an_error_event() -> None:
    calls = FakeCalls({})  # no scripted replies: the first model call fails
    with TestClient(create_app(services=fake_services(calls))) as client:
        response = client.post(
            "/v1/chat/stream",
            json=REQUEST,
            headers={"Authorization": f"Bearer {token()}"},
        )
    stream = events(response.text)
    assert stream[-1] == {
        "type": "error",
        "message": "The answer could not be generated. Please try again.",
    }
    assert "no scripted reply" not in response.text  # internals stay in the logs


class OverBudget(FakeCalls):
    """The gateway refuses every call: the user's daily budget is spent."""

    def _next(self, run_name: str, messages: Any) -> Any:
        raise litellm_error(
            422, "ExceededBudget: End User=a1b2 over budget. Spend=0.51, Budget=0.5"
        )


def test_a_user_over_the_daily_budget_is_told_so() -> None:
    with TestClient(create_app(services=fake_services(OverBudget({})))) as client:
        response = client.post(
            "/v1/chat/stream",
            json=REQUEST,
            headers={"Authorization": f"Bearer {token()}"},
        )
    stream = events(response.text)
    assert stream[-1] == {
        "type": "error",
        "message": "The daily limit on your AI usage has been reached. "
        f"It resets at {next_reset():%Y-%m-%d} 00:00 UTC.",
    }


def test_search_returns_the_token_users_hits() -> None:
    retriever = FakeRetriever(results=[[hit("doc-1", 0, "text")]])
    with TestClient(create_app(services=fake_services(retriever=retriever))) as client:
        response = client.post(
            "/v1/search",
            json={"query": "total", "k": 3},
            headers={"Authorization": f"Bearer {token('user-c')}"},
        )
    assert response.status_code == 200
    assert response.json()["hits"][0]["docId"] == "doc-1"
    assert retriever.searches == [("total", "user-c", None)]


def test_the_service_imports_no_ingestion_code() -> None:
    import subprocess
    import sys

    code = (
        "import sys; import docai.api.app; "
        "bad = [m for m in sys.modules if m.startswith(('docai.ingest', 'docling', 'torch'))]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout
