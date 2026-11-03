"""Golden set items, their JSONL file and the hand-review CSV round trip."""

import csv
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from docai.corpus.manifest import Language, Split

GOLDEN_DIR = Path(__file__).resolve().parents[3] / "evals" / "golden"
GOLDEN_PATH = GOLDEN_DIR / "v1.jsonl"
REVIEW_PATH = GOLDEN_DIR / "review-v1.csv"

QuestionType = Literal[
    "single_fact",
    "table",
    "image",
    "cross_doc",
    "aggregate",
    "cross_lingual",
    "unanswerable",
]
AnswerKind = Literal["number", "date", "text", "boolean", "refusal"]
Decision = Literal["keep", "drop"]


class Relevant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    page: int | None = None


class GoldenItem(BaseModel):
    """One question. `value` is what scoring compares: a decimal number, an ISO date,
    "yes"/"no", or text whose `accepted` forms are all valid answers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    split: Split
    type: QuestionType
    language: Language
    question: str
    answer: str
    kind: AnswerKind
    value: str
    accepted: tuple[str, ...] = ()
    relevant: tuple[Relevant, ...]
    source: str


def write_items(items: list[GoldenItem], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            item.model_dump_json() + "\n" for item in sorted(items, key=lambda i: i.id)
        ),
        encoding="utf-8",
    )


def read_items(path: Path = GOLDEN_PATH) -> list[GoldenItem]:
    return [
        GoldenItem.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


REVIEW_COLUMNS = (
    "decision",
    "id",
    "split",
    "type",
    "language",
    "question",
    "answer",
    "kind",
    "value",
    "accepted",
    "documents",
    "files",
    "source",
)


def write_review(
    items: list[GoldenItem],
    titles: dict[str, str],
    files: dict[str, str],
    path: Path = REVIEW_PATH,
) -> None:
    """A spreadsheet-friendly sheet: change `decision` to drop, or edit question/answer/value/accepted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: Excel needs the byte-order mark to read Polish characters.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        for item in sorted(items, key=lambda i: (i.split, i.type, i.id)):
            writer.writerow(
                {
                    "decision": "keep",
                    "id": item.id,
                    "split": item.split,
                    "type": item.type,
                    "language": item.language,
                    "question": item.question,
                    "answer": item.answer,
                    "kind": item.kind,
                    "value": item.value,
                    "accepted": " | ".join(item.accepted),
                    "documents": " ; ".join(
                        f"{r.doc_id}{f' p.{r.page}' if r.page else ''} {titles.get(r.doc_id, '')}".strip()
                        for r in item.relevant
                    ),
                    "files": " ; ".join(files.get(r.doc_id, "") for r in item.relevant),
                    "source": item.source,
                }
            )


def apply_review(
    drafts: list[GoldenItem], path: Path = REVIEW_PATH
) -> list[GoldenItem]:
    """Keep reviewed items, applying edits to the question, answer, value and accepted forms."""
    by_id = {item.id: item for item in drafts}
    kept = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            decision = row["decision"].strip().lower()
            if decision not in ("keep", "drop"):
                raise ValueError(
                    f"{row['id']}: decision must be keep or drop, got {row['decision']!r}"
                )
            if decision == "drop":
                continue
            item = by_id[row["id"]]
            accepted = tuple(
                part.strip() for part in row["accepted"].split("|") if part.strip()
            )
            kept.append(
                item.model_copy(
                    update={
                        "question": row["question"].strip(),
                        "answer": row["answer"].strip(),
                        "value": row["value"].strip(),
                        "accepted": accepted,
                    }
                )
            )
    return kept
