"""Language detection: lingua over a fixed set of languages. Local, free, deterministic."""

from functools import cache

from lingua import Language, LanguageDetector, LanguageDetectorBuilder

ISO_CODES = {
    Language.ENGLISH: "en",
    Language.POLISH: "pl",
    Language.GERMAN: "de",
    Language.FRENCH: "fr",
    Language.SPANISH: "es",
    Language.ITALIAN: "it",
    Language.DUTCH: "nl",
    Language.PORTUGUESE: "pt",
}
UNKNOWN = "unknown"
SAMPLE_CHARS = 20_000


@cache
def detector() -> LanguageDetector:
    return LanguageDetectorBuilder.from_languages(*ISO_CODES).build()


def detect_language(text: str) -> str:
    """ISO 639-1 code of the main language, or "unknown" for too little text."""
    sample = text[:SAMPLE_CHARS]
    if sum(word.isalpha() for word in sample.split()) < 3:
        return UNKNOWN
    language = detector().detect_language_of(sample)
    return ISO_CODES.get(language, UNKNOWN) if language else UNKNOWN
