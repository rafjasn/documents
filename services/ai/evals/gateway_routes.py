"""Stage 10b route test: LiteLLM's two routes to Gemini, before building on either.

- router: the OpenAI-compatible /v1/chat/completions, through `docai.core.gateway`
  (requests translated; fallbacks, retries and per-user budgets apply).
- pass-through: /gemini/..., Gemini's native API forwarded as it is, through
  LangChain's Gemini client pointed at the gateway.

Each route is tried, next to a direct Gemini call as the reference, for what the
services need: structured output (the router's schema and the extraction's), streaming
with usage, cached-token counts, low media resolution for figures, the end user that
per-user budgets key on (recorded spend and a refusal over budget), and, with
`--drill` and the gateway on its drill config (the primary always rate-limited), the
fallback.

About 30 small calls to gemini-3.1-flash-lite (under $0.02). From services/ai, with the
gateway up (docker compose --profile ops up -d) and set up (docai gateway setup):
    uv run --env-file ../../.env python evals/gateway_routes.py
    LLM_GATEWAY_CONFIG=drill-primary-down.yaml docker compose --profile ops up -d llm-gateway
    uv run --env-file ../../.env python evals/gateway_routes.py --drill
"""

import base64
import io
import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from PIL import Image, ImageDraw
from pydantic import BaseModel

from docai.agent.prompt_store import render
from docai.agent.prompts import ROUTE, Route
from docai.core.gateway import budget_refusal, gateway_chat
from docai.core.llm import chat_model
from docai.core.prices import cost
from docai.core.settings import GatewaySettings, Settings
from docai.ingest.extract import LlmExtraction, extraction_messages

URL = "http://localhost:4000"
MODEL = "google_genai:gemini-3.1-flash-lite"
FALLBACK = "gemini-3.5-flash-lite"
RUNS = Path(__file__).parent / "runs"
ROUTES = ("direct", "router", "pass-through")

INVOICE = """FAKTURA VAT nr FV/2026/10/017
Data wystawienia: 2026-10-05, termin płatności: 2026-10-19
Sprzedawca: Kowalczyk Instalacje Sp. z o.o., ul. Polna 4, 61-001 Poznań, NIP 778-14-62-019
Nabywca: Zielony Ogród S.A., ul. Leśna 12, 00-950 Warszawa, NIP 525-23-44-078
1. Montaż instalacji grzewczej, 1 szt., netto 12 400,00 PLN, VAT 23%
2. Serwis kotła, 2 szt., netto 450,00 PLN, VAT 23%
Razem netto: 13 300,00 PLN, VAT: 3 059,00 PLN, brutto: 16 359,00 PLN"""


def long_contract() -> str:
    """About 7,000 tokens of contract clauses: above Gemini's minimum for its implicit
    prefix cache."""
    clauses = [
        f"{n}. The Supplier shall deliver batch {n} of the goods described in Annex "
        f"{n % 7 + 1} within {10 + n % 20} business days of the Buyer's order, at the "
        f"unit price of {100 + n * 3},{n % 100:02d} EUR, and shall bear the risk of loss "
        f"until delivery to the warehouse at Dock {n % 12 + 1}. Late delivery incurs a "
        f"penalty of {n % 5 + 1} per cent of the batch value per week, capped at "
        f"{n % 10 + 10} per cent."
        for n in range(1, 121)
    ]
    return "SUPPLY AGREEMENT\n\n" + "\n".join(clauses)


def figure_png() -> bytes:
    """A small figure, like a stamp or a fee table: low media resolution applies to it."""
    image = Image.new("RGB", (640, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 620, 380), outline="navy", width=6)
    for row, text in enumerate(
        ["ZAPŁACONO", "2026-10-07", "Kowalczyk Instalacje", "16 359,00 PLN"]
    ):
        draw.text((60, 60 + row * 70), text, fill="navy")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@dataclass
class Check:
    route: str
    check: str
    ok: bool | None  # None: not available on this route
    detail: str


@dataclass
class Clients:
    settings: Settings
    key: str
    master: str

    def model(self, route: str, end_user: str | None = None, **kwargs: Any) -> Any:
        if route == "direct":
            return chat_model(
                MODEL, self.settings, direct=True, max_retries=1, **kwargs
            )
        if route == "router":
            model = gateway_chat(MODEL, URL, self.key, **kwargs)
            return model.model_copy(update={"end_user": end_user})
        headers = {"x-litellm-end-user-id": end_user} if end_user else {}
        return ChatGoogleGenerativeAI(
            model=MODEL.partition(":")[2],
            api_key=self.key,
            base_url=f"{URL}/gemini",
            additional_headers=headers,
            max_retries=1,  # LangChain's Gemini client: 1 means no retries
            **kwargs,
        )

    def customer(self, end_user: str) -> dict[str, Any] | None:
        response = httpx.get(
            f"{URL}/customer/info",
            params={"end_user_id": end_user},
            headers={"Authorization": f"Bearer {self.master}"},
            timeout=10,
        )
        return response.json() if response.status_code == 200 else None

    def new_customer(self, end_user: str, max_budget: float) -> None:
        httpx.post(
            f"{URL}/customer/new",
            json={"user_id": end_user, "max_budget": max_budget},
            headers={"Authorization": f"Bearer {self.master}"},
            timeout=10,
        ).raise_for_status()


