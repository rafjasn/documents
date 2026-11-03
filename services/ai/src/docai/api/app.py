"""FastAPI app for the AI service (internal; nginx and the ALB never route to it).

- POST /v1/chat/stream: the chat graph, streamed as server-sent events (see
  packages/shared/contracts/chat-stream-event.schema.json)
- POST /v1/search: retrieval only, for debugging and evaluation
- GET /health
- GET /metrics: Prometheus metrics (docai.core.telemetry), for the ops profile's scraper

Every endpoint but /health and /metrics needs the user's bearer token; the user comes
from it alone. Requests continue the trace the API sends (W3C `traceparent`).

Run with: uvicorn --factory docai.api.app:create_app
"""

import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from docai.agent.graph import ANSWER_NODES, AgentContext, build_graph, initial_state
from docai.agent.models import AgentModels, Calls, Usage
from docai.agent.prompt_store import PromptStore
from docai.agent.records import RecordStore
from docai.api.auth import TokenVerifier, bearer_token
from docai.core.contracts import (
    ChatRequest,
    ChatScope,
    ChatUsage,
    CitationsEvent,
    DoneEvent,
    ErrorEvent,
    ResetEvent,
    StepEvent,
    TokenEvent,
    sse,
)
from docai.core.embeddings import MODELS, TeiEmbedder
from docai.core.gateway import budget_refusal
from docai.core.index_config import IndexConfig
from docai.core.logs import configure_logging
from docai.core.retrieval import DEFAULT_RETRIEVAL, Retriever
from docai.core.settings import IndexSettings, ServiceSettings
from docai.core.sparse import SparseEncoder
from docai.core.telemetry import (
    BUDGET_REFUSALS,
    CHAT_SECONDS,
    CHATS,
    REQUEST_COST,
    REQUEST_SECONDS,
    REQUESTS,
    instrument,
)
from docai.core.tracing import Tracing, setup_tracing, user_hash
from docai.core.vectorstore import SearchFilters, aliased_collection, qdrant_client

logger = logging.getLogger(__name__)

RECURSION_LIMIT = 30
FAILED = "The answer could not be generated. Please try again."


@dataclass
class Services:
    """What requests use; built from settings at startup, or given by tests."""

    verifier: TokenVerifier
    calls: Callable[[str], Calls]  # a fresh call budget for each request and end user
    retriever: Callable[[], Retriever]
    records: RecordStore
    tracing: Tracing
    today: Callable[[], date] = date.today
    prompts: PromptStore = field(default_factory=PromptStore)


def retriever_factory(settings: IndexSettings) -> Callable[[], Retriever]:
    """A Retriever for the live index version, built with that version's config."""
    client = qdrant_client(settings)
    embedders: dict[str, TeiEmbedder] = {}

    def make() -> Retriever:
        collection = aliased_collection(client, settings.qdrant_alias)
        if collection is None:
            raise RuntimeError(f"No index behind the alias {settings.qdrant_alias}")
        metadata = client.get_collection(collection).config.metadata or {}
        config = IndexConfig.model_validate(metadata.get("config") or {})
        if config.embedder not in embedders:
            embedders[config.embedder] = TeiEmbedder(
                settings.tei_embed_url, MODELS[config.embedder]
            )
        # Querying the alias, not the collection, so a switch applies at once.
        return Retriever(
            client,
            settings.qdrant_alias,
            embedders[config.embedder],
            SparseEncoder(stem=config.stem),
        )

    return make


def services_from(settings: ServiceSettings) -> Services:
    models = AgentModels(settings)
    tracing = setup_tracing(settings)
    return Services(
        verifier=TokenVerifier(settings),
        calls=lambda end_user: Calls(models, end_user=end_user),
        retriever=retriever_factory(settings),
        records=RecordStore(settings),
        tracing=tracing,
        # The versions under the label, from Langfuse; the code's prompts without it.
        prompts=PromptStore(tracing.client, settings.langfuse_prompt_label),
    )


def search_filters(scope: ChatScope) -> SearchFilters | None:
    if not (
        scope.document_ids
        or scope.categories
        or scope.languages
        or scope.date_from
        or scope.date_to
    ):
        return None
    return SearchFilters(
        doc_ids=tuple(scope.document_ids),
        categories=tuple(scope.categories),
        languages=tuple(scope.languages),
        date_from=scope.date_from,
        date_to=scope.date_to,
    )


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    scope: ChatScope = ChatScope()
    k: int = Field(10, ge=1, le=50)


