"""Load the corpus into the local stack, the way the API creates uploads.

For each corpus document the command writes a PENDING DynamoDB item and uploads the
file to S3, which queues it for the ingestion worker like any upload. It is idempotent:
each document has a fixed ID per user, and is uploaded again only when its S3 object is
missing (LocalStack loses S3 objects on restart) or its processing failed. With
INGEST_CACHE_DIR shared by the worker, a reseed makes no model calls.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from docai.corpus.manifest import DocumentRecord
from docai.ingest.store import DocumentStore, now

logger = logging.getLogger(__name__)

SEED_NAMESPACE = uuid.UUID("5b0b7c53-3d2e-4f43-9f38-0c6f3f7c4a10")
DEFAULT_USER = "corpus"
FINISHED = ("COMPLETED", "FAILED")


def document_id(user_id: str, corpus_id: str) -> str:
    return str(uuid.uuid5(SEED_NAMESPACE, f"{user_id}/{corpus_id}"))


def safe_name(file_name: str) -> str:
    """The API's file name sanitising, so keys look like real uploads."""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", Path(file_name).name)


@dataclass
class SeedResult:
    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    document_ids: dict[str, str] = field(
        default_factory=dict
    )  # corpus ID -> document ID


def upload_key(user_id: str, record: DocumentRecord) -> str:
    return f"uploads/{user_id}/{document_id(user_id, record.doc_id)}/{safe_name(record.path)}"


NOTIFICATION_WAIT_SECONDS = 60


def wait_for_notifications(
    store: DocumentStore, seconds: float = NOTIFICATION_WAIT_SECONDS
) -> None:
    """Wait until uploads reach the processing queue. LocalStack reports healthy before
    its init script has configured the bucket notification, and uploads made before then
    are never processed."""
    deadline = time.monotonic() + seconds
    while True:
        config = store.aws.s3.get_bucket_notification_configuration(
            Bucket=store.settings.s3_bucket
        )
        if config.get("QueueConfigurations"):
            return
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"{store.settings.s3_bucket} sends no events to a queue; is the stack up?"
            )
        time.sleep(2)


def seed(
    store: DocumentStore,
    records: Iterable[DocumentRecord],
    user_id: str,
    dry_run: bool = False,
    reprocess: bool = False,
) -> SeedResult:
    """Upload what is missing or failed; with `reprocess`, everything again."""
    if not dry_run:
        wait_for_notifications(store)
    result = SeedResult()
    for record in records:
        doc = document_id(user_id, record.doc_id)
        key = upload_key(user_id, record)
        result.document_ids[record.doc_id] = doc
        item = store.get_document(user_id, doc)
        loaded = item is not None and item.get("status") != "FAILED"
        if not reprocess and loaded and store.exists(key):
            result.skipped.append(record.doc_id)
            continue
        result.uploaded.append(record.doc_id)
        if dry_run:
            continue
        data = record.file.read_bytes()
        created = now()
        # The item first: the upload's S3 event starts processing straight away.
        store.put_document(
            {
                "PK": f"USER#{user_id}",
                "SK": f"DOC#{doc}",
                "GSI1PK": f"DOC#{doc}",
                "GSI1SK": f"STATUS#PENDING#{created}",
                "id": doc,
                "userId": user_id,
                "fileName": record.title,
                "originalName": safe_name(record.path),
                "mimeType": record.mime_type,
                "fileSize": len(data),
                "s3Key": key,
                "status": "PENDING",
                "retryCount": 0,
                "createdAt": created,
                "updatedAt": created,
                "corpusDocId": record.doc_id,
            }
        )
        store.put(key, data, record.mime_type)
    return result


def wait(
    store: DocumentStore,
    user_id: str,
    document_ids: list[str],
    timeout_seconds: float,
    report_every: float = 15,
) -> Counter[str]:
    """Poll DynamoDB until every document is COMPLETED or FAILED, or the timeout passes."""
    started = time.monotonic()
    last_report = 0.0
    while True:
        statuses = store.get_statuses(user_id, document_ids)
        counts = Counter(status or "MISSING" for status in statuses.values())
        finished = sum(counts[s] for s in FINISHED)
        elapsed = time.monotonic() - started
        if finished == len(document_ids) or elapsed > timeout_seconds:
            return counts
        if elapsed - last_report >= report_every:
            last_report = elapsed
            logger.info(
                "%d/%d finished after %ds: %s",
                finished,
                len(document_ids),
                elapsed,
                ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
            )
        time.sleep(3)


def login(api_url: str, email: str, password: str, register: bool) -> str:
    """The user ID of an account, through the API's auth endpoints."""

    def post(path: str) -> dict[str, str]:
        request = urllib.request.Request(
            f"{api_url.rstrip('/')}/api/auth/{path}",
            data=json.dumps({"email": email, "password": password}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body: dict[str, str] = json.loads(response.read())
            return body

    try:
        return post("login")["userId"]
    except urllib.error.HTTPError as error:
        if not register or error.code not in (400, 401, 404):
            raise
    logger.info("Registering %s", email)
    return post("register")["userId"]