def usage(message: Any) -> dict[str, Any]:
    found = dict(getattr(message, "usage_metadata", None) or {})
    return {
        "model": (getattr(message, "response_metadata", None) or {}).get("model_name"),
        "input": found.get("input_tokens"),
        "output": found.get("output_tokens"),
        "cache_read": (found.get("input_token_details") or {}).get("cache_read"),
        "reasoning": (found.get("output_token_details") or {}).get("reasoning"),
    }


def attempt(route: str, check: str, run: Callable[[], Check]) -> Check:
    try:
        return run()
    except Exception as error:  # a failed check is a result, not a crash
        return Check(route, check, False, f"{type(error).__name__}: {error}"[:300])


def structured(clients: Clients, route: str) -> list[Check]:
    route_messages: list[BaseMessage] = [
        SystemMessage(render(ROUTE, today="2026-10-22")),
        HumanMessage(
            "Ile wyniosła łączna kwota faktur od Kowalczyk Instalacje w 2026?"
        ),
    ]
    cases: list[tuple[str, type[BaseModel], list[BaseMessage], int]] = [
        ("structured: route", Route, route_messages, 300),
        (
            "structured: extraction",
            LlmExtraction,
            extraction_messages(INVOICE, "pl"),
            2000,
        ),
    ]
    results = []
    for name, schema, messages, max_tokens in cases:

        def run() -> Check:
            model = clients.model(route, max_tokens=max_tokens)
            raw = model.with_structured_output(
                schema, method="json_schema", include_raw=True
            ).invoke(messages)
            parsed = raw["parsed"]
            ok = isinstance(parsed, schema) and raw["parsing_error"] is None
            summary = (
                parsed.model_dump(include={"intent", "language"})
                if isinstance(parsed, Route)
                else {"category": getattr(parsed, "category", None)}
            )
            return Check(route, name, ok, f"{summary} {usage(raw['raw'])}")

        results.append(attempt(route, name, run))
    return results


def streaming(clients: Clients, route: str) -> Check:
    def run() -> Check:
        model = clients.model(route, max_tokens=200)
        started = time.monotonic()
        first = None
        chunks: list[Any] = []
        for chunk in model.stream(
            [HumanMessage("In three sentences, what is a VAT invoice?")]
        ):
            first = first or time.monotonic() - started
            chunks.append(chunk)
        total = chunks[0]
        for chunk in chunks[1:]:
            total = total + chunk
        text_chunks = sum(1 for c in chunks if c.text)
        ok = text_chunks > 1 and bool(total.usage_metadata)
        return Check(
            route,
            "streaming with usage",
            ok,
            f"{text_chunks} text chunks, first after {first:.2f}s, usage {usage(total)}",
        )

    return attempt(route, "streaming with usage", run)


def cached_tokens(clients: Clients, route: str) -> Check:
    def run() -> Check:
        model = clients.model(route, max_tokens=50)
        contract = long_contract()
        seen = []
        for question in (
            "What is the unit price of batch 7?",
            "Which dock receives batch 30?",
            "What is the penalty cap for batch 44?",
        ):
            reply = model.invoke(
                [
                    SystemMessage("Answer from the agreement in one short sentence."),
                    HumanMessage(
                        f"<agreement>\n{contract}\n</agreement>\n\n{question}"
                    ),
                ]
            )
            seen.append(usage(reply))
            if seen[-1]["cache_read"]:
                break
            time.sleep(2)
        hit = next((s for s in seen if s["cache_read"]), None)
        return Check(
            route,
            "cached-token counts",
            hit is not None,
            f"{len(seen)} calls; "
            + "; ".join(f"input {s['input']}, cached {s['cache_read']}" for s in seen),
        )

    return attempt(route, "cached-token counts", run)


