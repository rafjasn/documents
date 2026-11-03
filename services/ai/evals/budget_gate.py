"""Stage 10b gate: the gateway's budgets hold in chat and at upload, on the running stack.

    users   Two accounts. A gets a daily budget smaller than one call (its own budget
            in the gateway, like the default tier but tiny), spent by one call made as
            A; B keeps the default. A is refused in chat with the error event naming the
            daily limit, and A's upload fails with the reason and the reset time,
            without a retry. B asks and uploads.
    global  Run with the gateway's global budget below the day's spend
            (LLM_GATEWAY_DAILY_BUDGET=0.000001): B, under its own budget, is refused.
    db      Run with the gateway's database just stopped: a call with the services' key
            is refused rather than served without a budget check, at once and after the
            gateway's cache of the key expires.

Uploads are new PDFs (a unique invoice number), so ingestion calls the model: a file in
the ingest cache makes no call and would never meet the budget. Each question and
upload costs well under a cent.

From services/ai, with the stack and the ops profile up, and the services pointed at the
gateway (LLM_GATEWAY_URL=http://localhost:4000 docker compose --profile ops up -d):
    uv run --env-file ../../.env python evals/budget_gate.py users
"""

import base64
import io
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from chat_e2e import ask  # evals/chat_e2e.py: streams a question, checks the events
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from docai.core.gateway import USER_BUDGET_ID
from docai.core.tracing import user_hash

API = os.environ.get("API_URL", "http://localhost")
GATEWAY = "http://localhost:4000"
ACCOUNTS = {
    "A": ("budget-a@documents.local", "budget-a-password"),
    "B": ("budget-b@documents.local", "budget-b-password"),
}
TINY = "budget-gate-tiny"  # a daily budget smaller than one question
RUNS = Path(__file__).parent / "runs"
QUESTION = "What is the total amount of my invoices?"


def session(account: str) -> tuple[httpx.Client, str]:
    """An API client logged in as the account, and the account's user ID."""
    email, password = ACCOUNTS[account]
    api = httpx.Client(base_url=f"{API}/api", timeout=180)
    credentials = {"email": email, "password": password}
    response = api.post("/auth/login", json=credentials)
    if response.status_code >= 400:
        api.post("/auth/register", json=credentials)
        response = api.post("/auth/login", json=credentials)
    response.raise_for_status()
    token = str(response.json()["accessToken"])
    api.headers["Authorization"] = f"Bearer {token}"
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    return api, str(claims["sub"])


def admin() -> httpx.Client:
    key = os.environ["LITELLM_MASTER_KEY"]
    return httpx.Client(
        base_url=GATEWAY, headers={"Authorization": f"Bearer {key}"}, timeout=30
    )


def give_tiny_budget(user_id: str) -> None:
    """The user's own daily budget, smaller than one question."""
    with admin() as gateway:
        budget = {"budget_id": TINY, "max_budget": 0.000001, "budget_duration": "1d"}
        if gateway.post("/budget/new", json=budget).status_code >= 400:
            gateway.post("/budget/update", json=budget).raise_for_status()
        customer = {"user_id": user_hash(user_id), "budget_id": TINY}
        if gateway.post("/customer/new", json=customer).status_code >= 400:
            gateway.post("/customer/update", json=customer).raise_for_status()


def spend_as(user_id: str) -> None:
    """One small call through the gateway, counted against the user's budget."""
    completion(user_hash(user_id)).raise_for_status()


def completion(end_user: str) -> httpx.Response:
    return httpx.post(
        f"{GATEWAY}/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['LLM_GATEWAY_KEY']}"},
        json={
            "model": "gemini-3.1-flash-lite",
            "messages": [{"role": "user", "content": "Say OK."}],
            "max_tokens": 10,
            "user": end_user,
        },
        timeout=120,
    )


def customer(user_id: str) -> dict[str, Any]:
    with admin() as gateway:
        found = gateway.get(
            "/customer/info", params={"end_user_id": user_hash(user_id)}
        )
        return dict(found.json()) if found.status_code == 200 else {}


def invoice_pdf(number: str) -> bytes:
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4, invariant=True)
    for row, line in enumerate(
        [
            f"FAKTURA VAT nr {number}",
            "Data wystawienia: 2026-10-22",
            "Sprzedawca: Budżet Test Sp. z o.o.",
            "Nabywca: Odbiorca Testowy S.A.",
            "Usługa konsultingowa, netto 1 000,00 PLN, VAT 23%",
            "Razem brutto: 1 230,00 PLN",
        ]
    ):
        page.drawString(72, 760 - row * 20, line)
    page.save()
    return buffer.getvalue()


