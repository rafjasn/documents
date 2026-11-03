"""Numbers and dates as people write them in Polish and English: 5 166,00, 5,166.00,
17.04.2026, 17 April 2026, 17 kwietnia 2026, April 17, 2026, 4/17/26. Used to score
answers and, in the chat graph, to check an answer leads with the value computed in
code. `quantities` reads the numbers a text states with what they count (a currency,
days, another word), leaving out identifiers (INV-260056, 43/2025, § 4) and dates."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

EN_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
PL_MONTHS = {  # accent-free stems of every case: stycznia, styczeń, ...
    stem: number
    for number, stem in enumerate(
        (
            "stycz",
            "lut",
            "mar",
            "kwie",
            "maj",
            "czerw",
            "lip",
            "sierp",
            "wrzes",
            "pazdziern",
            "listopad",
            "grud",
        ),
        start=1,
    )
}
MONTH_WORD = r"[a-ząćęłńóśźż]+"
TEXT_DATE_DMY = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH_WORD})\.?,?\s+(\d{{4}})\b"
)
TEXT_DATE_MDY = re.compile(
    rf"\b({MONTH_WORD})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b"
)
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
NUMERIC_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b")
NUMBER = re.compile(
    r"(?<![\d.,])\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|(?<![\d.,])\d+(?:[.,]\d+)*"
)


def normalise_text(text: str) -> str:
    """Lower case, no accents, punctuation as spaces: 'Leżoń Serwis, S.A.' -> 'lezon serwis s a'."""
    decomposed = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L"))
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^\w]+", " ", plain.casefold()).split())


def safe_date(year: int, month: int, day: int) -> date | None:
    year = year + 2000 if year < 50 else year + 1900 if year < 100 else year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def month_number(word: str) -> int | None:
    word = word.casefold().rstrip(".")
    if word in EN_MONTHS:
        return EN_MONTHS[word]
    plain = normalise_text(word)
    for stem, number in PL_MONTHS.items():
        if plain.startswith(stem):
            return number
    return None


def dates_in(text: str) -> tuple[set[date], str]:
    """All dates found, and the text with them removed (so their digits aren't read as numbers)."""
    mentions, lowered = date_mentions(text)
    return {value for _, _, readings in mentions for value in readings}, lowered


def date_mentions(text: str) -> tuple[list[tuple[int, int, set[date]]], str]:
    """Where each date is written and what it can mean (04/05/2026 two things), and the
    lower-cased text with the dates blanked out, every other character where it was."""
    mentions: list[tuple[int, int, set[date]]] = []
    lowered = text.casefold()

    def take(pattern: re.Pattern[str], convert: object) -> None:
        nonlocal lowered
        for match in pattern.finditer(lowered):
            readings = {v for v in convert(match) if v is not None}  # type: ignore[operator]
            if readings:
                mentions.append((match.start(), match.end(), readings))
        lowered = pattern.sub(lambda m: " " * len(m.group()), lowered)

    take(ISO_DATE, lambda m: [safe_date(int(m[1]), int(m[2]), int(m[3]))])
    take(
        TEXT_DATE_DMY,
        lambda m: [
            safe_date(int(m[3]), month, int(m[1]))
            for month in [month_number(m[2])]
            if month
        ],
    )
    take(
        TEXT_DATE_MDY,
        lambda m: [
            safe_date(int(m[3]), month, int(m[2]))
            for month in [month_number(m[1])]
            if month
        ],
    )
    # 04/05/2026 is ambiguous between day-first and month-first; accept either reading.
    take(
        NUMERIC_DATE,
        lambda m: [
            safe_date(int(m[3]), int(m[2]), int(m[1])),
            safe_date(int(m[3]), int(m[1]), int(m[2])),
        ],
    )
    mentions.sort()
    return mentions, lowered


def number_candidates(token: str) -> set[Decimal]:
    token = re.sub(r"[   ]", "", token)
    readings = set()
    if "." in token and "," in token:
        decimal_mark = max(token.rfind("."), token.rfind(","))
        readings.add(
            re.sub(r"[.,]", "", token[:decimal_mark]) + "." + token[decimal_mark + 1 :]
        )
    elif "," in token or "." in token:
        mark = "," if "," in token else "."
        head, _, tail = token.rpartition(mark)
        if len(tail) == 3:  # 1,234 or 1.234: thousands, or (rarely) a decimal
            readings.add(token.replace(mark, ""))
            if token.count(mark) == 1:
                readings.add(head + "." + tail)
        else:
            readings.add(head.replace(mark, "") + "." + tail)
    else:
        readings.add(token)
    values = set()
    for reading in readings:
        try:
            values.add(Decimal(reading))
        except InvalidOperation:
            continue
    return values


def numbers_in(text: str) -> set[Decimal]:
    _, without_dates = dates_in(text)
    values: set[Decimal] = set()
    for match in NUMBER.finditer(without_dates):
        values |= number_candidates(match.group())
    return values


# What a number counts --------------------------------------------------------------

TOLERANCE = Decimal("0.005")
CURRENCIES = {"£": "GBP", "gbp": "GBP", "€": "EUR", "eur": "EUR", "$": "USD",
              "usd": "USD", "zł": "PLN", "pln": "PLN", "rm": "MYR", "myr": "MYR"}  # fmt: skip
# Unit families of accent-free words: "30 days", "30 dni" and "30-dniowy" are all days.
UNIT_WORDS = (
    ("day", re.compile(r"days?|dni\w*|dzien|dnia")),
    ("week", re.compile(r"weeks?|tydzien|tygodni\w*")),
    ("month", re.compile(r"months?|miesi\w*")),
    ("year", re.compile(r"years?|lat|lata|rok\w*")),
    ("percent", re.compile(r"%|percent|procent\w*")),
)
CURRENCY_CODES = set(CURRENCIES.values())
KNOWN_UNITS = {*CURRENCY_CODES, *(family for family, _ in UNIT_WORDS)}
# Words after a number that say nothing about what it counts.
NOT_UNITS = {"and", "or", "in", "on", "at", "of", "is", "was", "the", "a", "an", "to",
             "for", "from", "with", "by", "as", "but", "not", "this", "that", "these",
             "it", "which", "each", "per", "net", "gross", "i", "oraz", "lub", "w", "na",
             "z", "do", "od", "za", "po", "jest", "to", "ale", "nie", "czy", "ta", "ten",
             "te", "co", "netto", "brutto"}  # fmt: skip
CURRENCY_BEFORE = re.compile(r"(?P<c>[£€$]|\b(?:gbp|eur|usd|pln|zł|rm|myr))\s*$")
WORD_AFTER = re.compile(r"[ \t]*-?[ \t]*([£€$%]|[^\W\d_]+)")
# INV-260056, FV/2026, Q3, 77/87, 52-027, 10:30, § 4, nr 5
IDENTIFIER_BEFORE = re.compile(
    r"(?:[^\W\d_]|\w[-/:]|[#§]\s?|\b(?:nr|no|art|ust|pkt|section|clause)\.?\s)$"
)
IDENTIFIER_AFTER = re.compile(r"[-/:]\d")
# Citation markers ([1][3]) and Markdown emphasis, blanked before reading.
MARKUP = re.compile(r"\[\d+(?:\s*[,;]\s*\d+)*\]|\*+")
NEGATED = re.compile(
    r"(?:\b(?:not|nie|never|nor|zamiast|instead of|rather than)|n['’]t)"
    r"\s+(?:[^\W\d_]+\s+){0,2}(?:[£€$]|\b(?:gbp|eur|usd|pln|zł|rm|myr))?\s?$"
)
YEAR = re.compile(r"(?:19|20)\d\d")
CONTEXT = 40  # characters before a value that can make it an identifier or negate it


@dataclass(frozen=True)
class Quantity:
    """A number as an answer states it."""

    text: str  # as written
    values: frozenset[Decimal]  # its readings: 1,234 is a thousand or (rarely) one
    unit: str | None  # a currency, a unit family, another word after it; None: bare
    negated: bool  # "not 98.35", "nie 98,35"
    year: bool  # a bare 1900-2099, which says when rather than how much

    def equals(self, value: Decimal) -> bool:
        return any(abs(v - value) < TOLERANCE for v in self.values)

    @property
    def amount(self) -> bool:
        """An amount of money, or a number with nothing after it that says otherwise:
        not "3 invoices", "14 days", "23%" or a year."""
        return not self.year and (self.unit is None or self.unit in CURRENCY_CODES)


def unit_of(currency: re.Match[str] | None, after: str) -> str | None:
    if currency:
        return CURRENCIES[currency["c"]]
    word = WORD_AFTER.match(after)
    if word is None:
        return None
    token = word.group(1)
    if token in CURRENCIES:
        return CURRENCIES[token]
    plain = normalise_text(token) or token
    for family, pattern in UNIT_WORDS:
        if pattern.fullmatch(plain):
            return family
    return None if plain in NOT_UNITS else plain


def quantities(answer: str) -> list[Quantity]:
    """The numbers a text states, in order: not the digits of dates, identifiers or
    citation markers."""
    _, text = date_mentions(MARKUP.sub(lambda m: " " * len(m.group()), answer))
    found = []
    for match in NUMBER.finditer(text):
        before = text[max(0, match.start() - CONTEXT) : match.start()]
        after = text[match.end() : match.end() + CONTEXT]
        currency = CURRENCY_BEFORE.search(before)
        if not currency and (
            IDENTIFIER_BEFORE.search(before) or IDENTIFIER_AFTER.match(after)
        ):
            continue
        found.append(
            Quantity(
                match.group(),
                frozenset(number_candidates(match.group())),
                unit_of(currency, after),
                NEGATED.search(before) is not None,
                YEAR.fullmatch(match.group()) is not None,
            )
        )
    return found
