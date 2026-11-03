"""Gemini API paid-tier prices in USD per million tokens (input, output), standard tier.

Source: https://ai.google.dev/gemini-api/docs/pricing, checked 2026-10-11. Output prices
include thinking tokens. Gemma models are free; 3.6 and 3.8 Flash double on 2027-01-01.
Input tokens served from Gemini's cache (implicit prefix caching) cost a tenth of the
input price.
"""

from decimal import Decimal

PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gemini-3.1-flash-lite": (Decimal("0.25"), Decimal("1.50")),
    "gemini-3.5-flash-lite": (Decimal("0.30"), Decimal("2.50")),
    "gemini-3.6-flash": (Decimal("0.75"), Decimal("3.75")),
    "gemini-3.8-flash": (Decimal("0.75"), Decimal("3.75")),
    "gemini-3.1-pro-preview": (Decimal("2.00"), Decimal("12.00")),
    "gemma-4-31b-it": (Decimal("0"), Decimal("0")),
    "gemma-4-26b-a4b-it": (Decimal("0"), Decimal("0")),
}


CACHED_INPUT_SHARE = Decimal("0.1")


def cost(
    spec: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
) -> Decimal | None:
    """Cost of a call in USD, or None for a model missing from the table.
    `input_tokens` includes the cached ones."""
    model = spec.partition(":")[2] or spec
    if model not in PRICES:
        return None
    input_price, output_price = PRICES[model]
    billed_input = input_tokens - cached_input_tokens * (1 - CACHED_INPUT_SHARE)
    return (input_price * billed_input + output_price * output_tokens) / 1_000_000
