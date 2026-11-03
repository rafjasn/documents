"""A mocked AWS stack (moto): the bucket, table, queues and topic the worker uses."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from docai.core.settings import WorkerSettings
from docai.ingest.store import DocumentStore

USER, DOC = "user-1", "doc-1"
KEY = f"uploads/{USER}/{DOC}/fv 1.pdf"


@dataclass
class Aws:
    settings: WorkerSettings
    store: DocumentStore
    events_url: str

    def events(self) -> list[tuple[dict[str, Any], dict[str, str]]]:
        """Status events delivered through SNS, with their message attributes."""
        sqs = self.store.aws.sqs
        found: list[tuple[dict[str, Any], dict[str, str]]] = []
        while True:
            messages = sqs.receive_message(
                QueueUrl=self.events_url, MaxNumberOfMessages=10
            ).get("Messages", [])
            if not messages:
                return found
            for message in messages:
                envelope = json.loads(message["Body"])
                attributes = {
                    k: v["Value"] for k, v in envelope["MessageAttributes"].items()
                }
                found.append((json.loads(envelope["Message"]), attributes))
                sqs.delete_message(
                    QueueUrl=self.events_url, ReceiptHandle=message["ReceiptHandle"]
                )

    def queue_depth(self) -> tuple[int, int]:
        attributes = self.store.aws.sqs.get_queue_attributes(
            QueueUrl=self.settings.sqs_queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        return (
            int(attributes["ApproximateNumberOfMessages"]),
            int(attributes["ApproximateNumberOfMessagesNotVisible"]),
        )

    def item(self) -> dict[str, Any] | None:
        return self.store.get_document(USER, DOC)

    def upload(self, data: bytes, content_type: str = "application/pdf") -> None:
        """What the API does: a PENDING item, then the S3 upload."""
        self.store.put_document(
            {
                "PK": f"USER#{USER}",
                "SK": f"DOC#{DOC}",
                "id": DOC,
                "userId": USER,
                "status": "PENDING",
                "s3Key": KEY,
            }
        )
        self.store.put(KEY, data, content_type)

    def s3_event(self, key: str = KEY) -> None:
        record = {
            "eventName": "ObjectCreated:Post",
            "s3": {"object": {"key": key.replace(" ", "+")}},
        }
        self.store.aws.sqs.send_message(
            QueueUrl=self.settings.sqs_queue_url,
            MessageBody=json.dumps({"Records": [record]}),
        )


def mock_stack(monkeypatch: pytest.MonkeyPatch) -> Iterator[Aws]:
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_ENDPOINT", raising=False)
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(
            Bucket="documents-uploads"
        )
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="documents-documents",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs = boto3.client("sqs", region_name="us-east-1")
        sns = boto3.client("sns", region_name="us-east-1")
        queue_url = sqs.create_queue(QueueName="documents-processing")["QueueUrl"]
        topic = sns.create_topic(Name="documents-notifications")["TopicArn"]
        events_url = sqs.create_queue(QueueName="api-notifications")["QueueUrl"]
        events_arn = sqs.get_queue_attributes(
            QueueUrl=events_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=events_arn)
        settings = WorkerSettings(
            gemini_api_key="test-key",
            vision_model="google_genai:gemini-3.1-flash-lite",
            extract_model="google_genai:gemini-3.1-flash-lite",
            sqs_queue_url=queue_url,
            sns_topic_arn=topic,
            health_file=Path("/nonexistent/health"),
        )
        yield Aws(settings, DocumentStore(settings), events_url)
