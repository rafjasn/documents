"""BM25 sparse vectors, computed in code; Qdrant applies the IDF part (Modifier.IDF).

Tokens are lower-case words with accents folded, plus whole identifiers such as
"fv/2026/01/319" and amounts normalised to "3564.54", so invoice numbers, tax IDs and
totals match however they are formatted. `stem` truncates words to their first
characters: a dependency-free stemmer for Polish, whose inflection defeats exact-word
BM25 and which Snowball doesn't cover.
"""

import re
import unicodedata
import zlib
from collections import Counter
from dataclasses import dataclass

from qdrant_client import models

WORD = re.compile(r"\w+")
# Letters and digits joined by / - or . : invoice numbers, tax IDs, ISO dates.
IDENTIFIER = re.compile(r"\w+(?:[/.\-]\w+)+")
# Amounts with a two-digit decimal part, with or without thousands separators.
AMOUNT = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:[  .,]\d{3})+|\d+)[.,]\d{2}(?![\d.,]\d)")
K1, B = 1.2, 0.75


def fold(text: str) -> str:
    """Lower case without accents: 'Płatność' -> 'platnosc'."""
    text = text.replace("ł", "l").replace("Ł", "L")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def amount(text: str) -> str:
    """'3 564,54' and '3,564.54' -> '3564.54'."""
    digits = re.sub(r"[  ]", "", text)
    whole, decimals = digits[:-3], digits[-2:]
    return re.sub(r"[.,]", "", whole) + "." + decimals


def is_identifier(token: str) -> bool:
    """Invoice numbers, tax IDs and dates, but not decimals such as '30.91'."""
    has_digit = any(c.isdigit() for c in token)
    has_letter = any(c.isalpha() for c in token)
    return has_digit and ("/" in token or "-" in token or has_letter)


def token_index(token: str) -> int:
    return zlib.crc32(token.encode())


@dataclass(frozen=True)
class SparseEncoder:
    stem: int | None = None  # keep words to this many characters; None keeps them whole
    avg_tokens: float = 256.0  # BM25 length normalisation

    def tokens(self, text: str) -> list[str]:
        folded = fold(text)
        words = []
        for word in WORD.findall(folded):
            if self.stem and word.isalpha() and len(word) > self.stem:
                word = word[: self.stem]
            words.append(word)
        identifiers = [i for i in IDENTIFIER.findall(folded) if is_identifier(i)]
        amounts = [amount(a) for a in AMOUNT.findall(text)]
        return words + identifiers + amounts

    def document(self, text: str) -> models.SparseVector:
        tokens = self.tokens(text)
        length = len(tokens)
        counts = Counter(token_index(t) for t in tokens)
        norm = K1 * (1 - B + B * length / self.avg_tokens)
        indices = sorted(counts)
        values = [counts[i] * (K1 + 1) / (counts[i] + norm) for i in indices]
        return models.SparseVector(indices=indices, values=values)

    def query(self, text: str) -> models.SparseVector:
        indices = sorted({token_index(t) for t in self.tokens(text)})
        return models.SparseVector(indices=indices, values=[1.0] * len(indices))