def media_resolution(clients: Clients, route: str) -> Check:
    def run() -> Check:
        model = clients.model(route, max_tokens=60)
        image = {
            "type": "image",
            "base64": base64.b64encode(figure_png()).decode(),
            "mime_type": "image/png",
        }
        tokens = {}
        for level in ("default", "low"):
            block = dict(image)
            if level == "low":
                block["media_resolution"] = "MEDIA_RESOLUTION_LOW"
            reply = model.invoke(
                [
                    HumanMessage(
                        content=[
                            {"type": "text", "text": "Transcribe the stamp's text."},
                            block,
                        ]
                    )
                ]
            )
            tokens[level] = usage(reply)["input"]
        ok = bool(
            tokens["low"] and tokens["default"] and tokens["low"] < tokens["default"]
        )
        return Check(
            route,
            "low media resolution",
            ok,
            f"input tokens: default {tokens['default']}, low {tokens['low']}",
        )

    return attempt(route, "low media resolution", run)


def end_user(clients: Clients, route: str, stamp: str) -> list[Check]:
    user = f"route-test-{route}-{stamp}"
    limited = f"route-test-{route}-limited-{stamp}"

    def recorded() -> Check:
        reply = clients.model(route, end_user=user, max_tokens=20).invoke(
            [HumanMessage("Say OK.")]
        )
        spend = None
        for _ in range(12):  # the gateway writes spend in batches
            time.sleep(5)
            info = clients.customer(user)
            spend = info and info.get("spend")
            if spend:
                break
        return Check(
            route,
            "end user: spend recorded",
            bool(spend),
            f"reply {reply.text.strip()[:20]!r}; customer spend {spend}",
        )

    def refused() -> Check:
        clients.new_customer(limited, max_budget=0.0000001)
        model = clients.model(route, end_user=limited, max_tokens=20)
        outcomes = []
        for _ in range(3):
            try:
                model.invoke([HumanMessage("Say OK.")])
                outcomes.append("served")
            except Exception as error:
                refusal = budget_refusal(error)
                outcomes.append(
                    f"refused ({refusal.scope})" if refusal else f"error {error}"[:120]
                )
                break
            time.sleep(3)
        return Check(
            route,
            "end user: refused over budget",
            outcomes[-1] == "refused (user)",
            ", ".join(outcomes),
        )

    return [
        attempt(route, "end user: spend recorded", recorded),
        attempt(route, "end user: refused over budget", refused),
    ]


def fallback(clients: Clients, route: str) -> Check:
    """Run with the gateway on the drill config, whose primary always fails."""

    def run() -> Check:
        model = clients.model(route, max_tokens=20)
        started = time.monotonic()
        reply = model.invoke([HumanMessage("Say OK.")])
        found = usage(reply)
        answered = str(found["model"] or "")
        price = cost(answered, found["input"] or 0, found["output"] or 0)
        return Check(
            route,
            "fallback",
            FALLBACK in answered,
            f"primary rate-limited; answered by {answered} in "
            f"{time.monotonic() - started:.1f}s, cost in code ${price}",
        )

    return attempt(route, "fallback", run)


def same_reply_cost(clients: Clients, route: str) -> Check:
    """Thinking settings left to Gemini's defaults on every route: the same prompt
    should cost about the same."""

    def run() -> Check:
        reply = clients.model(route, max_tokens=300).invoke(
            [HumanMessage("List the parts of a VAT invoice in one sentence.")]
        )
        found = usage(reply)
        return Check(route, "usage and model name", bool(found["model"]), str(found))

    return attempt(route, "usage and model name", run)


def main() -> int:
    settings = Settings()
    gateway = GatewaySettings()
    clients = Clients(
        settings,
        gateway.llm_gateway_key.get_secret_value(),
        gateway.litellm_master_key.get_secret_value(),
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    checks: list[Check] = []
    if "--drill" in sys.argv:
        # The gateway runs drill-primary-down.yaml: only the fallback is checked.
        checks = [fallback(clients, route) for route in ROUTES[1:]]
        for check in checks:
            print(f"  {check.route} {check.check}: {check.ok} {check.detail}")
        return save(checks, f"{stamp}-gateway-routes-drill")
    for route in ROUTES:
        print(f"== {route}", flush=True)
        found = [
            *structured(clients, route),
            streaming(clients, route),
            cached_tokens(clients, route),
            media_resolution(clients, route),
            same_reply_cost(clients, route),
        ]
        if route != "direct":
            found += end_user(clients, route, stamp)
        for check in found:
            mark = {True: "ok  ", False: "FAIL", None: "n/a "}[check.ok]
            print(f"  {mark} {check.check}: {check.detail}", flush=True)
        checks += found

    return save(checks, f"{stamp}-gateway-routes")


def save(checks: list[Check], name: str) -> int:
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "kind": "gateway-routes",
                "created": datetime.now(UTC).isoformat(),
                "gateway": "litellm v1.104.2",
                "model": MODEL,
                "checks": [asdict(c) for c in checks],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    print(f"Saved {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