def create_app(
    settings: ServiceSettings | None = None, services: Services | None = None
) -> FastAPI:
    """Build the app. Without arguments, settings come from the environment."""
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.services = services or services_from(settings or ServiceSettings())
        app.state.graph = build_graph()
        threading.Thread(target=app.state.services.prompts.warm, daemon=True).start()
        yield
        app.state.services.tracing.flush()

    app = FastAPI(title="Documents AI service", lifespan=lifespan)
    instrument(app)

    @app.middleware("http")
    async def measure(request: Request, call_next: Any) -> Any:
        started = time.monotonic()
        response = await call_next(request)
        # The route's template, not the path, so IDs don't become labels. A streamed
        # answer counts until its headers are sent; docai_chat_seconds times the rest.
        endpoint = getattr(request.scope.get("route"), "path", "unmatched")
        if endpoint not in ("/health", "/metrics"):
            REQUESTS.labels(endpoint, str(response.status_code)).inc()
            REQUEST_SECONDS.labels(endpoint).observe(time.monotonic() - started)
        return response

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    def current_user(
        request: Request, token: Annotated[str, Depends(bearer_token)]
    ) -> str:
        verifier: TokenVerifier = request.app.state.services.verifier
        return verifier.user_id(token)

    User = Annotated[str, Depends(current_user)]

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/search")
    def search(body: SearchRequest, user: User, request: Request) -> dict[str, Any]:
        found: Services = request.app.state.services
        hits = found.retriever().search(
            body.query,
            user,
            DEFAULT_RETRIEVAL,
            k=body.k,
            filters=search_filters(body.scope),
        )
        return {
            "hits": [
                {
                    "docId": h.doc_id,
                    "chunkId": h.chunk,
                    "title": h.payload.get("title"),
                    "pages": h.pages,
                    "score": round(h.score, 4),
                    "text": h.text,
                }
                for h in hits
            ]
        }

    @app.post("/v1/chat/stream")
    async def chat(
        body: ChatRequest, user: User, request: Request
    ) -> StreamingResponse:
        found: Services = request.app.state.services
        graph = request.app.state.graph
        return StreamingResponse(
            stream_answer(graph, found, user, body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


async def stream_answer(
    graph: Any, services: Services, user: str, body: ChatRequest
) -> AsyncIterator[str]:
    usage = Usage()
    handler = services.tracing.request_handler()
    callbacks: list[Any] = [usage, *([handler] if handler else [])]
    config: Any = {
        "callbacks": callbacks,
        "run_name": "chat",
        "recursion_limit": RECURSION_LIMIT,
        "metadata": {
            "langfuse_session_id": body.conversation_id,
            "langfuse_user_id": user_hash(user),
            "langfuse_tags": ["chat"],
        },
    }
    context = AgentContext(
        user_id=user,
        calls=services.calls(user_hash(user)),
        retriever=services.retriever(),
        load_records=lambda: services.records.load(user, body.scope),
        filters=search_filters(body.scope),
        today=services.today(),
        prompts=services.prompts,
    )
    state = initial_state(
        body.message, [(turn.role, turn.content) for turn in body.history]
    )
    final: dict[str, Any] = {}
    started = time.monotonic()
    try:
        async for mode, chunk in graph.astream(
            state,
            config,
            context=context,
            stream_mode=["custom", "messages", "values"],
        ):
            if mode == "custom":
                if chunk.get("type") == "step":
                    yield sse(StepEvent(node=chunk["node"], label=chunk["label"]))
                elif chunk.get("type") == "token":
                    yield sse(TokenEvent(text=chunk["text"]))
                elif chunk.get("type") == "reset":
                    yield sse(ResetEvent())
            elif mode == "messages":
                message, metadata = chunk
                if (
                    metadata.get("langgraph_node") in ANSWER_NODES
                    and isinstance(message, AIMessageChunk)
                    and message.text
                ):
                    yield sse(TokenEvent(text=message.text))
            elif mode == "values":
                final = chunk
        yield sse(CitationsEvent(citations=final.get("citations", [])))
        yield sse(
            DoneEvent(
                intent=final.get("intent", "retrieve"),
                usage=ChatUsage(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=None if usage.cost is None else round(usage.cost, 6),
                    llm_calls=usage.calls,
                ),
                trace_id=getattr(handler, "last_trace_id", None) if handler else None,
            )
        )
        CHATS.labels("answered").inc()
        if usage.cost is not None:
            REQUEST_COST.labels("chat").observe(usage.cost)
    except Exception as error:
        CHAT_SECONDS.observe(time.monotonic() - started)
        if refusal := budget_refusal(error):
            CHATS.labels(f"refused_{refusal.scope}").inc()
            BUDGET_REFUSALS.labels("chat", refusal.scope).inc()
            # Over the user's daily budget, or the global one: the event says which
            # and when it resets.
            logger.warning(
                "Chat refused for conversation %s: %s budget exceeded",
                body.conversation_id,
                refusal.scope,
            )
            yield sse(ErrorEvent(message=str(refusal)))
            return
        CHATS.labels("failed").inc()
        logger.exception("Chat failed for conversation %s", body.conversation_id)
        yield sse(ErrorEvent(message=FAILED))
    else:
        CHAT_SECONDS.observe(time.monotonic() - started)
