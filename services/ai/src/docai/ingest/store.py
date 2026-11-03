"""AWS I/O for the worker and the seed command: S3, DynamoDB, SNS and SQS.

Document items and status events keep the shapes the NestJS worker used, so the API
and the frontend need no changes: the same keys and attribute names, and
WsDocumentStatusEvent on SNS with eventType=DOCUMENT_STATUS and userId attributes.
boto3 clients are thread-safe; the worker calls them from worker threads.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from docai.core.contracts import DocumentStatus, WsDocumentStatusEvent
from docai.core.settings import AwsSettings

if TYPE_CHECKING:
    from types_boto3_dynamodb import DynamoDBClient
    from types_boto3_s3 import S3Client
    from types_boto3_s3.type_defs import ObjectIdentifierTypeDef
    from types_boto3_sns import SNSClient
    from types_boto3_sqs import SQSClient
    from types_boto3_sqs.type_defs import MessageTypeDef

logger = logging.getLogger(__name__)

TIMESTAMPED = {
    DocumentStatus.PROCESSING: "processingStartedAt",
    DocumentStatus.COMPLETED: "processingCompletedAt",
    DocumentStatus.FAILED: "processingCompletedAt",
}


def now() -> str:
    """UTC timestamp in the form JavaScript's toISOString() gives the NestJS services."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dynamo_value(value: Any) -> Any:
    """Floats become Decimals: DynamoDB numbers are exact, and boto3 rejects floats."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: dynamo_value(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [dynamo_value(v) for v in value]
    return value


def document_key(user_id: str, document_id: str) -> dict[str, str]:
    return {"PK": f"USER#{user_id}", "SK": f"DOC#{document_id}"}


@dataclass(frozen=True)
class Clients:
    s3: S3Client
    dynamodb: DynamoDBClient
    sns: SNSClient
    sqs: SQSClient


def clients(settings: AwsSettings) -> Clients:
    options: Any = {
        "region_name": settings.aws_region,
        "endpoint_url": settings.aws_endpoint,
        "config": Config(retries={"mode": "standard", "max_attempts": 5}),
    }
    return Clients(
        s3=boto3.client("s3", **options),
        dynamodb=boto3.client("dynamodb", **options),
        sns=boto3.client("sns", **options),
        sqs=boto3.client("sqs", **options),
    )


class DocumentStore:
    def __init__(self, settings: AwsSettings, aws: Clients | None = None) -> None:
        self.settings = settings
        self.aws = aws or clients(settings)
        self._serializer = TypeSerializer()
        self._deserializer = TypeDeserializer()

    # S3 ---------------------------------------------------------------------------

    def download(self, key: str) -> tuple[bytes, str] | None:
        """The object's bytes and content type, or None if it no longer exists."""
        try:
            response = self.aws.s3.get_object(Bucket=self.settings.s3_bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        content_type = response.get("ContentType") or "application/octet-stream"
        return response["Body"].read(), content_type

    def exists(self, key: str) -> bool:
        try:
            self.aws.s3.head_object(Bucket=self.settings.s3_bucket, Key=key)
            return True
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return False
            raise

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.aws.s3.put_object(
            Bucket=self.settings.s3_bucket, Key=key, Body=data, ContentType=content_type
        )

    def delete(self, key: str) -> None:
        self.aws.s3.delete_object(Bucket=self.settings.s3_bucket, Key=key)

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under `prefix`; returns how many."""
        deleted = 0
        paginator = self.aws.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.settings.s3_bucket, Prefix=prefix):
            keys: list[ObjectIdentifierTypeDef] = [
                {"Key": o["Key"]} for o in page.get("Contents", [])
            ]
            if keys:
                self.aws.s3.delete_objects(
                    Bucket=self.settings.s3_bucket, Delete={"Objects": keys}
                )
                deleted += len(keys)
        return deleted

    def keys(self, prefix: str) -> list[str]:
        paginator = self.aws.s3.get_paginator("list_objects_v2")
        return [
            o["Key"]
            for page in paginator.paginate(
                Bucket=self.settings.s3_bucket, Prefix=prefix
            )
            for o in page.get("Contents", [])
        ]

    # DynamoDB ----------------------------------------------------------------------

    def get_document(self, user_id: str, document_id: str) -> dict[str, Any] | None:
        response = self.aws.dynamodb.get_item(
            TableName=self.settings.dynamodb_table,
            Key={k: {"S": v} for k, v in document_key(user_id, document_id).items()},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if item is None:
            return None
        return {k: self._deserializer.deserialize(v) for k, v in item.items()}

    def put_document(self, item: dict[str, Any]) -> None:
        self.aws.dynamodb.put_item(
            TableName=self.settings.dynamodb_table,
            Item={
                k: self._serializer.serialize(v) for k, v in dynamo_value(item).items()
            },
        )

    def update_document(
        self,
        user_id: str,
        document_id: str,
        status: DocumentStatus | None = None,
        fields: dict[str, Any] | None = None,
        remove: tuple[str, ...] = (),
    ) -> None:
        """SET the given fields (and the status, as the NestJS worker did) on an existing item."""
        timestamp = now()
        values: dict[str, Any] = {":now": timestamp}
        names: dict[str, str] = {}
        sets = ["updatedAt = :now"]
        if status is not None:
            names["#status"] = "status"
            values[":status"] = status.value
            values[":gsi1sk"] = f"STATUS#{status.value}#{timestamp}"
            sets += ["#status = :status", "GSI1SK = :gsi1sk"]
            if status in TIMESTAMPED:
                sets.append(f"{TIMESTAMPED[status]} = :now")
        for index, (name, value) in enumerate((fields or {}).items()):
            names[f"#f{index}"] = name
            values[f":f{index}"] = dynamo_value(value)
            sets.append(f"#f{index} = :f{index}")
        expression = "SET " + ", ".join(sets)
        if remove:
            for index, name in enumerate(remove):
                names[f"#r{index}"] = name
            expression += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(remove)))
        arguments: Any = {
            "TableName": self.settings.dynamodb_table,
            "Key": {k: {"S": v} for k, v in document_key(user_id, document_id).items()},
            "UpdateExpression": expression,
            "ExpressionAttributeValues": {
                k: self._serializer.serialize(v) for k, v in values.items()
            },
            "ConditionExpression": "attribute_exists(PK)",
        }
        if names:
            arguments["ExpressionAttributeNames"] = names
        self.aws.dynamodb.update_item(**arguments)

    def get_statuses(
        self, user_id: str, document_ids: list[str]
    ) -> dict[str, str | None]:
        """Status of each document, None for missing items. Batches of 100."""
        result: dict[str, str | None] = dict.fromkeys(document_ids)
        for start in range(0, len(document_ids), 100):
            keys = [
                {k: {"S": v} for k, v in document_key(user_id, d).items()}
                for d in document_ids[start : start + 100]
            ]
            pending: Any = {
                self.settings.dynamodb_table: {
                    "Keys": keys,
                    "ProjectionExpression": "SK, #s",
                    "ExpressionAttributeNames": {"#s": "status"},
                }
            }
            while pending:
                response = self.aws.dynamodb.batch_get_item(RequestItems=pending)
                for item in response["Responses"].get(self.settings.dynamodb_table, []):
                    document_id = item["SK"]["S"].removeprefix("DOC#")
                    result[document_id] = item.get("status", {}).get("S")
                pending = response.get("UnprocessedKeys") or None
        return result

    # SNS ---------------------------------------------------------------------------

    def publish(
        self,
        user_id: str,
        document_id: str,
        status: DocumentStatus,
        document: dict[str, Any] | None = None,
    ) -> None:
        """Publish a WsDocumentStatusEvent. Failures are logged, as in the NestJS worker."""
        event = WsDocumentStatusEvent(
            document_id=document_id,
            status=status,
            timestamp=datetime.now(UTC),
            document=document,
        )
        message = event.model_dump_json(exclude_none=True)
        try:
            self.aws.sns.publish(
                TopicArn=self.settings.sns_topic_arn,
                Message=message,
                MessageAttributes={
                    "eventType": {
                        "DataType": "String",
                        "StringValue": "DOCUMENT_STATUS",
                    },
                    "userId": {"DataType": "String", "StringValue": user_id},
                },
            )
        except Exception:
            logger.warning(
                "SNS publish failed for %s; processing continues",
                document_id,
                exc_info=True,
            )

    # SQS ---------------------------------------------------------------------------

    def receive(
        self, wait_seconds: int, visibility_timeout: int
    ) -> list[MessageTypeDef]:
        response = self.aws.sqs.receive_message(
            QueueUrl=self.settings.sqs_queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=wait_seconds,
            VisibilityTimeout=visibility_timeout,
            MessageSystemAttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
        )
        return response.get("Messages", [])

    def delete_message(self, receipt_handle: str) -> None:
        self.aws.sqs.delete_message(
            QueueUrl=self.settings.sqs_queue_url, ReceiptHandle=receipt_handle
        )

    def queue_depth(self) -> tuple[int, int]:
        """Messages waiting on the queue, and messages being processed."""
        attributes = self.aws.sqs.get_queue_attributes(
            QueueUrl=self.settings.sqs_queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        return (
            int(attributes.get("ApproximateNumberOfMessages", 0)),
            int(attributes.get("ApproximateNumberOfMessagesNotVisible", 0)),
        )

    def extend_visibility(self, receipt_handle: str, seconds: int) -> None:
        self.aws.sqs.change_message_visibility(
            QueueUrl=self.settings.sqs_queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=seconds,
        )
