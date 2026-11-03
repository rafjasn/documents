"""The corpus manifest: one record per document, committed to git."""

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

CORPUS_ROOT = Path(__file__).resolve().parents[3] / "corpus"
RAW_DIR = CORPUS_ROOT / "raw"
DATA_DIR = CORPUS_ROOT / "data"
EXTRACTED_DIR = CORPUS_ROOT / "extracted"
MANIFEST_PATH = CORPUS_ROOT / "manifest.jsonl"

Source = Literal["cuad", "sroie", "synthetic", "injection"]
# security: the prompt-injection fixtures (docai.corpus.injection), outside the corpus.
Split = Literal["train", "dev", "test", "security"]
Language = Literal["en", "pl"]
Format = Literal["pdf", "scanned_pdf", "docx", "photo", "image"]

MIME_TYPES: dict[Format, str] = {
    "pdf": "application/pdf",
    "scanned_pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "photo": "image/jpeg",
    "image": "image/jpeg",
}


class DocumentRecord(BaseModel):
    """One corpus document. `group` is the unit that is never split across splits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str
    source: Source
    title: str
    category: str
    language: Language
    format: Format
    path: str
    split: Split
    group: str
    license: str
    sha256: str
    pages: int | None = None

    @property
    def mime_type(self) -> str:
        return MIME_TYPES[self.format]

    @property
    def file(self) -> Path:
        return DATA_DIR / self.path


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(
    records: Iterable[DocumentRecord], path: Path = MANIFEST_PATH
) -> None:
    ordered = sorted(records, key=lambda record: record.doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(record.model_dump_json() + "\n" for record in ordered), encoding="utf-8"
    )


def read_manifest(path: Path = MANIFEST_PATH) -> list[DocumentRecord]:
    return [
        DocumentRecord.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