def upload(api: httpx.Client, number: str) -> dict[str, Any]:
    """Upload a new invoice as the frontend does; the document once processing ends."""
    data, name = invoice_pdf(number), f"{number.replace('/', '-')}.pdf"
    presign = api.post(
        "/documents/presign",
        json={"fileName": name, "mimeType": "application/pdf", "fileSize": len(data)},
    )
    presign.raise_for_status()
    body = presign.json()
    httpx.post(
        body["uploadUrl"],
        data=body["fields"],
        files={"file": (name, data, "application/pdf")},
    ).raise_for_status()
    deadline = time.monotonic() + 300
    while True:
        document = dict(api.get(f"/documents/{body['documentId']}").json())
        if document["status"] in ("COMPLETED", "FAILED"):
            return document
        if time.monotonic() > deadline:
            raise TimeoutError("the upload wasn't processed in 5 minutes")
        time.sleep(2)


def outcome(events: list[dict[str, Any]]) -> str:
    error = next((e["message"] for e in events if e["type"] == "error"), None)
    if error:
        return f"error: {error}"
    return (
        "answered: " + "".join(e["text"] for e in events if e["type"] == "token")[:80]
    )


def chat(api: httpx.Client, label: str) -> str:
    conversation = api.post("/ai/conversations", json={}).json()["id"]
    print(f"{label} asks")
    result = outcome(ask(api, conversation, QUESTION))
    print(f"  {result}")
    return result


def users(stamp: str) -> dict[str, Any]:
    a, a_id = session("A")
    b, b_id = session("B")
    give_tiny_budget(a_id)
    spend_as(a_id)  # served: the spend starts at zero
    time.sleep(5)  # the gateway counts the spend after the reply
    results: dict[str, Any] = {"A": {}, "B": {}}
    results["A"]["question"] = chat(a, "A (over budget)")
    document = upload(a, f"BG/{stamp}/A")
    results["A"]["upload"] = {k: document.get(k) for k in ("status", "errorMessage")}
    print(f"A uploads: {results['A']['upload']}")
    results["B"]["question"] = chat(b, "B")
    document = upload(b, f"BG/{stamp}/B")
    results["B"]["upload"] = {"status": document.get("status")}
    print(f"B uploads: {results['B']['upload']}")
    for name, user_id in (("A", a_id), ("B", b_id)):
        found = customer(user_id)
        results[name]["gateway"] = {
            "spend": found.get("spend"),
            "budget": (found.get("litellm_budget_table") or {}).get("budget_id")
            or USER_BUDGET_ID,
        }
    passed = (
        "daily limit on your AI usage" in results["A"]["question"]
        and results["A"]["upload"]["status"] == "FAILED"
        and "resets at" in str(results["A"]["upload"]["errorMessage"])
        and results["B"]["question"].startswith("answered")
        and results["B"]["upload"]["status"] == "COMPLETED"
    )
    return {"results": results, "passed": passed}


def global_budget(stamp: str) -> dict[str, Any]:
    b, _ = session("B")
    asked = chat(b, "B (global budget spent)")
    document = upload(b, f"BG/{stamp}/G")
    uploaded = {k: document.get(k) for k in ("status", "errorMessage")}
    print(f"B uploads: {uploaded}")
    passed = (
        "daily limit on the service's AI usage" in asked
        and uploaded["status"] == "FAILED"
    )
    return {"results": {"question": asked, "upload": uploaded}, "passed": passed}


def database_down(stamp: str) -> dict[str, Any]:
    """Right after the database stops, and again once the gateway's cache of the key
    has expired."""
    attempts = []
    for wait in (0, 10):  # the key cache lasts 5 s (user_api_key_cache_ttl)
        time.sleep(wait)
        response = completion("budget-gate-db")
        body = response.text[:300]
        print(f"after {wait:>2}s: HTTP {response.status_code} {body}")
        attempts.append(
            {"after_seconds": wait, "status": response.status_code, "body": body}
        )
    return {
        "results": attempts,
        # Within the cache period a call may still be served on the cached key.
        "passed": attempts[-1]["status"] >= 400,
    }


def main() -> int:
    check = sys.argv[1] if len(sys.argv) > 1 else "users"
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    run = {"users": users, "global": global_budget, "db": database_down}[check](stamp)
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"{stamp}-budget-gate-{check}.json"
    record = {"kind": "budget-gate", "check": check, "created": stamp, **run}
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(f"{'PASS' if run['passed'] else 'FAIL'}; saved {path}")
    return 0 if run["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
