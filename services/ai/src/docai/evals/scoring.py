"""Rule-based answer scoring.

`score` returns True or False when the rules are certain and None when an LLM judge
must decide (paraphrased text, an unclear yes/no, a refusal worded unusually).
Numbers and dates are read in Polish and English formats: 5 166,00, 5,166.00,
17.04.2026, 17 April 2026, 17 kwietnia 2026, April 17, 2026, 4/17/26.

The rules credit only the value an answer asserts. Finding the expected value is not
enough when the answer negates it ("not 98.35"), gives it in another currency or unit,
or also states a value of the same kind (a second total, a second date): those go to
the judge. Identifiers (INV-260056, 43/2025, § 4), years and citation markers are not
values. A refusal is certain only when the answer opens with one and every later
sentence refuses too, says what the documents only give, or asks for the document;
"However, the rate is 8%." or "The answer is 8%." goes to the judge.
"""

import re
from datetime import date
from decimal import Decimal

from docai.agent.sources import CITATION
from docai.core.numbers import (
    CONTEXT,
    KNOWN_UNITS,
    NEGATED,
    NUMBER,
    TOLERANCE,
    date_mentions,
    dates_in,
    normalise_text,
    number_candidates,
    numbers_in,
    quantities,
)
from docai.evals.golden import GoldenItem

__all__ = ["NUMBER", "dates_in", "normalise_text", "number_candidates", "numbers_in"]

REFUSAL = re.compile(
    r"\b("
    r"not (?:\w+ )?(?:mentioned|specified|stated|provided|included|listed|available|found|given|indicated|contain)"
    r"|(?:(?:does|do|did) not|(?:doesn|don|didn)['’]t) (?:\w+ )?(?:contain|include|specify|mention|state|provide|list|show|appear|exist)"
    r"|no (?:information|mention|details|record|such|data)"
    r"|(?:cannot|can't|unable to|could not|couldn't) (?:find|determine|locate|identify|answer|see)"
    r"|there is no(?! (?:doubt|question|problem|issue)\b)|isn't (?:mentioned|specified|stated|provided|included)"
    r"|(?:does|do|did) not provide for|provides no|please (?:provide|paste|share|upload)"
    r"|(?:you )?(?:haven't|have not|did not|didn't) (?:included|include|provided|provide|attached|attach)"
    r"|(?:document|text|file) (?:is|was|appears) (?:blank|empty)"
    r"|nie (?:zawiera|podano|wskazano|określono|wymieniono|ma (?:informacji|danych|takiej)|znaleziono|znalazłem|znalazłam|można|jest (?:określon|podan|wskazan|wymienion)|podaje|wynika|przewiduje|jestem w stanie|mogę)"
    r"|nie podał(?:eś|aś|es|as)|proszę (?:o )?(?:wklej|podaj|prześlij|przeslij|załącz|zalacz|wklejenie|przesłanie)"
    r"|brak (?:informacji|danych|takiej|takich)|(?:dokument|tekst) jest pusty"
    r")",
    re.IGNORECASE,
)
# What turns a refusal phrase around ("There is no information missing.") or announces
# an answer after it.
UNDONE = re.compile(
    r"\b(?:no|nothing|nic)\b(?:\s+\w+){0,2}?\s+(?:is\s+|are\s+)?(?:missing|lacking)\b"
    r"|\bnie brakuje\b",
    re.IGNORECASE,
)
ANSWER_CUE = re.compile(
    r"\b(?:the )?answer(?: is\b|:)|\bodpowied[zź](?: to\b| brzmi\b|:)", re.IGNORECASE
)
CONTRAST = re.compile(
    r"\b(?:however|but|although|though|yet|jednak|ale|lecz|natomiast|chociaż|choć)\b",
    re.IGNORECASE,
)
ONLY = re.compile(r"\b(?:only|merely|solely|jedynie|wyłącznie|tylko)\b", re.IGNORECASE)
REQUEST = re.compile(
    r"\b(?:please|once you|if you|proszę|jeśli|jeżeli|gdy)\b.*?"
    r"\b(?:provid|past|shar|upload|attach|send|wklej|prześl|przesl|poda|załącz|zalacz)",
    re.IGNORECASE,
)
SENTENCE = re.compile(r"(?<=[.!?…])\s+(?=[\"„“'(]?[A-ZĄĆĘŁŃÓŚŹŻ])|\n+")

