from pathlib import Path

import pytest

from docai.corpus import manifest
from docai.corpus.manifest import DocumentRecord
from docai.ingest import seed as seed_module
from docai.ingest.seed import (
    document_id,
    safe_name,
    seed,
    upload_key,
    wait,
    wait_for_notifications,
)
from tests.aws_support import Aws

USER = "demo-user"


@pytest.fixture(autouse=True)
def bucket_notifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """moto's bucket has no notification; seeding only checks for it."""
    monkeypatch.setattr(seed_module, "wait_for_notifications", lambda store: None)


def test_seeding_waits_for_the_bucket_to_notify_the_queue(aws: Aws) -> None:
    with pytest.raises(RuntimeError, match="sends no events"):
        wait_for_notifications(aws.store, seconds=0)
    queue_arn = aws.store.aws.sqs.get_queue_attributes(
        QueueUrl=aws.settings.sqs_queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]
    aws.store.aws.s3.put_bucket_notification_configuration(
        Bucket="documents-uploads",
        NotificationConfiguration={
            "QueueConfigurations": [
                {"QueueArn": queue_arn, "Events": ["s3:ObjectCreated:*"]}
            ]
        },
    )
    wait_for_notifications(aws.store, seconds=0)


def record(doc_id: str, path: str) -> DocumentRecord:
    return DocumentRecord(
        doc_id=doc_id,
        source="synthetic",
        title=f"Faktura {doc_id} – Świech",
        category="INVOICE",
        language="pl",
        format="pdf",
        path=path,
        split="dev",
        group="syn-w3",
        license="generated",
        sha256="0" * 64,
        pages=1,
    )


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[DocumentRecord]:
    monkeypatch.setattr(manifest, "DATA_DIR", tmp_path)
    (tmp_path / "w3").mkdir()
    records = [record("syn-a", "w3/fv a.pdf"), record("syn-b", "w3/fv-b.pdf")]
    for r in records:
        r.file.write_bytes(b"%PDF-1.7 " + r.doc_id.encode())
    return records


def test_documents_get_fixed_ids_and_api_style_keys() -> None:
    assert document_id(USER, "syn-a") == document_id(USER, "syn-a")
    assert document_id(USER, "syn-a") != document_id("other-user", "syn-a")
    assert safe_name("w3/Faktura lipiec ł.pdf") == "Faktura_lipiec__.pdf"
    key = upload_key(USER, record("syn-a", "w3/fv a.pdf"))
    assert key == f"uploads/{USER}/{document_id(USER, 'syn-a')}/fv_a.pdf"


def test_seeding_writes_pending_items_and_uploads(
    aws: Aws,  # noqa: F811
    corpus: list[DocumentRecord],
) -> None:
    result = seed(aws.store, corpus, USER)

    assert result.uploaded == ["syn-a", "syn-b"]
    item = aws.store.get_document(USER, result.document_ids["syn-a"])
    assert item is not None
    assert (item["status"], item["fileName"], item["corpusDocId"]) == (
        "PENDING",
        "Faktura syn-a – Świech",
        "syn-a",
    )
    assert item["GSI1PK"] == f"DOC#{result.document_ids['syn-a']}"
    downloaded = aws.store.download(item["s3Key"])
    assert downloaded == (b"%PDF-1.7 syn-a", "application/pdf")


def test_seeding_again_uploads_only_what_is_missing_or_failed(
    aws: Aws,  # noqa: F811
    corpus: list[DocumentRecord],
) -> None:
    first = seed(aws.store, corpus, USER)
    a, b = (first.document_ids[d] for d in ("syn-a", "syn-b"))
    assert seed(aws.store, corpus, USER).uploaded == []

    # After a LocalStack restart S3 is empty but DynamoDB items remain.
    aws.store.aws.s3.delete_object(
        Bucket="documents-uploads", Key=upload_key(USER, corpus[0])
    )
    aws.store.update_document(USER, b, fields={"status": "FAILED"})
    again = seed(aws.store, corpus, USER)

    assert again.uploaded == ["syn-a", "syn-b"]
    assert aws.store.get_statuses(USER, [a, b]) == {a: "PENDING", b: "PENDING"}
    assert seed(aws.store, corpus, USER, reprocess=True).uploaded == ["syn-a", "syn-b"]
    assert seed(aws.store, corpus, USER, dry_run=True).uploaded == []


def test_waiting_ends_when_every_document_has_finished(
    aws: Aws,  # noqa: F811
    corpus: list[DocumentRecord],
) -> None:
    ids = list(seed(aws.store, corpus, USER).document_ids.values())
    aws.store.update_document(USER, ids[0], fields={"status": "COMPLETED"})
    aws.store.update_document(USER, ids[1], fields={"status": "FAILED"})

    counts = wait(aws.store, USER, ids, timeout_seconds=5)

    assert counts == {"COMPLETED": 1, "FAILED": 1}
    assert wait(aws.store, USER, [*ids, "missing"], timeout_seconds=0) == {
        "COMPLETED": 1,
        "FAILED": 1,
        "MISSING": 1,
    }
