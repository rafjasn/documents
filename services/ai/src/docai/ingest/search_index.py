"""The live search index: versioned collections behind an alias.

Each version is a Qdrant collection chunks_v{N}; the alias (QDRANT_ALIAS, `chunks`)
points at the live one. The registry is the collections themselves: each records, in
its Qdrant metadata, the IndexConfig that built it and its status, so the record can't
drift from the data it describes.

- building: `docai index build` is filling it from the stored parsed/ artifacts
- ready: built, not live yet
- live: behind the alias
- retired: replaced by a newer live version; kept until `docai index drop`

The worker indexes new documents into the live version and into any building or ready
version, so a version being built doesn't miss uploads; the build's catch-up pass adds
whatever the worker couldn't (for example when TEI serves a different embedding model).
Deletions remove a document from every version. Switching moves the alias in one
atomic request, so queries never see a missing or half-built collection.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from qdrant_client import QdrantClient, models

from docai.core.embeddings import MODELS, Embedder, TeiEmbedder
from docai.core.index_config import IndexConfig
from docai.core.settings import IndexSettings
from docai.core.vectorstore import (
    aliased_collection,
    collection_exists,
    create_collection,
    delete_document,
    qdrant_client,
    replace_document,
    switch_alias,
)
from docai.ingest.indexer import build_points
from docai.ingest.models import Models
from docai.ingest.pipeline import ParsedDocument
from docai.ingest.store import DocumentStore, now

logger = logging.getLogger(__name__)

PREFIX = "chunks_v"
# The default for a fresh stack, chosen from the Stage 03 evaluation (docs/RESULTS.md):
# structure-aware chunks with Gemini-written context, embedded with multilingual-e5-small,
# and BM25 over whole words (prefix stemming helped only without the context).
DEFAULT_INDEX = IndexConfig(chunker="structure", context="llm", embedder="e5-small")
WRITABLE = ("building", "ready")  # written by the worker besides the live version


def collection_for(version: int) -> str:
    return f"{PREFIX}{version}"


def version_of(collection: str) -> int:
    return int(collection.removeprefix(PREFIX))


def parsed_key(user_id: str, doc_id: str) -> str:
    return f"parsed/{user_id}/{doc_id}/document.json"


@dataclass(frozen=True)
class IndexVersion:
    version: int
    config: IndexConfig
    status: str
    metadata: dict[str, Any]

    @property
    def collection(self) -> str:
        return collection_for(self.version)


class IndexRegistry:
    """Index versions, read from and recorded in their collections' metadata."""

    def __init__(self, client: QdrantClient) -> None:
        self.client = client

    def get(self, version: int) -> IndexVersion | None:
        name = collection_for(version)
        if not collection_exists(self.client, name):
            return None
        metadata = dict(self.client.get_collection(name).config.metadata or {})
        if "config" not in metadata:
            return None  # not created by this module
        return IndexVersion(
            version,
            IndexConfig.model_validate(metadata["config"]),
            str(metadata.get("status", "building")),
            metadata,
        )

    def versions(self) -> list[IndexVersion]:
        names = [
            c.name
            for c in self.client.get_collections().collections
            if c.name.startswith(PREFIX) and c.name.removeprefix(PREFIX).isdigit()
        ]
        found = [self.get(version_of(name)) for name in names]
        return sorted((v for v in found if v), key=lambda v: v.version)

    def create(self, version: int, config: IndexConfig, status: str) -> IndexVersion:
        if collection_exists(self.client, collection_for(version)):
            raise ValueError(f"Index version {version} already exists")
        metadata = {"config": config.model_dump(), "status": status, "createdAt": now()}
        create_collection(
            self.client,
            collection_for(version),
            MODELS[config.embedder].dim,
            metadata=metadata,
        )
        return IndexVersion(version, config, status, metadata)

    def update(self, version: int, **fields: Any) -> None:
        """Merge fields into the version's metadata."""
        self.client.update_collection(collection_for(version), metadata=fields)


@dataclass
class BuildStats:
    version: int
    documents: int = 0
    chunks: int = 0
    skipped: list[str] = field(default_factory=list)  # deleted or without an item
    caught_up: int = 0  # documents found by the second pass
    removed: int = 0  # documents deleted while the build ran
    seconds: float = 0.0


