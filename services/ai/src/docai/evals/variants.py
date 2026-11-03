"""Pipeline variants under evaluation. Later stages add retrieval variants (V1+)."""

from dataclasses import dataclass
from functools import cache

from docai.corpus.manifest import EXTRACTED_DIR
from docai.evals.golden import GoldenItem
from docai.evals.llm import Messages

# The prompt and limits of the current NestJS chat (services/api/src/modules/chat/chat.service.ts).
LEGACY_SYSTEM = "You are a helpful assistant answering questions about a document. Here is the document content:\n\n{text}"
LEGACY_CHARS = 8000
LEGACY_MAX_TOKENS = 1000


@cache
def legacy_text(doc_id: str) -> str:
    return (EXTRACTED_DIR / "legacy" / f"{doc_id}.txt").read_text(encoding="utf-8")


@dataclass(frozen=True)
class TruncationBaseline:
    """V0: today's app. The question goes to single-document chat with the first relevant
    document, as extracted by the legacy worker and cut to its first 8,000 characters.
    Multi-document questions therefore see only their first document."""

    name: str = "V0"
    description: str = "Truncation baseline: first relevant document, legacy extraction, first 8,000 characters"

    def messages(self, item: GoldenItem) -> Messages:
        text = (
            legacy_text(item.relevant[0].doc_id)[:LEGACY_CHARS] if item.relevant else ""
        )
        return [("system", LEGACY_SYSTEM.format(text=text)), ("human", item.question)]

    @property
    def params(self) -> dict[str, int]:
        return {"max_tokens": LEGACY_MAX_TOKENS}


VARIANTS = {"V0": TruncationBaseline()}
