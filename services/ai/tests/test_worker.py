import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from docai.core.contracts import WsDocumentStatusEvent
from docai.core.gateway import next_reset
from docai.ingest.pipeline import Pipeline
from docai.ingest.worker import Job, Worker, jobs_from_message
from tests.aws_support import Aws
from tests.ingest_support import FakeModels, text_pdf
from tests.test_contracts import FIXTURES, SCHEMA
from tests.test_gateway import litellm_error
from tests.test_pipeline import FakeParser

USER, DOC = "user-1", "doc-1"
KEY = f"uploads/{USER}/{DOC}/fv 1.pdf"


class FakeSearchIndex:
    """Records what the worker indexes and deletes."""

    def __init__(self) -> None:
        self.indexed: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str]] = []

    def index(
        self,
        parsed: Any,
        user_id: str,
        doc_id: str,
        title: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        self.indexed.append((user_id, doc_id, title))
        return {"collection": "chunks_v1", "version": 1, "chunkCount": 3}

    def delete(self, user_id: str, doc_id: str) -> int:
        self.deleted.append((user_id, doc_id))
        return 1


def worker(
    aws: Aws, parser: FakeParser | None = None, index: FakeSearchIndex | None = None
) -> Worker:
    pipeline = Pipeline(aws.settings, FakeModels(), parser=parser or FakeParser())
    search_index: Any = index or FakeSearchIndex()
    return Worker(aws.settings, aws.store, pipeline, search_index)


def test_an_upload_is_processed_with_the_nestjs_workers_statuses_and_fields(
    aws: Aws,
) -> None:
    aws.upload(text_pdf(["Faktura VAT nr FV/1"]))
    index = FakeSearchIndex()

    worker(aws, index=index).process(Job(USER, DOC, KEY))

    item = aws.item()
    assert item is not None
    assert item["status"] == "COMPLETED"
    assert item["GSI1SK"].startswith("STATUS#COMPLETED#")
    assert item["processingStartedAt"] <= item["processingCompletedAt"]
    assert item["category"] == "INVOICE"
    assert item["summary"].startswith("Faktura FV/1")
    assert item["metadata"]["names"] == [
        "Jan Kowalski",
        "Acme Sp. z o.o.",
        "Beta S.A.",
    ]
    assert item["metadata"]["confidence"] == Decimal("0.97")
    assert item["language"] == "pl"
    assert item["extracted"]["kind"] == "invoice"
    assert str(item["extracted"]["grossTotal"]) == "123.00"
    assert "extractedText" not in item
    assert item["thumbnailKey"] == f"thumbnails/{USER}/{DOC}.png"
    assert item["index"]["collection"] == "chunks_v1"
    assert index.indexed == [(USER, DOC, "fv 1.pdf")]

    stored = aws.store.aws.s3.list_objects_v2(Bucket="documents-uploads")
    keys = {o["Key"] for o in stored["Contents"]}
    assert {
        f"parsed/{USER}/{DOC}/document.json",
        f"parsed/{USER}/{DOC}/figures/picture-0.png",
        f"thumbnails/{USER}/{DOC}.png",
    } <= keys

    events = aws.events()
    assert [(e["status"], sorted(e.get("document", {}))) for e, _ in events] == [
        ("PROCESSING", []),
        ("PROCESSING", ["thumbnailKey"]),
        ("EXTRACTING", []),
        ("CLASSIFYING", []),
        ("CLASSIFYING", ["category", "metadata"]),
        ("SUMMARIZING", []),
        ("INDEXING", []),
        ("COMPLETED", ["summary"]),
    ]
    for event, attributes in events:
        jsonschema.validate(event, SCHEMA, format_checker=jsonschema.FormatChecker())
        WsDocumentStatusEvent.model_validate(event)
        # With tracing on, the SNS instrumentation adds the upload's trace context.
        attributes.pop("traceparent", None)
        assert attributes == {"eventType": "DOCUMENT_STATUS", "userId": USER}
    # The TypeScript consumer is tested against the shared fixtures; every shape the
    # worker publishes must have one.
    fixture_shapes = {
        tuple(sorted(json.loads(f.read_text()).get("document", {}))) for f in FIXTURES
    }
    assert {tuple(sorted(e.get("document", {}))) for e, _ in events} <= fixture_shapes


class BrokenParser(FakeParser):
    def parse(self, data: bytes, mime_type: str, file_name: str) -> Any:
        raise ValueError("Docling could not convert the file: damaged xref")


def handle_one(aws: Aws, the_worker: Worker) -> None:
    [message] = aws.store.receive(wait_seconds=0, visibility_timeout=30)
    asyncio.run(the_worker.handle(message))


def test_a_failure_marks_the_document_failed_and_leaves_the_message_for_a_retry(
    aws: Aws,
) -> None:
    aws.upload(text_pdf(["Faktura"]))
    aws.s3_event()

    handle_one(aws, worker(aws, BrokenParser()))

    item = aws.item()
    assert item is not None and item["status"] == "FAILED"
    assert item["errorMessage"] == "Docling could not convert the file: damaged xref"
    assert [e["status"] for e, _ in aws.events()][-1] == "FAILED"
    # Not deleted: SQS delivers it again, and to the dead-letter queue after 3 receives.
    assert aws.queue_depth() == (0, 1)


def test_a_retry_after_a_failure_clears_the_error(aws: Aws) -> None:
    aws.upload(text_pdf(["Faktura"]))
    with pytest.raises(ValueError):
        worker(aws, BrokenParser()).process(Job(USER, DOC, KEY))
    # Written by workers before Stage 05.
    aws.store.update_document(USER, DOC, fields={"extractedText": "Faktura"})

    worker(aws).process(Job(USER, DOC, KEY))

    item = aws.item()
    assert item is not None and item["status"] == "COMPLETED"
    assert "errorMessage" not in item
    assert "extractedText" not in item


def test_processed_messages_are_deleted(aws: Aws) -> None:
    aws.upload(text_pdf(["Faktura"]))
    aws.s3_event()
    handle_one(aws, worker(aws))
    assert aws.queue_depth() == (0, 0)


def test_an_upload_without_its_item_yet_comes_back_soon(aws: Aws) -> None:
    aws.store.put(KEY, text_pdf(["Faktura"]), "application/pdf")
    aws.s3_event()

    handle_one(aws, worker(aws))

    assert aws.item() is None
    assert aws.queue_depth() == (0, 1)
    assert aws.events() == []


def test_deleted_documents_are_skipped(aws: Aws) -> None:
    aws.upload(text_pdf(["Faktura"]))
    aws.store.update_document(USER, DOC, fields={"status": "DELETED"})
    aws.s3_event()

    handle_one(aws, worker(aws))

    item = aws.item()
    assert item is not None and item["status"] == "DELETED"
    assert aws.queue_depth() == (0, 0)


def test_unsupported_files_fail_without_retries(aws: Aws) -> None:
    key = f"uploads/{USER}/{DOC}/archive.zip"
    aws.upload(b"PK\x03\x04")
    aws.store.put(key, b"PK\x03\x04", "application/zip")
    aws.s3_event(key)

    handle_one(aws, worker(aws))

    item = aws.item()
    assert item is not None and item["status"] == "FAILED"
    assert "Unsupported file type" in item["errorMessage"]
    assert aws.queue_depth() == (0, 0)


class OverBudget(FakeModels):
    """The gateway refuses the document's calls: its owner's daily budget is spent."""

    def call(self, spec: str, messages: Any, **kwargs: Any) -> Any:
        raise litellm_error(422, "ExceededBudget: End User=a1b2 over budget.")


def test_an_upload_over_the_owners_budget_fails_until_the_reset_without_retries(
    aws: Aws,
) -> None:
    aws.upload(text_pdf(["Faktura"]))
    aws.s3_event()
    pipeline = Pipeline(aws.settings, OverBudget(), parser=FakeParser())
    search_index: Any = FakeSearchIndex()

    handle_one(aws, Worker(aws.settings, aws.store, pipeline, search_index))

    item = aws.item()
    assert item is not None and item["status"] == "FAILED"
    assert item["errorMessage"] == (
        "The daily limit on your AI usage has been reached. "
        f"It resets at {next_reset():%Y-%m-%d} 00:00 UTC."
    )
    assert aws.queue_depth() == (0, 0)  # dropped: a retry before the reset can't help


def test_a_deleted_upload_loses_its_chunks_artifacts_and_thumbnail(aws: Aws) -> None:
    aws.upload(text_pdf(["Faktura"]))
    worker(aws).process(Job(USER, DOC, KEY))
    assert aws.store.keys(f"parsed/{USER}/{DOC}/")
    index = FakeSearchIndex()

    worker(aws, index=index).process(Job(USER, DOC, KEY, removed=True))

    assert index.deleted == [(USER, DOC)]
    assert aws.store.keys(f"parsed/{USER}/{DOC}/") == []
    assert aws.store.keys(f"thumbnails/{USER}/") == []


def test_a_document_deleted_while_processing_stays_deleted(aws: Aws) -> None:
    aws.upload(text_pdf(["Faktura"]))

    class DeletingIndex(FakeSearchIndex):
        def index(self, *args: Any) -> dict[str, Any]:
            aws.store.update_document(USER, DOC, fields={"status": "DELETED"})
            return super().index(*args)

    deleting = DeletingIndex()
    worker(aws, index=deleting).process(Job(USER, DOC, KEY))

    item = aws.item()
    assert item is not None and item["status"] == "DELETED"
    assert deleting.deleted == [(USER, DOC)]
    assert aws.store.keys(f"parsed/{USER}/{DOC}/") == []


def test_s3_event_messages_become_jobs() -> None:
    def body(*records: dict[str, Any]) -> str:
        return json.dumps({"Records": list(records)})

    created = {
        "eventName": "ObjectCreated:Put",
        "s3": {"object": {"key": "uploads/u/d/faktura+lipiec%C5%82.pdf"}},
    }
    assert jobs_from_message(body(created)) == [
        Job("u", "d", "uploads/u/d/faktura lipiecł.pdf")
    ]
    assert jobs_from_message(
        body({**created, "eventName": "ObjectRemoved:Delete"})
    ) == [Job("u", "d", "uploads/u/d/faktura lipiecł.pdf", removed=True)]
    assert jobs_from_message(body({**created, "eventName": "ObjectRestore:Post"})) == []
    assert (
        jobs_from_message(body({"s3": {"object": {"key": "thumbnails/u/d.png"}}})) == []
    )
    assert jobs_from_message(json.dumps({"Event": "s3:TestEvent"})) == []
    assert jobs_from_message("not json") == []


def test_the_healthcheck_reads_the_age_of_the_health_file(tmp_path: Path) -> None:
    import os
    import time

    from docai.ingest.health import healthy

    path = tmp_path / "alive"
    assert not healthy(path)
    path.touch()
    assert healthy(path)
    old = time.time() - 300
    os.utime(path, (old, old))
    assert not healthy(path)
