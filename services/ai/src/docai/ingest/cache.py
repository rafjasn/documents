"""On-disk cache for ingestion: parse results and model replies, keyed by content hash.

Reprocessing a file, for example when the seed command reloads the corpus after a
LocalStack restart, then makes no model calls and doesn't run Docling or OCR again.
One JSON file per entry, written atomically, so the worker container and processes on
the host can share the directory.
"""

import hashlib
import json
import os
import shutil
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

from docling_core.types.doc import DoclingDocument
from pydantic import BaseModel

from docai.ingest.parse import FigureImage, PageInfo, ParseResult
from docai.ingest.setup import TESSDATA_COMMIT

# Bump when parsing changes in a way that should invalidate cached parse results.
PARSER_VERSION = "2"  # 2: Docling OCRs regions without a text layer


def parser_fingerprint() -> str:
    parts = [PARSER_VERSION, version("docling-slim"), version("docling-ibm-models")]
    parts += [version("tesserocr"), TESSDATA_COMMIT]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


class StoredFigure(BaseModel):
    figure_id: str
    ref: str
    kind: Literal["figure", "page"]
    page_no: int | None
    width: int
    height: int
    mime_type: str
    file_name: str


class StoredParse(BaseModel):
    parser: str
    pages: list[PageInfo]
    warnings: list[str]
    figures: list[StoredFigure]
    document: dict[str, Any]


def write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(handle, "wb") as file:
        file.write(data)
    os.replace(temporary, path)


def save_parse(directory: Path, result: ParseResult) -> None:
    """Write a parse result as document.json plus figures/<figure>.<ext>."""
    stored = StoredParse(
        parser=result.parser,
        pages=result.pages,
        warnings=result.warnings,
        figures=[
            StoredFigure(
                figure_id=f.figure_id,
                ref=f.ref,
                kind=f.kind,
                page_no=f.page_no,
                width=f.width,
                height=f.height,
                mime_type=f.mime_type,
                file_name=f.file_name,
            )
            for f in result.figures
        ],
        document=result.document.export_to_dict(),
    )
    for figure in result.figures:
        write_atomically(directory / "figures" / figure.file_name, figure.data)
    write_atomically(directory / "parse.json", stored.model_dump_json().encode())


def load_parse(directory: Path) -> ParseResult | None:
    path = directory / "parse.json"
    if not path.exists():
        return None
    stored = StoredParse.model_validate_json(path.read_bytes())
    figures = []
    for f in stored.figures:
        image = directory / "figures" / f.file_name
        if not image.exists():
            return None
        figures.append(
            FigureImage(
                figure_id=f.figure_id,
                ref=f.ref,
                kind=f.kind,
                page_no=f.page_no,
                width=f.width,
                height=f.height,
                mime_type=f.mime_type,
                data=image.read_bytes(),
            )
        )
    return ParseResult(
        document=DoclingDocument.model_validate(stored.document),
        parser=stored.parser,
        pages=stored.pages,
        figures=figures,
        warnings=stored.warnings,
    )


class DiskCache:
    """Parse results under parsed/<file hash>-<parser>/, replies under llm/<key>.json."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def parse_dir(self, file_sha256: str) -> Path:
        return self.root / "parsed" / f"{file_sha256}-{parser_fingerprint()}"

    def get_parse(self, file_sha256: str) -> ParseResult | None:
        return load_parse(self.parse_dir(file_sha256))

    def put_parse(self, file_sha256: str, result: ParseResult) -> None:
        target = self.parse_dir(file_sha256)
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=self.root, prefix=".parse-"))
        try:
            save_parse(staging, result)
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _reply_path(self, key: str) -> Path:
        return self.root / "llm" / key[:2] / f"{key}.json"

    def get_reply(self, key: str) -> dict[str, Any] | None:
        path = self._reply_path(key)
        if not path.exists():
            return None
        value: dict[str, Any] = json.loads(path.read_text())
        return value

    def put_reply(self, key: str, value: dict[str, Any]) -> None:
        write_atomically(
            self._reply_path(key), json.dumps(value, ensure_ascii=False).encode()
        )
