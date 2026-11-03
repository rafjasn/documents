"""The ingestion worker: an asyncio consumer of the S3 upload queue.

Queue behaviour matches the NestJS worker it replaces: the same queue and long polling,
a 120 s visibility timeout, and failed messages left on the queue, so SQS delivers them
again and moves them to the dead-letter queue after three receives. Two additions: a
heartbeat extends the visibility timeout while a long document is still processing,
and a message whose document item doesn't exist yet comes back after 10 s.

Uploads are parsed, described, extracted and indexed (status INDEXING) into the live
search index. Deleted uploads (S3 ObjectRemoved events) have their chunks, parsed
artifacts and thumbnail removed.

One message at a time. Processing runs in a worker thread, so the event loop stays
free for the heartbeat and for shutdown signals; SIGTERM finishes the current document.

An upload refused by the LLM gateway (its owner's daily budget, or the global one, is
spent) is marked FAILED with the reason and the reset time, and its message is dropped:
retrying before the reset can't help, and uploading the file again after it does.

Telemetry (Stage 10c): each new upload is one trace, from its download to the index (a
file already in the cache isn't traced), and the worker's metrics (uploads by status,
each message's age at receipt from its SentTimestamp, the queue's depth) are served on
its metrics port. It serves no API.
"""

import asyncio
import json
import logging
import signal
import time
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from docai.core.contracts import DocumentStatus
from docai.core.gateway import BudgetExceeded, budget_refusal
from docai.core.settings import WorkerSettings
from docai.core.telemetry import (
    BUDGET_REFUSALS,
    DOCUMENT_SECONDS,
    DOCUMENTS,
    MESSAGE_AGE,
    QUEUE_MESSAGES,
    REQUEST_COST,
    span,
)
from docai.core.tracing import user_hash
from docai.ingest.parse import UnsupportedFileError, resolve_mime_type
from docai.ingest.pipeline import ParsedDocument, Pipeline
from docai.ingest.search_index import SearchIndex
from docai.ingest.store import DocumentStore
from docai.ingest.thumbnail import thumbnail

if TYPE_CHECKING:
    from types_boto3_sqs.type_defs import MessageTypeDef

logger = logging.getLogger(__name__)

RETRY_SOON_SECONDS = 10
QUARANTINE_EXCERPTS = 3  # stored on the item for the document page
MAX_CONSECUTIVE_POLL_FAILURES = 5
POLL_FAILURE_COOLDOWN_SECONDS = 30
QUEUE_DEPTH_SECONDS = 15  # how often the queue's depth is read for the metrics


@dataclass(frozen=True)
class Job:
    user_id: str
    document_id: str
    s3_key: str
    removed: bool = False  # an ObjectRemoved event: clean up instead of processing

    @property
    def file_name(self) -> str:
        return self.s3_key.rsplit("/", 1)[-1]


class RetrySoon(Exception):
    """The message should come back shortly instead of after the visibility timeout."""


def jobs_from_message(body: str) -> list[Job]:
    """Jobs in an S3 event notification (uploads/{userId}/{documentId}/{file})."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("Ignoring a message that isn't JSON")
        return []
    jobs = []
    for record in payload.get("Records", []) if isinstance(payload, dict) else []:
        event = str(record.get("eventName", "ObjectCreated"))
        if not event.startswith(("ObjectCreated", "ObjectRemoved")):
            continue
        raw_key = record.get("s3", {}).get("object", {}).get("key")
        if not raw_key:
            continue
        key = urllib.parse.unquote_plus(raw_key)
        parts = key.split("/")
        if len(parts) < 4 or parts[0] != "uploads":
            logger.warning("Unexpected S3 key format: %s", key)
            continue
        removed = event.startswith("ObjectRemoved")
        jobs.append(Job(parts[1], parts[2], key, removed=removed))
    return jobs


def frontend_metadata(parsed: ParsedDocument) -> dict[str, Any]:
    """The `metadata` map the frontend already shows, in the NestJS worker's shape."""
    extraction = parsed.extraction
    metadata: dict[str, Any] = {
        "keywords": extraction.metadata.keywords,
        "tags": extraction.metadata.tags or [extraction.category.lower()],
        "names": [*extraction.metadata.people, *extraction.metadata.organizations],
        "locations": extraction.metadata.locations,
        "language": parsed.language,
        "confidence": extraction.confidence,
    }
    if parsed.page_count:
        metadata["pageCount"] = parsed.page_count
    return metadata


