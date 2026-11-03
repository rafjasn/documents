"""Stage 04 end-to-end test on the running stack: upload → index → ask → cited answer.

Uploads a synthetic invoice through the API as the frontend does, waits until it is
processed and indexed, asks about it in a conversation scoped to it and in one over the
whole library, and checks that both answers state the amount due and cite the invoice,
that every streamed event matches the contract, and that the API stored the answers
with their citations. Deletes the invoice at the end.

From services/ai, with the stack up (docker compose up):
    uv run --env-file ../../.env python evals/chat_e2e.py
"""

import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import jsonschema

from docai.evals.scoring import numbers_in

API = os.environ.get("API_URL", "http://localhost")
EMAIL = os.environ.get("FLOW_CHECK_EMAIL", "flow-check@documents.local")
PASSWORD = os.environ.get("FLOW_CHECK_PASSWORD", "flow-check-password")
ROOT = Path(__file__).resolve().parents[1]
INVOICE = ROOT / "corpus" / "data" / "synthetic" / "w3" / "syn-w3-inv-2-q2.pdf"
AMOUNT_DUE = Decimal("1665.40")  # INV-260306, from the golden set
SCHEMA = json.loads(
    (
        ROOT.parents[1]
        / "packages"
        / "shared"
        / "contracts"
        / "chat-stream-event.schema.json"
    ).read_text()
)


def login(api: httpx.Client) -> str:
    credentials = {"email": EMAIL, "password": PASSWORD}
    response = api.post("/auth/login", json=credentials)
    if response.status_code >= 400:
        api.post("/auth/register", json=credentials)
        response = api.post("/auth/login", json=credentials)
    response.raise_for_status()
    return str(response.json()["accessToken"])


def upload(api: httpx.Client) -> str:
    data = INVOICE.read_bytes()
    presign = api.post(
        "/documents/presign",
        json={
            "fileName": INVOICE.name,
            "mimeType": "application/pdf",
            "fileSize": len(data),
        },
    )
    presign.raise_for_status()
    body = presign.json()
    httpx.post(
        body["uploadUrl"],
        data=body["fields"],
        files={"file": (INVOICE.name, data, "application/pdf")},
    ).raise_for_status()
    document = str(body["documentId"])
    deadline = time.monotonic() + 600
    while (status := api.get(f"/documents/{document}").json()["status"]) not in (
        "COMPLETED",
        "FAILED",
    ):
        if time.monotonic() > deadline:
            raise TimeoutError("the upload wasn't processed in 10 minutes")
        time.sleep(2)
    if status != "COMPLETED":
        raise RuntimeError(f"processing ended {status}")
    return document


def ask(api: httpx.Client, conversation: str, question: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    started = time.monotonic()
    first_token = None
    with api.stream(
        "POST", f"/ai/conversations/{conversation}/stream", json={"message": question}
    ) as response:
        response.raise_for_status()
        buffer = ""
        for text in response.iter_text():
            buffer += text
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                data = "\n".join(
                    line[6:] for line in block.splitlines() if line.startswith("data: ")
                )
                if data:
                    event = json.loads(data)
                    jsonschema.validate(event, SCHEMA)
                    if event["type"] == "token" and first_token is None:
                        first_token = time.monotonic() - started
                    events.append(event)
    total = time.monotonic() - started
    print(
        f"  {len(events)} events, first token after {first_token or 0:.1f}s, done after {total:.1f}s"
    )
    return events


def check(events: list[dict[str, Any]], document: str) -> str:
    kinds = [e["type"] for e in events]
    assert "error" not in kinds, events[-1]
    answer = "".join(e["text"] for e in events if e["type"] == "token")
    [citations] = [e["citations"] for e in events if e["type"] == "citations"]
    [done] = [e for e in events if e["type"] == "done"]
    assert any(abs(v - AMOUNT_DUE) < Decimal("0.005") for v in numbers_in(answer)), (
        answer
    )
    assert any(c["docId"] == document for c in citations), citations
    print(f"  {done['intent']}: {answer!r}")
    print(f"  cites {[(c['citationId'], c['title'], c['page']) for c in citations]}")
    print(
        f"  usage {done['usage']}, steps {[e['node'] for e in events if e['type'] == 'step']}"
    )
    return answer


def main() -> int:
    api = httpx.Client(base_url=f"{API}/api", timeout=120)
    api.headers["Authorization"] = f"Bearer {login(api)}"
    print(f"Uploading {INVOICE.name}")
    document = upload(api)
    print(f"Processed and indexed as {document}")
    try:
        scoped = api.post(
            "/ai/conversations", json={"scope": {"documentIds": [document]}}
        ).json()
        print("Asking in a conversation scoped to the invoice")
        answer = check(
            ask(api, scoped["id"], "What is the amount due on this invoice?"), document
        )
        stored = api.get(f"/ai/conversations/{scoped['id']}/messages").json()
        assert [m["role"] for m in stored] == ["user", "assistant"], stored
        assert stored[1]["content"] == answer and stored[1]["citations"], stored[1]
        print("  stored with its citations")

        library = api.post("/ai/conversations", json={}).json()
        print("Asking across the whole library")
        check(
            ask(
                api,
                library["id"],
                "How much is due on invoice INV-260306 from Clark Services Ltd?",
            ),
            document,
        )
    finally:
        api.delete(f"/documents/{document}").raise_for_status()
        print(f"Deleted {document}")
    print("End-to-end test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
