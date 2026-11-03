"""Optional Langfuse tracing.

This is the only module that imports Langfuse. Tracing is on when LANGFUSE_PUBLIC_KEY,
LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL are all set and off otherwise, so CI, tests
and fresh clones run without a Langfuse account. Tracing failures are logged and never
break a request.

Whatever this process sends is masked first (docai.core.masking): observations through
the client's `mask`, other OpenTelemetry spans through `mask_otel_spans`, and dataset
items before they are written. Besides the model calls, the client exports the service
spans of docai.core.telemetry (Stage 10c), unless a process asks for model spans only.
"""

import hashlib
import logging
import re
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from langchain_core.callbacks import BaseCallbackHandler

from docai.core.masking import Masker
from docai.core.settings import CoreSettings

if TYPE_CHECKING:
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExperimentRecord:
    """One scored item of a finished evaluation run, for logging as a Langfuse experiment."""

    id: str
    input: dict[str, Any]
    expected_output: dict[str, Any]
    metadata: dict[str, Any]
    output: str
    score: float
    comment: str


@dataclass(frozen=True)
class Tracing:
    """Callbacks to pass to LangChain and LangGraph calls, and the client to flush."""

    handler: CallbackHandler | None = None
    client: Langfuse | None = None
    public_key: str | None = None
    masker: Masker | None = None

    @property
    def enabled(self) -> bool:
        return self.handler is not None

    @property
    def callbacks(self) -> list[BaseCallbackHandler]:
        return [self.handler] if self.handler is not None else []

    def request_handler(self) -> CallbackHandler | None:
        """A handler of its own for one request, so concurrent requests don't share a
        trace ID. None when tracing is off or the handler can't be created."""
        if self.handler is None:
            return None
        try:
            from langfuse.langchain import CallbackHandler

            return CallbackHandler(public_key=self.public_key)
        except Exception:
            logger.warning("Langfuse handler setup failed", exc_info=True)
            return None

    def last_trace_url(self) -> str | None:
        """Langfuse URL of the most recent trace, or None when tracing is off."""
        if (
            self.handler is None
            or self.client is None
            or not self.handler.last_trace_id
        ):
            return None
        try:
            return self.client.get_trace_url(trace_id=self.handler.last_trace_id)
        except Exception:
            logger.warning("Could not build the Langfuse trace URL", exc_info=True)
            return None

    def flush(self) -> None:
        """Send buffered traces. Call before short-lived processes exit."""
        if self.client is None:
            return
        try:
            self.client.flush()
        except Exception:
            logger.warning(
                "Langfuse flush failed; traces may be missing", exc_info=True
            )

    def log_experiment(
        self,
        *,
        dataset: str,
        run_name: str,
        description: str,
        records: list[ExperimentRecord],
        metadata: dict[str, str],
    ) -> bool:
        """Upsert `records` into a Langfuse dataset and log the finished run on it.

        The task returns each item's existing output, so no model is called again.
        Returns False when tracing is off or Langfuse fails; failures never raise.
        """
        if self.client is None:
            return False
        try:
            from langfuse.experiment import Evaluation

            client = self.client
            mask = self.masker.mask if self.masker else (lambda data: data)
            client.create_dataset(name=dataset, description="Documents golden set")
            for record in records:
                client.create_dataset_item(
                    dataset_name=dataset,
                    id=record.id,
                    input=mask(record.input),
                    expected_output=mask(record.expected_output),
                    metadata={**mask(record.metadata), "golden_id": record.id},
                )
            by_id = {record.id: record for record in records}
            items = [i for i in client.get_dataset(dataset).items if i.id in by_id]

            def task(*, item: Any, **_: Any) -> str:
                output: str = mask(by_id[item.id].output)
                return output

            def correct(*, metadata: dict[str, Any] | None, **_: Any) -> Evaluation:
                record = by_id[(metadata or {})["golden_id"]]
                return Evaluation(
                    name="correct", value=record.score, comment=mask(record.comment)
                )

            client.run_experiment(
                name=dataset,
                run_name=run_name,
                description=description,
                data=items,
                task=task,
                evaluators=[correct],
                max_concurrency=4,
                metadata=metadata,
            )
            client.flush()
            return True
        except Exception:
            logger.warning("Langfuse experiment logging failed", exc_info=True)
            return False

    def observations(
        self, since: datetime, fields: str = "core,basic,io,metadata,prompt"
    ) -> Iterator[dict[str, Any]]:
        """Observations recorded since `since`, read back through Langfuse's API, as
        dicts. For gate checks on what traces contain; yields nothing with tracing off."""
        if self.client is None:
            return
        cursor = None
        while True:
            page = self.client.api.observations.get_many(
                fields=fields, from_start_time=since, limit=1000, cursor=cursor
            )
            for observation in page.data:
                yield (
                    observation if isinstance(observation, dict) else dict(observation)
                )
            cursor = getattr(page.meta, "cursor", None)
            if not cursor or not page.data:
                return