class Worker:
    def __init__(
        self,
        settings: WorkerSettings,
        store: DocumentStore,
        pipeline: Pipeline,
        search_index: SearchIndex | None,
    ) -> None:
        self.settings, self.store, self.pipeline = settings, store, pipeline
        self.search_index = search_index

    # Processing one document (runs in a worker thread) ----------------------------------

    def process(self, job: Job, attributes: dict[str, Any] | None = None) -> None:
        """Process one job; `attributes` describe its message, for the upload's trace."""
        if job.removed:
            self.remove(job)
            return
        item = self.store.get_document(job.user_id, job.document_id)
        if item is None:
            raise RetrySoon(f"document {job.document_id} has no item yet")
        if item.get("status") == "DELETED":
            logger.info("Skipping deleted document %s", job.document_id)
            return
        started = time.monotonic()
        try:
            self._process(job, str(item.get("fileName") or job.file_name), attributes)
        except Exception as error:
            refusal = budget_refusal(error)
            if refusal:
                logger.warning(
                    "Refused %s: %s budget exceeded", job.document_id, refusal.scope
                )
                DOCUMENTS.labels("refused").inc()
                BUDGET_REFUSALS.labels("upload", refusal.scope).inc()
            else:
                logger.exception("Failed %s", job.document_id)
                DOCUMENTS.labels("failed").inc()
            self.store.update_document(
                job.user_id,
                job.document_id,
                DocumentStatus.FAILED,
                {"errorMessage": str(refusal or error) or type(error).__name__},
            )
            self.store.publish(job.user_id, job.document_id, DocumentStatus.FAILED)
            if refusal:
                raise refusal from error
            raise
        finally:
            DOCUMENT_SECONDS.observe(time.monotonic() - started)

    def _status(
        self, job: Job, status: DocumentStatus, fields: dict[str, Any] | None = None
    ) -> None:
        # extractedText was the NestJS chat's context until Stage 04; nothing reads it.
        remove = (
            ("errorMessage", "extractedText", "quarantined")
            if status == DocumentStatus.PROCESSING
            else ()
        )
        self.store.update_document(job.user_id, job.document_id, status, fields, remove)
        self.store.publish(job.user_id, job.document_id, status)

    def remove(self, job: Job) -> None:
        """An upload was deleted: drop its chunks from every index version, its parsed
        artifacts and its thumbnail. The API has already marked the item DELETED."""
        user, document = job.user_id, job.document_id
        if self.search_index is not None:
            self.search_index.delete(user, document)
        removed = self.store.delete_prefix(f"parsed/{user}/{document}/")
        self.store.delete(f"thumbnails/{user}/{document}.png")
        logger.info(
            "Removed %s: chunks, %d parsed objects, thumbnail", document, removed
        )

    def _process(
        self, job: Job, title: str, attributes: dict[str, Any] | None = None
    ) -> None:
        logger.info("Processing %s", job.document_id)
        self._status(job, DocumentStatus.PROCESSING)
        downloaded = self.store.download(job.s3_key)
        if downloaded is None:
            raise FileNotFoundError(
                f"s3://{self.settings.s3_bucket}/{job.s3_key} is missing"
            )
        data, content_type = downloaded
        if self.pipeline.is_cached(data):
            # Processed before: served from the cache with no model call, so there is
            # nothing for a trace to show (a reseed would spend Langfuse units on it).
            self._ingest(job, title, data, content_type)
            return
        # One trace per new upload, from here to the index.
        with span("ingest", **(attributes or {"document.id": job.document_id})):
            self._ingest(job, title, data, content_type)

    def _ingest(self, job: Job, title: str, data: bytes, content_type: str) -> None:
        user, document = job.user_id, job.document_id
        mime_type = resolve_mime_type(content_type, job.file_name)
        self._thumbnail(job, data, mime_type)

        self._status(job, DocumentStatus.EXTRACTING)
        # Langfuse groups a document's calls into one session; the user ID is hashed.
        metadata = {
            "document_id": document,
            "langfuse_session_id": f"ingest-{document}",
            "langfuse_user_id": user_hash(user),
            "langfuse_tags": ["ingest"],
        }
        read = self.pipeline.read(data, mime_type, job.file_name, metadata)

        self._status(job, DocumentStatus.CLASSIFYING)
        extraction, usage = self.pipeline.extract(read, metadata)
        parsed = self.pipeline.assemble(read, extraction, usage)
        classified: dict[str, Any] = {
            "category": extraction.category,
            "metadata": frontend_metadata(parsed),
        }
        stored = {**classified, "language": parsed.language}
        if parsed.quarantined:
            stored["quarantined"] = parsed.quarantined[:QUARANTINE_EXCERPTS]
        if extraction.fields is not None:
            stored["extracted"] = extraction.fields.model_dump(mode="python")
        self.store.update_document(user, document, fields=stored)
        self.store.publish(user, document, DocumentStatus.CLASSIFYING, classified)

        self._status(job, DocumentStatus.SUMMARIZING)
        prefix = f"parsed/{user}/{document}"
        for figure in read.parse.figures:
            self.store.put(
                f"{prefix}/figures/{figure.file_name}", figure.data, figure.mime_type
            )
        self.store.put(
            f"{prefix}/document.json",
            parsed.model_dump_json().encode(),
            "application/json",
        )
        completed: dict[str, Any] = {"summary": extraction.summary}
        if self.search_index is not None:
            self._status(job, DocumentStatus.INDEXING)
            completed["index"] = self.search_index.index(
                parsed, user, document, title, metadata
            )
        current = self.store.get_document(user, document) or {}
        if current.get("status") == "DELETED":
            # Deleted while processing: don't bring it back or leave its chunks behind.
            self.remove(job)
            DOCUMENTS.labels("deleted").inc()
            return
        self.store.update_document(user, document, DocumentStatus.COMPLETED, completed)
        DOCUMENTS.labels("completed").inc()
        REQUEST_COST.labels("upload").observe(
            float(sum(c.cost_usd or 0 for c in parsed.calls if not c.cached))
        )
        self.store.publish(
            user, document, DocumentStatus.COMPLETED, {"summary": extraction.summary}
        )
        logger.info(
            "Completed %s: %s, %s, %d pages, %d figures, $%.5f (%s)",
            document,
            extraction.category,
            parsed.language,
            parsed.page_count,
            len(parsed.figures),
            parsed.cost_usd,
            ", ".join(f"{k} {v}s" for k, v in parsed.timings.items()),
        )

    def _thumbnail(self, job: Job, data: bytes, mime_type: str) -> None:
        """Thumbnail failures are logged and never fail the document, as before."""
        key = f"thumbnails/{job.user_id}/{job.document_id}.png"
        try:
            self.store.put(key, thumbnail(data, mime_type), "image/png")
            self.store.update_document(
                job.user_id, job.document_id, fields={"thumbnailKey": key}
            )
            self.store.publish(
                job.user_id,
                job.document_id,
                DocumentStatus.PROCESSING,
                {"thumbnailKey": key},
            )
        except Exception:
            logger.warning("Thumbnail failed for %s", job.document_id, exc_info=True)

    # The queue --------------------------------------------------------------------------

    def alive(self) -> None:
        """Touch the health file that the container's healthcheck reads."""
        try:
            self.settings.health_file.touch()
        except OSError:
            logger.debug("Could not touch %s", self.settings.health_file)

    async def heartbeat(self, receipt_handle: str) -> None:
        interval = max(self.settings.sqs_visibility_timeout // 2, 5)
        while True:
            await asyncio.sleep(interval)
            self.alive()
            try:
                await asyncio.to_thread(
                    self.store.extend_visibility,
                    receipt_handle,
                    self.settings.sqs_visibility_timeout,
                )
            except Exception:
                logger.warning("Could not extend message visibility", exc_info=True)

    async def handle(self, message: MessageTypeDef) -> None:
        receipt = message["ReceiptHandle"]
        attributes = message.get("Attributes", {})
        receives = attributes.get("ApproximateReceiveCount", "1")
        age = None
        if sent := attributes.get("SentTimestamp"):
            age = max(time.time() - int(sent) / 1000, 0.0)
            MESSAGE_AGE.observe(age)
        heartbeat = asyncio.create_task(self.heartbeat(receipt))
        try:
            for job in jobs_from_message(message.get("Body", "")):
                logger.info("Job %s (receive %s)", job.s3_key, receives)
                described: dict[str, Any] = {
                    "document.id": job.document_id,
                    "messaging.message.id": message.get("MessageId"),
                    "messaging.receive_count": int(receives),
                    "messaging.message.age_seconds": age,
                }
                await asyncio.to_thread(self.process, job, described)
        except RetrySoon as reason:
            logger.info("Retrying in %ds: %s", RETRY_SOON_SECONDS, reason)
            await asyncio.to_thread(
                self.store.extend_visibility, receipt, RETRY_SOON_SECONDS
            )
            return
        except (UnsupportedFileError, FileNotFoundError, BudgetExceeded) as error:
            # Retrying can't help; the document is already marked FAILED.
            logger.warning("Dropping message: %s", error)
        except Exception:
            # Left on the queue: SQS redelivers it after the visibility timeout and moves
            # it to the dead-letter queue after the third receive.
            return
        finally:
            heartbeat.cancel()
        await asyncio.to_thread(self.store.delete_message, receipt)

    async def watch_queue(self, stop: asyncio.Event) -> None:
        """The queue's depth, for the metrics."""
        while not stop.is_set():
            try:
                waiting, in_flight = await asyncio.to_thread(self.store.queue_depth)
                QUEUE_MESSAGES.labels("visible").set(waiting)
                QUEUE_MESSAGES.labels("in_flight").set(in_flight)
            except Exception:
                logger.debug("Could not read the queue's depth", exc_info=True)
            await sleep_or_stop(stop, QUEUE_DEPTH_SECONDS)

    async def run(self, stop: asyncio.Event) -> None:
        logger.info("Ingestion worker polling %s", self.settings.sqs_queue_url)
        watcher = asyncio.create_task(self.watch_queue(stop))
        failures = 0
        while not stop.is_set():
            self.alive()
            try:
                messages = await asyncio.to_thread(
                    self.store.receive,
                    self.settings.sqs_wait_seconds,
                    self.settings.sqs_visibility_timeout,
                )
                failures = 0
            except Exception:
                failures += 1
                logger.exception(
                    "Poll error (%d/%d)", failures, MAX_CONSECUTIVE_POLL_FAILURES
                )
                if failures >= MAX_CONSECUTIVE_POLL_FAILURES:
                    logger.warning(
                        "Cooling down for %ds", POLL_FAILURE_COOLDOWN_SECONDS
                    )
                    await sleep_or_stop(stop, POLL_FAILURE_COOLDOWN_SECONDS)
                    failures = 0
                else:
                    await sleep_or_stop(stop, 1)
                continue
            for message in messages:
                await self.handle(message)
        watcher.cancel()
        logger.info("Ingestion worker stopped")


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def serve(worker: Worker) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await worker.run(stop)
