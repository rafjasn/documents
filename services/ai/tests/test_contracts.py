import json
import re
from pathlib import Path
from typing import Annotated, Any

import jsonschema
import pytest
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from docai.core.contracts import (
    CHAT_STREAM_EVENT,
    ChatRequest,
    DocumentStatus,
    WsDocumentStatusEvent,
    sse,
)
from docai.ingest.extract import ContractFields, Fields, InvoiceFields, ReceiptFields
from docai.ingest.store import dynamo_value

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = REPO_ROOT / "packages" / "shared" / "contracts"
SCHEMA = json.loads((CONTRACTS / "ws-document-status-event.schema.json").read_text())
FIXTURES = sorted((CONTRACTS / "fixtures" / "ws-document-status-event").glob("*.json"))


def typescript_enum(name: str) -> list[str]:
    source = (REPO_ROOT / "packages" / "shared" / "src" / "index.ts").read_text()
    body = re.search(rf"export enum {name} \{{(.*?)\}}", source, re.DOTALL)
    assert body, f"enum {name} not found in packages/shared/src/index.ts"
    return re.findall(r"=\s*'([A-Z_]+)'", body.group(1))


def test_fixtures_exist() -> None:
    assert FIXTURES


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda path: path.stem)
def test_fixture_matches_schema_and_model(fixture: Path) -> None:
    payload = json.loads(fixture.read_text())

    jsonschema.validate(payload, SCHEMA, format_checker=jsonschema.FormatChecker())
    event = WsDocumentStatusEvent.model_validate(payload)

    roundtrip = json.loads(event.model_dump_json(exclude_none=True))
    jsonschema.validate(roundtrip, SCHEMA, format_checker=jsonschema.FormatChecker())
    assert roundtrip["documentId"] == payload["documentId"]


def test_status_values_match_typescript_schema_and_python() -> None:
    assert typescript_enum("DocumentStatus") == SCHEMA["properties"]["status"]["enum"]
    assert [status.value for status in DocumentStatus] == SCHEMA["properties"][
        "status"
    ]["enum"]


@pytest.mark.parametrize(
    "change",
    [{"status": "INDEXED"}, {"unexpected": True}, {"documentId": ""}],
    ids=["unknown-status", "extra-field", "empty-id"],
)
def test_schema_and_model_reject_the_same_payloads(change: dict[str, Any]) -> None:
    payload = json.loads(FIXTURES[0].read_text()) | change

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, SCHEMA)
    with pytest.raises(ValidationError):
        WsDocumentStatusEvent.model_validate(payload)


CHAT_EVENT_SCHEMA = json.loads(
    (CONTRACTS / "chat-stream-event.schema.json").read_text()
)
CHAT_EVENT_FIXTURES = sorted(
    (CONTRACTS / "fixtures" / "chat-stream-event").glob("*.json")
)
CHAT_REQUEST_SCHEMA = json.loads((CONTRACTS / "chat-request.schema.json").read_text())
CHAT_REQUEST_FIXTURES = sorted((CONTRACTS / "fixtures" / "chat-request").glob("*.json"))


@pytest.mark.parametrize("fixture", CHAT_EVENT_FIXTURES, ids=lambda path: path.stem)
def test_chat_events_match_schema_and_model(fixture: Path) -> None:
    payload = json.loads(fixture.read_text())

    jsonschema.validate(payload, CHAT_EVENT_SCHEMA)
    event = CHAT_STREAM_EVENT.validate_python(payload)
    assert json.loads(event.model_dump_json()) == payload
    assert sse(event).startswith(f"event: {payload['type']}\ndata: {{")


def test_every_chat_event_type_has_a_fixture() -> None:
    types = {json.loads(f.read_text())["type"] for f in CHAT_EVENT_FIXTURES}
    assert types == {
        d["properties"]["type"]["const"]
        for d in (
            CHAT_EVENT_SCHEMA["$defs"][name]
            for name in ("step", "token", "reset", "citations", "done", "error")
        )
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "token"},
        {"type": "token", "text": "a", "extra": 1},
        {"type": "unknown"},
        {"type": "error", "message": ""},
        {
            "type": "done",
            "intent": "retrieve",
            "usage": {
                "inputTokens": -1,
                "outputTokens": 0,
                "costUsd": 0,
                "llmCalls": 1,
            },
            "traceId": None,
        },
    ],
    ids=[
        "missing-text",
        "extra-field",
        "unknown-type",
        "empty-error",
        "negative-usage",
    ],
)
def test_chat_event_schema_and_model_reject_the_same_payloads(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, CHAT_EVENT_SCHEMA)
    with pytest.raises(ValidationError):
        CHAT_STREAM_EVENT.validate_python(payload)


@pytest.mark.parametrize("fixture", CHAT_REQUEST_FIXTURES, ids=lambda path: path.stem)
def test_chat_requests_match_schema_and_model(fixture: Path) -> None:
    payload = json.loads(fixture.read_text())

    jsonschema.validate(
        payload, CHAT_REQUEST_SCHEMA, format_checker=jsonschema.FormatChecker()
    )
    request = ChatRequest.model_validate(payload)
    roundtrip = json.loads(request.model_dump_json(exclude_defaults=True))
    jsonschema.validate(roundtrip, CHAT_REQUEST_SCHEMA)


@pytest.mark.parametrize(
    "change",
    [{"message": ""}, {"userId": "someone-else"}, {"scope": {"userId": "x"}}],
    ids=["empty-message", "user-in-body", "user-in-scope"],
)
def test_chat_requests_carry_no_user(change: dict[str, Any]) -> None:
    payload = json.loads(CHAT_REQUEST_FIXTURES[0].read_text()) | change

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, CHAT_REQUEST_SCHEMA)
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(payload)


EXTRACTED_SCHEMA = json.loads((CONTRACTS / "extracted-fields.schema.json").read_text())
EXTRACTED_FIXTURES = sorted(
    (CONTRACTS / "fixtures" / "extracted-fields").glob("*.json")
)
STORED_FIELDS: TypeAdapter[Fields] = TypeAdapter(
    Annotated[Fields, Field(discriminator="kind")]
)


def as_the_api_returns(fields: Fields) -> Any:
    """What the worker stores (no empty values, exact decimals), read back the way the
    API's DynamoDB client does: numbers become JavaScript numbers."""
    stored = dynamo_value(fields.model_dump(mode="python"))
    return json.loads(json.dumps(stored, default=float))


@pytest.mark.parametrize("fixture", EXTRACTED_FIXTURES, ids=lambda path: path.stem)
def test_extracted_fields_match_schema_and_model(fixture: Path) -> None:
    payload = json.loads(fixture.read_text())
    jsonschema.validate(payload, EXTRACTED_SCHEMA)

    fields = STORED_FIELDS.validate_python(payload)

    returned = as_the_api_returns(fields)
    jsonschema.validate(returned, EXTRACTED_SCHEMA)
    assert returned == payload


@pytest.mark.parametrize("model", [InvoiceFields, ReceiptFields, ContractFields])
def test_extracted_schema_lists_every_stored_field(model: type[BaseModel]) -> None:
    kind = model.model_fields["kind"].default
    properties = EXTRACTED_SCHEMA["$defs"][kind]["properties"]
    assert set(properties) == {
        info.alias or name for name, info in model.model_fields.items()
    }