class SearchIndex:
    """What the worker indexes into and deletes from, and what `docai index` manages."""

    def __init__(
        self,
        settings: IndexSettings,
        store: DocumentStore,
        models_: Models | None,
        context_model: str | None,
        client: QdrantClient | None = None,
        tei_embed_url: str | None = None,
    ) -> None:
        self.settings, self.store = settings, store
        self.models, self.context_model = models_, context_model
        self.client = client or qdrant_client(settings)
        self.registry = IndexRegistry(self.client)
        self.tei_embed_url = tei_embed_url or settings.tei_embed_url
        self._embedders: dict[str, Embedder] = {}

    def embedder(self, key: str) -> Embedder:
        """A TEI embedder; raises if the TEI server serves a different model."""
        if key not in self._embedders:
            model = MODELS[key]
            if model.provider != "tei":
                raise ValueError(f"Indexing embeds with TEI models only, not {key}")
            self._embedders[key] = TeiEmbedder(self.tei_embed_url, model)
        return self._embedders[key]

    # Versions -------------------------------------------------------------------------

    def live(self) -> IndexVersion:
        """The live version; a fresh stack gets version 1 with DEFAULT_INDEX."""
        collection = aliased_collection(self.client, self.settings.qdrant_alias)
        if collection is None:
            if self.registry.versions():
                raise RuntimeError(
                    f"No collection behind alias {self.settings.qdrant_alias};"
                    " run `docai index switch --version N`"
                )
            self.registry.create(1, DEFAULT_INDEX, "ready")
            self.switch(1)
            logger.info(
                "Created index version 1 behind alias %s", self.settings.qdrant_alias
            )
            collection = collection_for(1)
        version = self.registry.get(version_of(collection))
        if version is None:
            raise RuntimeError(f"{collection} has no index config in its metadata")
        return version

    def targets(self) -> list[IndexVersion]:
        """Where a new document goes: the live version first, then versions in progress."""
        live = self.live()
        return [live] + [
            v
            for v in self.registry.versions()
            if v.version != live.version and v.status in WRITABLE
        ]

    def switch(self, version: int) -> int | None:
        """Point the alias at a built version; the previous live version is retired."""
        target = self.registry.get(version)
        if target is None:
            raise LookupError(f"Index version {version} doesn't exist")
        if target.status == "building":
            raise ValueError(f"Index version {version} is still building")
        previous = aliased_collection(self.client, self.settings.qdrant_alias)
        switch_alias(self.client, self.settings.qdrant_alias, target.collection)
        self.registry.update(version, status="live", liveSince=now())
        old = version_of(previous) if previous else None
        if old is not None and old != version and self.registry.get(old) is not None:
            self.registry.update(old, status="retired", retiredAt=now())
        return old

    def drop(self, version: int) -> None:
        """Delete a version that isn't live."""
        if aliased_collection(self.client, self.settings.qdrant_alias) == (
            collection_for(version)
        ):
            raise ValueError(f"Index version {version} is live; switch away first")
        if self.registry.get(version) is None:
            raise LookupError(f"Index version {version} doesn't exist")
        self.client.delete_collection(collection_for(version))

    def counts(self, version: int) -> tuple[int, int]:
        """Points and documents in a version."""
        points = int(self.client.count(collection_for(version)).count)
        return points, len(self.indexed_documents(version))

    # Documents ------------------------------------------------------------------------

    def index(
        self,
        parsed: ParsedDocument,
        user_id: str,
        doc_id: str,
        title: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Index a document into the live version and any version in progress; returns
        the item's `index` attribute, which describes the live version."""
        [live, *others] = self.targets()
        result = self.index_into(live, parsed, user_id, doc_id, title, metadata)
        for other in others:
            try:
                self.index_into(other, parsed, user_id, doc_id, title, metadata)
            except Exception:
                # The build's catch-up pass indexes what the worker couldn't.
                logger.warning(
                    "Could not index %s into version %d",
                    doc_id,
                    other.version,
                    exc_info=True,
                )
        return result

    def index_into(
        self,
        target: IndexVersion,
        parsed: ParsedDocument,
        user_id: str,
        doc_id: str,
        title: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        points, _ = build_points(
            parsed,
            user_id=user_id,
            doc_id=doc_id,
            config=target.config,
            embedder=self.embedder(target.config.embedder),
            title=title,
            models_=self.models,
            context_model=self.context_model,
            metadata=metadata,
        )
        replace_document(self.client, target.collection, user_id, doc_id, points)
        return {
            "collection": target.collection,
            "version": target.version,
            "chunkCount": len(points),
            "embeddingModel": MODELS[target.config.embedder].model_id,
            "indexedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    def delete(self, user_id: str, doc_id: str) -> int:
        """Remove the document from every version; returns how many were checked."""
        versions = self.registry.versions()
        for version in versions:
            delete_document(self.client, version.collection, user_id, doc_id)
        return len(versions)

    # Rebuilding from parsed/ ----------------------------------------------------------

    def stored_documents(self) -> set[tuple[str, str]]:
        """(user_id, doc_id) of every document with a parsed/ artifact."""
        found = set()
        for key in self.store.keys("parsed/"):
            parts = key.split("/")
            if len(parts) == 4 and parts[3] == "document.json":
                found.add((parts[1], parts[2]))
        return found

    def indexed_documents(self, version: int) -> set[tuple[str, str]]:
        """(user_id, doc_id) of every document with points in the version."""
        found: set[tuple[str, str]] = set()
        offset: Any = None
        while True:
            points, offset = self.client.scroll(
                collection_for(version),
                limit=1000,
                offset=offset,
                with_payload=models.PayloadSelectorInclude(
                    include=["user_id", "doc_id"]
                ),
                with_vectors=False,
            )
            found |= {
                (str(p.payload["user_id"]), str(p.payload["doc_id"]))
                for p in points
                if p.payload
            }
            if offset is None:
                return found

    def rebuild_document(
        self, target: IndexVersion, user_id: str, doc_id: str
    ) -> int | None:
        """Index one stored document; None (and no points) if it was deleted."""
        item = self.store.get_document(user_id, doc_id)
        downloaded = self.store.download(parsed_key(user_id, doc_id))
        if item is None or item.get("status") == "DELETED" or downloaded is None:
            delete_document(self.client, target.collection, user_id, doc_id)
            return None
        parsed = ParsedDocument.model_validate_json(downloaded[0])
        title = str(item.get("fileName") or parsed.file_name)
        metadata = {
            "document_id": doc_id,
            "langfuse_session_id": f"reindex-v{target.version}",
            "langfuse_tags": ["reindex"],
        }
        info = self.index_into(target, parsed, user_id, doc_id, title, metadata)
        return int(info["chunkCount"])

    def build(
        self,
        version: int,
        config: IndexConfig,
        progress: Callable[[int, int], None] | None = None,
    ) -> BuildStats:
        """Fill a new version from parsed/, then catch up on uploads and deletions that
        happened meanwhile. The version is ready, not live, when this returns."""
        started = time.monotonic()
        target = self.registry.create(version, config, "building")
        stats = BuildStats(version)
        first = sorted(self.stored_documents())
        for n, (user_id, doc_id) in enumerate(first, 1):
            chunks = self.rebuild_document(target, user_id, doc_id)
            if chunks is None:
                stats.skipped.append(doc_id)
            else:
                stats.documents += 1
                stats.chunks += chunks
            if progress:
                progress(n, len(first))
        # Catch-up: uploads the worker couldn't add, and documents deleted meanwhile.
        stored = self.stored_documents()
        for user_id, doc_id in sorted(stored - set(first)):
            if self.rebuild_document(target, user_id, doc_id) is not None:
                stats.caught_up += 1
        for user_id, doc_id in self.indexed_documents(version) - stored:
            delete_document(self.client, target.collection, user_id, doc_id)
            stats.removed += 1
        stats.seconds = round(time.monotonic() - started, 1)
        self.registry.update(
            version, status="ready", builtAt=now(), buildSeconds=stats.seconds
        )
        return stats
