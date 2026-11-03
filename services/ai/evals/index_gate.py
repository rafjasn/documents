"""Stage 03 gate checks on the running stack (`docker compose up`, corpus seeded).

deletion: uploads a file through the API as the frontend does, waits until it is
indexed, deletes it through the API, and checks that its points, parsed/ artifacts and
thumbnail are gone.

reindex: queries the live alias from 4 threads, nonstop, while `docai index build
--switch` builds a new version from parsed/ and makes it live, `docai index switch`
switches back, and `docai index drop` deletes the new version. Every query must succeed
and return hits.

From services/ai:
    uv run --env-file ../../.env python evals/index_gate.py deletion <file>
    uv run --env-file ../../.env python evals/index_gate.py reindex --chunk-tokens 300
"""

import argparse
import mimetypes
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from docai.core.embeddings import MODELS, TeiEmbedder
from docai.core.retrieval import RetrievalConfig, Retriever
from docai.core.settings import AwsSettings, IndexSettings
from docai.core.vectorstore import aliased_collection, count_document
from docai.evals.golden import read_items
from docai.ingest.search_index import SearchIndex
from docai.ingest.store import DocumentStore

API = os.environ.get("API_URL", "http://localhost")
EMAIL = os.environ.get("FLOW_CHECK_EMAIL", "flow-check@documents.local")
PASSWORD = os.environ.get("FLOW_CHECK_PASSWORD", "flow-check-password")
TENANT = "corpus"


def wait_for(check: Callable[[], bool], seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError(f"Timed out waiting for {what}")
        time.sleep(2)


def deletion(path: Path, index: SearchIndex, store: DocumentStore) -> int:
    api = httpx.Client(base_url=f"{API}/api", timeout=60)
    credentials = {"email": EMAIL, "password": PASSWORD}
    login = api.post("/auth/login", json=credentials)
    if login.status_code >= 400:
        api.post("/auth/register", json=credentials)
        login = api.post("/auth/login", json=credentials)
    login.raise_for_status()
    token, user = login.json()["accessToken"], login.json()["userId"]
    api.headers["Authorization"] = f"Bearer {token}"

    data = path.read_bytes()
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    presign = api.post(
        "/documents/presign",
        json={"fileName": path.name, "mimeType": mime_type, "fileSize": len(data)},
    )
    presign.raise_for_status()
    document, url, fields = (
        presign.json()[k] for k in ("documentId", "uploadUrl", "fields")
    )
    httpx.post(
        url, data=fields, files={"file": (path.name, data, mime_type)}
    ).raise_for_status()
    print(f"Uploaded {path.name} as {document} for {user}")

    def status() -> str:
        return str(api.get(f"/documents/{document}").json().get("status"))

    wait_for(lambda: status() in ("COMPLETED", "FAILED"), 600, "processing")
    alias = index.settings.qdrant_alias
    points = count_document(index.client, alias, user, document)
    parsed = store.keys(f"parsed/{user}/{document}/")
    print(
        f"{status()}: {points} points in {aliased_collection(index.client, alias)}, {len(parsed)} parsed/ objects"
    )
    if status() != "COMPLETED" or not points or not parsed:
        return 1

    started = time.monotonic()
    api.delete(f"/documents/{document}").raise_for_status()

    def gone() -> bool:
        return (
            count_document(index.client, alias, user, document) == 0
            and not store.keys(f"parsed/{user}/{document}/")
            and not store.keys(f"thumbnails/{user}/{document}")
        )

    wait_for(gone, 120, "the worker to remove the document")
    print(
        f"Deleted through the API: 0 points, 0 parsed/ objects, no thumbnail"
        f" after {time.monotonic() - started:.1f}s; item status {status()}"
    )
    return 0


def reindex(build_args: list[str], index: SearchIndex) -> int:
    live = index.live()
    questions = [i.question for i in read_items() if i.split == "dev"]
    retriever = Retriever(
        index.client,
        index.settings.qdrant_alias,
        TeiEmbedder(index.settings.tei_embed_url, MODELS[live.config.embedder]),
        live.config.sparse,
    )
    errors: list[str] = []
    empty, answered = 0, 0
    stop = threading.Event()
    lock = threading.Lock()

    def query(offset: int) -> None:
        nonlocal empty, answered
        n = offset
        while not stop.is_set():
            question = questions[n % len(questions)]
            n += 1
            try:
                hits = retriever.search(question, TENANT, RetrievalConfig(sparse=True))
                with lock:
                    answered += 1
                    empty += not hits
            except Exception as error:  # every failure counts
                with lock:
                    errors.append(repr(error))

    seen: list[str] = []

    def watch_alias() -> None:
        while not stop.is_set():
            current = aliased_collection(index.client, index.settings.qdrant_alias)
            if current and (not seen or seen[-1] != current):
                seen.append(current)
            time.sleep(0.2)

    threads = [threading.Thread(target=query, args=(i * 17,)) for i in range(4)]
    threads.append(threading.Thread(target=watch_alias))
    for thread in threads:
        thread.start()
    started = time.monotonic()
    new = max(v.version for v in index.registry.versions()) + 1
    docai = [sys.executable, "-m", "docai.cli", "index"]
    steps = [
        [*docai, "build", "--version", str(new), *build_args, "--switch"],
        [*docai, "switch", "--version", str(live.version)],
        [*docai, "drop", "--version", str(new)],
    ]
    try:
        for step in steps:
            print("$ docai " + " ".join(step[3:]), flush=True)
            subprocess.run(step, check=True)
            time.sleep(3)  # queries against the alias's new target
    finally:
        stop.set()
        for thread in threads:
            thread.join()
    seconds = time.monotonic() - started
    print(
        f"{answered} queries in {seconds:.0f}s from 4 threads: {len(errors)} errors,"
        f" {empty} empty; alias: {' -> '.join(seen)}"
    )
    for error in errors[:5]:
        print(f"  {error}")
    return 1 if errors or empty or len(seen) < 3 else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="check", required=True)
    commands.add_parser("deletion").add_argument("file", type=Path)
    commands.add_parser("reindex")
    args, build_args = parser.parse_known_args()
    index = SearchIndex(IndexSettings(), DocumentStore(AwsSettings()), None, None)
    if args.check == "deletion":
        return deletion(args.file, index, DocumentStore(AwsSettings()))
    return reindex(build_args, index)


if __name__ == "__main__":
    sys.exit(main())