NEGATORS = {"not", "nie", "never", "nor", "zamiast", "instead", "rather", "t"}
YES = {"yes", "tak", "correct", "yep"}
NO = {"no", "nie", "incorrect", "nope"}


def plain_text(answer: str) -> str:
    """The answer without citation markers and Markdown emphasis."""
    return CITATION.sub("", answer).replace("*", "")


def expected_unit(reference: str) -> str | None:
    """The currency or unit family of a golden answer such as "RM 98.35" or "30 dni"."""
    units = [q.unit for q in quantities(reference) if q.unit in KNOWN_UNITS]
    return units[0] if units else None


def number_verdict(item: GoldenItem, answer: str) -> bool | None:
    expected = Decimal(item.value)
    stated = quantities(answer)
    matches = [q for q in stated if q.equals(expected)]
    if not matches:
        # Inside an identifier ("Q3" for 3) is unclear; nowhere is wrong.
        seen = numbers_in(plain_text(answer))
        return None if any(abs(v - expected) < TOLERANCE for v in seen) else False
    if any(q.negated for q in matches):
        return None
    wanted = expected_unit(item.answer)
    units = {q.unit for q in matches if q.unit is not None}
    kinds = units | ({wanted} if wanted else set())
    rivals = [
        q
        for q in stated
        if not q.equals(expected) and not q.year and (q.unit is None or q.unit in kinds)
    ]
    if rivals:  # "999999 ... not 98.35", "98.35. Actually 99.10", a sum's addends
        return None
    given = units & KNOWN_UNITS
    if wanted and given - {wanted}:
        return None if wanted in given else False  # "567.00 EUR" for £567.00
    return True


def date_verdict(item: GoldenItem, answer: str) -> bool | None:
    expected = date.fromisoformat(item.value)
    mentions, text = date_mentions(plain_text(answer))
    if not mentions:
        return None
    matching = [start for start, _, readings in mentions if expected in readings]
    if not matching:
        return False
    if any(NEGATED.search(text[max(0, s - CONTEXT) : s]) for s in matching):
        return None
    if any(expected not in readings for _, _, readings in mentions):
        return None  # another date, which could be the one meant
    return True


def text_verdict(item: GoldenItem, answer: str) -> bool | None:
    padded = f" {normalise_text(plain_text(answer))} "
    found = negated = False
    for form in item.accepted or (item.value,):
        needle = normalise_text(form)
        if not needle:
            continue
        for match in re.finditer(re.escape(f" {needle} "), padded):
            if NEGATORS & set(padded[: match.start()].split()[-3:]):
                negated = True
            else:
                found = True
    return True if found and not negated else None


def yes_no(text: str) -> str | None:
    words = normalise_text(text).split()
    if not words:
        return None
    if words[0] in YES:
        return "yes"
    if words[0] in NO:
        return "no"
    return None


def is_refusal(text: str) -> bool:
    return bool(REFUSAL.search(text.replace("*", "")))


def refusal_verdict(answer: str) -> bool | None:
    text = plain_text(answer)
    if UNDONE.search(text) or ANSWER_CUE.search(text):
        return None
    sentences = [s for s in SENTENCE.split(text.strip()) if s.strip()]
    if not sentences or not is_refusal(sentences[0]):
        return None
    for n, sentence in enumerate(sentences):
        head, *turns = CONTRAST.split(sentence)
        if not all(is_refusal(turn) for turn in turns):
            return None  # "..., but the rate is 8%."
        if n and not (is_refusal(head) or ONLY.search(head) or REQUEST.search(head)):
            return None  # a later sentence that could be an answer
    return True


def score(item: GoldenItem, answer: str) -> bool | None:
    if item.kind == "number":
        return number_verdict(item, answer)
    if item.kind == "date":
        return date_verdict(item, answer)
    if item.kind == "boolean":
        verdict = yes_no(answer)
        return None if verdict is None else verdict == item.value
    if item.kind == "refusal":
        return refusal_verdict(answer)
    return text_verdict(item, answer)
