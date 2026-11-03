"""Message contracts shared with the TypeScript services.

The JSON Schemas in packages/shared/contracts are the reference. These models mirror
them, and tests check both against the same fixtures and the TypeScript enum.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.alias_generators import to_camel


class DocumentStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    EXTRACTING = "EXTRACTING"
    CLASSIFYING = "CLASSIFYING"
    SUMMARIZING = "SUMMARIZING"
    INDEXING = "INDEXING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class WsDocumentStatusEvent(BaseModel):
    """Status change published to SNS (eventType=DOCUMENT_STATUS, userId attributes)."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
    )

    document_id: str = Field(min_length=1)
    status: DocumentStatus
    timestamp: datetime
    document: dict[str, Any] | None = None


class Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
    )


# Chat: the request the API sends to ai-service, and the events ai-service streams back.


class HistoryTurn(Contract):
    role: Literal["user", "assistant"]
    content: str


class ChatScope(Contract):
    """What a conversation may search; empty means the user's whole library."""

    document_ids: list[str] = Field(default=[], max_length=100)
    categories: list[Literal["INVOICE", "RECEIPT", "CONTRACT", "OTHER"]] = []
    languages: list[str] = []
    date_from: date | None = None
    date_to: date | None = None


class ChatRequest(Contract):
    conversation_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=4000)
    history: list[HistoryTurn] = Field(max_length=20)
    scope: ChatScope


class StepEvent(Contract):
    type: Literal["step"] = "step"
    node: str = Field(min_length=1)
    label: str = Field(min_length=1)


class TokenEvent(Contract):
    type: Literal["token"] = "token"
    text: str


class ResetEvent(Contract):
    type: Literal["reset"] = "reset"


class Citation(Contract):
    citation_id: int = Field(ge=1)
    doc_id: str = Field(min_length=1)
    title: str
    page: int | None = Field(ge=1)
    pages: list[int]
    bbox: tuple[float, float, float, float] | None
    chunk_id: int | None = Field(ge=0)
    snippet: str


class CitationsEvent(Contract):
    type: Literal["citations"] = "citations"
    citations: list[Citation]


class ChatUsage(Contract):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float | None = Field(ge=0)
    llm_calls: int = Field(ge=0)


class DoneEvent(Contract):
    type: Literal["done"] = "done"
    intent: Literal["smalltalk", "retrieve", "aggregate", "mixed"]
    usage: ChatUsage
    trace_id: str | None


class ErrorEvent(Contract):
    type: Literal["error"] = "error"
    message: str = Field(min_length=1)


ChatStreamEvent = Annotated[
    StepEvent | TokenEvent | ResetEvent | CitationsEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]
CHAT_STREAM_EVENT: TypeAdapter[ChatStreamEvent] = TypeAdapter(ChatStreamEvent)


def sse(
    event: StepEvent
    | TokenEvent
    | ResetEvent
    | CitationsEvent
    | DoneEvent
    | ErrorEvent,
) -> str:
    """One server-sent event: `event: <type>` and the JSON payload."""
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"
