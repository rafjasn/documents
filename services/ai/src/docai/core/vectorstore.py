"""Qdrant: the chunk collection schema, tenant-safe search, aliases.

One collection per index version (chunks_v{N}) behind the alias `chunks`. Points carry a
dense vector, a BM25 sparse vector (IDF applied by Qdrant) and the chunk's payload.
`user_id` is indexed as the tenant key. Every query and delete adds the caller's
`user_id` in code; SearchFilters has no field for it, so a filter built from model
output or user input can only narrow results within the tenant.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from qdrant_client import QdrantClient, models

from docai.core.settings import IndexSettings

DENSE, SPARSE = "dense", "sparse"
POINT_NAMESPACE = uuid.UUID("6f5b6c2e-0d6c-4c1a-9d2e-2b7a3f0e9c41")
KEYWORD_INDEXES = ("doc_id", "category", "lang", "modality", "vendor")


def qdrant_client(settings: IndexSettings) -> QdrantClient:
    key = (
        settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
    )
    return QdrantClient(url=settings.qdrant_url, api_key=key, timeout=120)


def point_id(user_id: str, doc_id: str, chunk_index: int) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, f"{user_id}/{doc_id}/{chunk_index}"))


@dataclass(frozen=True)
class SearchFilters:
    """Narrowing filters. The tenant (user_id) is never one of them."""

    doc_ids: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ()
    vendors: tuple[str, ...] = ()
    date_from: date | None = None
    date_to: date | None = None


def tenant_filter(user_id: str, filters: SearchFilters | None = None) -> models.Filter:
    if not user_id:
        raise ValueError("A tenant (user_id) is required")
    must: list[models.Condition] = [
        models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))
    ]
    f = filters or SearchFilters()
    for key, values in (
        ("doc_id", f.doc_ids),
        ("category", f.categories),
        ("lang", f.languages),
        ("modality", f.modalities),
        ("vendor", f.vendors),
    ):
        if values:
            must.append(
                models.FieldCondition(key=key, match=models.MatchAny(any=list(values)))
            )
    if f.date_from or f.date_to:
        must.append(
            models.FieldCondition(
                key="doc_date",
                range=models.DatetimeRange(
                    gte=f.date_from.isoformat() if f.date_from else None,
                    lte=f.date_to.isoformat() if f.date_to else None,
                ),
            )
        )
    return models.Filter(must=must)


def create_collection(
    client: QdrantClient, name: str, dim: int, metadata: dict[str, Any] | None = None
) -> None:
    client.create_collection(
        name,
        vectors_config={
            DENSE: models.VectorParams(size=dim, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={
            SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)
        },
        metadata=metadata,
    )
    client.create_payload_index(
        name,
        "user_id",
        models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True),
    )
    for key in KEYWORD_INDEXES:
        client.create_payload_index(name, key, models.PayloadSchemaType.KEYWORD)
    client.create_payload_index(name, "doc_date", models.PayloadSchemaType.DATETIME)


def collection_exists(client: QdrantClient, name: str) -> bool:
    return bool(client.collection_exists(name))


def aliased_collection(client: QdrantClient, alias: str) -> str | None:
    for item in client.get_aliases().aliases:
        if item.alias_name == alias:
            return str(item.collection_name)
    return None


def switch_alias(client: QdrantClient, alias: str, collection: str) -> None:
    """Point the alias at `collection` in one atomic request."""
    operations: list[models.AliasOperations] = []
    if aliased_collection(client, alias) is not None:
        operations.append(
            models.DeleteAliasOperation(
                delete_alias=models.DeleteAlias(alias_name=alias)
            )
        )
    operations.append(
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(
                collection_name=collection, alias_name=alias
            )
        )
    )
    client.update_collection_aliases(change_aliases_operations=operations)


def document_filter(user_id: str, doc_id: str) -> models.Filter:
    return tenant_filter(user_id, SearchFilters(doc_ids=(doc_id,)))


def delete_document(
    client: QdrantClient, collection: str, user_id: str, doc_id: str
) -> None:
    client.delete(
        collection,
        points_selector=models.FilterSelector(filter=document_filter(user_id, doc_id)),
        wait=True,
    )


def replace_document(
    client: QdrantClient,
    collection: str,
    user_id: str,
    doc_id: str,
    points: Sequence[models.PointStruct],
) -> None:
    """Replace a document's points: reindexing never leaves stale chunks behind."""
    delete_document(client, collection, user_id, doc_id)
    for start in range(0, len(points), 256):
        client.upsert(collection, points=list(points[start : start + 256]), wait=True)


def count_document(
    client: QdrantClient, collection: str, user_id: str, doc_id: str
) -> int:
    return int(
        client.count(collection, count_filter=document_filter(user_id, doc_id)).count
    )


def search(
    client: QdrantClient,
    collection: str,
    user_id: str,
    *,
    dense: Sequence[float] | None,
    sparse: models.SparseVector | None,
    limit: int,
    filters: SearchFilters | None = None,
    rrf_k: int = 60,
) -> list[models.ScoredPoint]:
    """Dense, sparse, or both fused with reciprocal rank fusion.

    Fusion returns every candidate of the two `limit`-long lists (up to 2 x limit), so
    the caller can break ties in a fixed order: Qdrant orders tied fused scores
    arbitrarily, which made the top `limit` differ between identical queries.
    """
    query_filter = tenant_filter(user_id, filters)
    if dense is not None and sparse is not None:
        result = client.query_points(
            collection,
            prefetch=[
                models.Prefetch(
                    query=list(dense), using=DENSE, limit=limit, filter=query_filter
                ),
                models.Prefetch(
                    query=sparse, using=SPARSE, limit=limit, filter=query_filter
                ),
            ],
            query=models.RrfQuery(rrf=models.Rrf(k=rrf_k)),
            query_filter=query_filter,
            limit=2 * limit,
            with_payload=True,
        )
    elif dense is not None:
        result = client.query_points(
            collection,
            query=list(dense),
            using=DENSE,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
    elif sparse is not None:
        result = client.query_points(
            collection,
            query=sparse,
            using=SPARSE,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
    else:
        raise ValueError("A dense or a sparse query is required")
    return list(result.points)


def payload(point: models.ScoredPoint) -> dict[str, Any]:
    return dict(point.payload or {})
