"""The user's processed documents and their extracted fields, from DynamoDB.

Read for the user in the token, one query per request, and only when a question needs
the metadata tool. The API's item layout: PK USER#{userId}, SK DOC#{documentId}.
"""

from typing import TYPE_CHECKING, Any, Protocol

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.config import Config

from docai.agent.metadata import Record, date_of
from docai.core.contracts import ChatScope
from docai.core.settings import AwsSettings

if TYPE_CHECKING:
    from types_boto3_dynamodb import DynamoDBClient


class Records(Protocol):
    """Where the metadata tool's records come from: DynamoDB, or a file in CI."""

    def load(self, user_id: str, scope: ChatScope | None = None) -> list[Record]: ...


class RecordStore:
    def __init__(
        self, settings: AwsSettings, client: DynamoDBClient | None = None
    ) -> None:
        self.table = settings.dynamodb_table
        self.client = client or boto3.client(
            "dynamodb",
            region_name=settings.aws_region,
            endpoint_url=settings.aws_endpoint,
            config=Config(retries={"mode": "standard", "max_attempts": 5}),
        )
        self.deserializer = TypeDeserializer()

    def load(self, user_id: str, scope: ChatScope | None = None) -> list[Record]:
        if not user_id:
            raise ValueError("A user is required")
        records: list[Record] = []
        start: dict[str, Any] | None = None
        while True:
            response = self.client.query(
                TableName=self.table,
                KeyConditionExpression="PK = :pk AND begins_with(SK, :doc)",
                FilterExpression="#status = :completed",
                ProjectionExpression="id, fileName, category, extracted, #lang, #status",
                ExpressionAttributeNames={"#status": "status", "#lang": "language"},
                ExpressionAttributeValues={
                    ":pk": {"S": f"USER#{user_id}"},
                    ":doc": {"S": "DOC#"},
                    ":completed": {"S": "COMPLETED"},
                },
                **({"ExclusiveStartKey": start} if start else {}),  # type: ignore[arg-type]
            )
            for raw in response.get("Items", []):
                item = {k: self.deserializer.deserialize(v) for k, v in raw.items()}
                extracted = item.get("extracted") or {}
                records.append(
                    Record(
                        doc_id=str(item["id"]),
                        title=str(item.get("fileName") or item["id"]),
                        category=str(
                            extracted.get("kind") or item.get("category") or "other"
                        ).lower(),
                        fields=extracted,
                        language=item.get("language"),
                    )
                )
            start = response.get("LastEvaluatedKey")
            if not start:
                break
        return in_scope(records, scope)


def in_scope(records: list[Record], scope: ChatScope | None) -> list[Record]:
    """The records a conversation's scope allows, filtered as the search filters its
    passages: by document, category, language and document date (issue, receipt or
    effective date; a document without one is outside any date range)."""
    if scope is None:
        return records
    if scope.document_ids:
        wanted = set(scope.document_ids)
        records = [r for r in records if r.doc_id in wanted]
    if scope.categories:
        kinds = {c.lower() for c in scope.categories}
        records = [r for r in records if r.category in kinds]
    if scope.languages:
        records = [r for r in records if r.language in scope.languages]
    if scope.date_from or scope.date_to:
        start, end = scope.date_from, scope.date_to
        records = [
            r
            for r in records
            if (when := date_of(r)) is not None
            and (start is None or when >= start)
            and (end is None or when <= end)
        ]
    return records