def user_hash(user_id: str) -> str:
    """The user as traces name them, and as the LLM gateway keys their daily budget: the
    same in chat and ingestion, never the ID itself."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


def setup_tracing(settings: CoreSettings, service_spans: bool = True) -> Tracing:
    """Tracing for this process. Call once at startup; it logs which mode is active.

    `service_spans` False exports only model spans, Langfuse's default."""
    public_key, secret_key = settings.langfuse_public_key, settings.langfuse_secret_key
    if public_key is None or secret_key is None or settings.langfuse_base_url is None:
        missing = settings.missing_langfuse_variables
        if len(missing) == 3:
            logger.info("Langfuse tracing off: no LANGFUSE_* variables are set")
        else:
            logger.warning("Langfuse tracing off: %s not set", ", ".join(missing))
        return Tracing()

    if settings.masking_key is None:
        logger.warning(
            "MASKING_KEY not set: masked identifiers get a key of this process only, "
            "so the same value hashes differently in other runs"
        )
        key = secrets.token_bytes(32)
    else:
        key = settings.masking_key.get_secret_value().encode()
    masker = Masker(key)
    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        from docai.core.telemetry import should_export, tracer_provider

        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key.get_secret_value(),
            base_url=settings.langfuse_base_url,
            mask=lambda *, data, **_: masker.mask(data),
            mask_otel_spans=otel_masking(masker),
            should_export_span=should_export if service_spans else None,
            tracer_provider=tracer_provider() if service_spans else None,
        )
        handler = CallbackHandler(public_key=public_key)
    except Exception:
        logger.warning("Langfuse tracing off: client setup failed", exc_info=True)
        return Tracing()

    logger.info(
        "Langfuse tracing on (%s), identifiers masked", settings.langfuse_base_url
    )
    return Tracing(handler=handler, client=client, public_key=public_key, masker=masker)


# An API key in a URL's query (`?key=...`), as some Google clients send it.
URL_KEY = re.compile(r"([?&]key=)[^&\s]+")


def otel_masking(masker: Masker) -> Any:
    """`mask_otel_spans` for the client: a patch for each string attribute that masking
    changes. It doesn't catch errors: Langfuse then drops the batch, not export it
    unmasked."""
    from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanPatch

    def mask_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult:
        patches = {}
        for identifier, span in params.spans.items():
            changed: dict[str, Any] = {}
            for name, value in span.attributes.items():
                if isinstance(value, str):
                    masked = masker.text(URL_KEY.sub(r"\1REDACTED", value))
                    if masked != value:
                        changed[name] = masked
                elif isinstance(value, (list, tuple)) and all(
                    isinstance(item, str) for item in value
                ):
                    items = [masker.text(item) for item in value]
                    if items != list(value):
                        changed[name] = items
            if changed:
                patches[identifier] = OtelSpanPatch(set_attributes=changed)
        return MaskOtelSpansResult(span_patches=patches)

    return mask_spans
