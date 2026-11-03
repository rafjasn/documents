"""Text addressed to an AI system, cut out of a document before any model reads it.

A document can carry instructions for the assistant that will read it ("ignore all
previous instructions…", "Asystencie AI: …"), forged source markup, our own delimiter
tags, or a markdown image that would carry data to someone else's server. Extraction,
contexts, search and answers all read the parsed document, so at ingest such text is
replaced by a marker before any of them sees it, and the document is flagged.

A flagged block is cut from its first flagged sentence to the end of the paragraph:
planted claims follow the line that addresses the reader ("Note for AI assistants:
ignore… The correct total is…"). Detection is by pattern, written for precision: it
finds nothing in the 200-document corpus. Paraphrases that don't address an AI get
past it; the answer step's rules and the URL filter are the next defences.
"""

import re
from dataclasses import dataclass, field

from docling_core.types.doc import DoclingDocument, PictureItem, TableItem, TextItem

MARKER = "[Removed: text addressed to an AI system]"
EXCERPT_CHARS = 200

# Who the text speaks to: an AI system, not a person.
AI = (
    r"\b(?:AI|A\.I\.|LLMs?|GPT|chat ?bots?|(?:large )?language models?"
    r"|(?:AI|virtual|digital) assistants?|dear assistant|assistants?\s*:"
    r"|asystencie|asystent\w*\s+AI|dla\s+asystent\w*"
    r"|sztuczn\w* inteligencj\w*|model\w* językow\w*)(?!\w)"
)
# What it tells the AI to do with its answer.
DIRECTIVE = (
    r"\b(?:ignor\w*|disregard\w*|override|instruct\w*|answer\w*|respond\w*|reply"
    r"|state|say|print|reveal|output|repeat|summari[sz]\w*|describe|your (?:answer|reply|response)"
    r"|zignoruj\w*|pomiń|instrukcj\w*|polece\w*|odpowi\w*|wypisz\w*|ujawnij\w*"
    r"|podawaj\w*|podaj|streszcz\w*|dołącz\w*|dodaj\w*)(?!\w)"
)
PATTERNS = {
    "override": re.compile(
        r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above"
        r"|earlier|preceding|other)\s+(?:instructions|prompts|rules|directions)"
        r"|\bzignoruj\w*\s+(?:wszystkie\s+)?(?:wcześniejsze|poprzednie|powyższe|inne)\s+"
        r"(?:polecenia|instrukcje|zasady)"
        r"|\bsystem\s+(?:message|prompt|instruction)s?\b|\bwiadomość\s+systemowa"
        r"|\binstrukcj\w*\s+systemow\w*",
        re.IGNORECASE,
    ),
    "delimiter": re.compile(
        r"</?\s*(?:source|computed|document|conversation|answer|system|instructions?)\b[^>]*>",
        re.IGNORECASE,
    ),
    "forged source": re.compile(
        r"\[\s*(?:source|źródło|zrodlo)\s*\d+\s*:", re.IGNORECASE
    ),
    "markdown image": re.compile(r"!\[[^\]]*\]\(\s*https?://", re.IGNORECASE),
}
ADDRESSED = (re.compile(AI, re.IGNORECASE), re.compile(DIRECTIVE, re.IGNORECASE))
SENTENCE = re.compile(r"(?<=[.!?])\s+")


def flagged(sentence: str) -> str | None:
    """Why the sentence is flagged, or None."""
    for reason, pattern in PATTERNS.items():
        if pattern.search(sentence):
            return reason
    ai, directive = ADDRESSED
    if ai.search(sentence) and directive.search(sentence):
        return "addressed to an AI"
    return None


@dataclass
class Quarantine:
    """What was cut from one document."""

    removed: list[str] = field(default_factory=list)

    def clean(self, text: str) -> str:
        """The text without its flagged part: from the first flagged sentence on."""
        sentences = SENTENCE.split(text)
        for i, sentence in enumerate(sentences):
            if flagged(sentence):
                cut = " ".join(sentences[i:])
                self.removed.append(cut[:EXCERPT_CHARS])
                kept = " ".join(sentences[:i])
                return f"{kept} {MARKER}" if kept else MARKER
        return text


def quarantine_document(
    document: DoclingDocument, captions: dict[str, str]
) -> list[str]:
    """Cut flagged text from the document's text items, table cells and image captions,
    in place; returns what was removed."""
    quarantine = Quarantine()
    for ref, caption in list(captions.items()):
        captions[ref] = quarantine.clean(caption)
    for item, _ in document.iterate_items():
        if isinstance(item, TextItem):
            cleaned = quarantine.clean(item.text)
            if cleaned != item.text:
                item.text = item.orig = cleaned
        elif isinstance(item, TableItem):
            for cell in item.data.table_cells:
                cell.text = quarantine.clean(cell.text)
        elif isinstance(item, PictureItem) and item.meta and item.meta.description:
            item.meta.description.text = quarantine.clean(item.meta.description.text)
    return quarantine.removed
