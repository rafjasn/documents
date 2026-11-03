"""Personal identifiers masked out of traces before they leave the services (Stage 10a).

Tax IDs (Polish NIP by checksum, EU VAT numbers by format), PESEL numbers, bank account
numbers, email addresses and phone numbers become their type and a keyed hash, such as
`[NIP:3f9a1c2e7b]`. The hash keeps traces comparable without the value, and the key
stops brute force: there are only 10^10 tax IDs to try against an unkeyed hash.

Amounts, dates and document numbers stay readable; they are what traces are read for,
and the tests check that each survives. Names and addresses aren't detected, which is a
stated residual risk. Masking protects the trace store only: Gemini still receives the
documents' text, and the local caches keep it.

`docai.core.tracing` passes `Masker.mask` to the Langfuse client, so it applies to every
trace this process sends.
"""

import hashlib
import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Matched in this order: accounts first, so a VAT or NIP pattern can't take part of one.
EU_COUNTRIES = "AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|GB|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK|XI"
# IBAN-style: a country code, two check digits, then groups of four; or the Polish
# 26-digit form without the country code. Matched by format, not checksum: the
# synthetic corpus's account numbers are random digits.
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
POLISH_ACCOUNT = re.compile(r"(?<!\d)(?<!\d[,.])\d{2}(?:[ ]?\d{4}){6}(?!\d|[,.]\d)")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# Only numbers marked as phones, by a leading + or a label: a bare "123 456 789" is as
# likely an amount.
PHONE_INTERNATIONAL = re.compile(
    r"(?<![\w+])\+\d{1,3}(?:[ .-]?\(?\d{1,4}\)?){2,5}(?!\d)"
)
PHONE_LABELLED = re.compile(
    r"(?i)\b(?P<label>tel(?:efon)?|phone|mobile|fax|kom(?:órka)?)\.?\s*[:.]?\s*"
    r"(?P<number>\(?\d[\d ()-]{6,18}\d)"
)
EU_VAT = re.compile(rf"\b(?:{EU_COUNTRIES})[ ]?(?=[0-9A-Z]*\d{{6}})[0-9A-Z]{{8,12}}\b")
# Ten digits, plain or grouped 3-3-2-2 or 3-2-2-3 with dashes or spaces; only valid
# checksums count, and a digit, comma or point on either side means it's something else.
NIP = re.compile(
    r"(?<![\d-])(?<!\d[,.])(?:\d{3}[- ]?\d{3}[- ]?\d{2}[- ]?\d{2}|\d{3}[- ]\d{2}[- ]\d{2}[- ]\d{3})(?!\d|[,.]\d)"
)
# After a NIP label, any 10 digits count, so a typo in the checksum doesn't leak one.
NIP_LABELLED = re.compile(
    r"(?i)\bNIP\s*[:.]?\s*(?:PL\s?)?(?P<number>\d(?:[- ]?\d){9})(?!\d|[,.]\d)"
)
PESEL = re.compile(r"(?<!\d)(?<!\d[,.])\d{11}(?!\d|[,.]\d)")

NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)
PESEL_WEIGHTS = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)


def digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit())


def valid_nip(text: str) -> bool:
    d = [int(c) for c in digits(text)]
    if len(d) != 10:
        return False
    check = sum(a * w for a, w in zip(d, NIP_WEIGHTS)) % 11
    return check < 10 and check == d[9]


def valid_pesel(text: str) -> bool:
    d = [int(c) for c in digits(text)]
    if len(d) != 11:
        return False
    return (10 - sum(a * w for a, w in zip(d, PESEL_WEIGHTS)) % 10) % 10 == d[10]


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern[str]
    valid: Callable[[str], bool] = lambda value: True
    group: str | int = 0


RULES = (
    Rule("EMAIL", EMAIL),
    Rule("ACCOUNT", IBAN),
    Rule("ACCOUNT", POLISH_ACCOUNT),
    Rule("PHONE", PHONE_INTERNATIONAL),
    Rule("PHONE", PHONE_LABELLED, group="number"),
    Rule("VAT", EU_VAT),
    Rule("NIP", NIP_LABELLED, group="number"),
    Rule("NIP", NIP, valid_nip),
    Rule("PESEL", PESEL, valid_pesel),
)
MARKER = re.compile(r"\[(?:EMAIL|ACCOUNT|PHONE|VAT|NIP|PESEL):[0-9a-f]{10}\]")


class Masker:
    """Masks identifiers in strings and in the dicts and lists that hold them."""

    def __init__(self, key: bytes) -> None:
        self.key = key

    def token(self, kind: str, value: str) -> str:
        normal = "".join(c for c in value.upper() if c.isalnum())
        digest = hmac.new(self.key, f"{kind}:{normal}".encode(), hashlib.sha256)
        return f"[{kind}:{digest.hexdigest()[:10]}]"

    def text(self, text: str) -> str:
        for rule in RULES:
            text = self._apply(rule, text)
        return text

    def _apply(self, rule: Rule, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            value = match.group(rule.group)
            if MARKER.fullmatch(value) or not rule.valid(value):
                return match.group(0)
            start, end = match.span(rule.group)
            whole_start = match.start(0)
            whole = match.group(0)
            return (
                whole[: start - whole_start]
                + self.token(rule.kind, value)
                + whole[end - whole_start :]
            )

        return rule.pattern.sub(replace, text)

    def mask(self, data: Any) -> Any:
        """Masked copy of `data`: strings, and the values of dicts, lists and tuples."""
        if isinstance(data, str):
            return self.text(data)
        if isinstance(data, dict):
            return {key: self.mask(value) for key, value in data.items()}
        if isinstance(data, (list, tuple)):
            return [self.mask(value) for value in data]
        if hasattr(data, "model_dump"):
            return self.mask(data.model_dump(mode="json"))
        return data
