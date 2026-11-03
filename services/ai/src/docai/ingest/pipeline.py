"""The ingestion pipeline for one file, independent of AWS.

`read` parses the file (cached by file hash), captions its figures and detects its
language; `extract` makes the one structured call for category, fields and summary;
`assemble` builds the record stored as parsed/{userId}/{documentId}/document.json.
The worker publishes a status between the steps. `process` runs all three.
"""

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from docling_core.types.doc import (
    DescriptionMetaField,
    PictureItem,
    PictureMeta,
    TableItem,
    TextItem,
)
from pydantic import BaseModel

from docai.core.settings import PipelineSettings
from docai.ingest.cache import DiskCache
from docai.ingest.extract import Extraction, document_text, extract
from docai.ingest.language import detect_language
from docai.ingest.models import Models, Usage
from docai.ingest.parse import (
    FigureImage,
    PageInfo,
    Parser,
    ParseResult,
    resolve_mime_type,
)
from docai.ingest.quarantine import quarantine_document
from docai.ingest.vision import Caption, caption, plan_captions

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
CAPTION_THREADS = 4


class CallRecord(BaseModel):
    role: Literal["caption", "extract"]
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None
    cached: bool
    seconds: float

    @classmethod
    def of(cls, role: Literal["caption", "extract"], usage: Usage) -> CallRecord:
        return cls(
            role=role,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            cached=usage.cached,
            seconds=usage.seconds,
        )


class FigureRecord(BaseModel):
    figure_id: str
    kind: Literal["figure", "page"]
    page_no: int | None
    width: int
    height: int
    file_name: str
    caption: str | None
    caption_status: str


class ParsedDocument(BaseModel):
    """Everything later stages need, so they never parse or call a model again."""

    schema_version: int = SCHEMA_VERSION
    file_name: str
    mime_type: str
    file_sha256: str
    parser: str
    page_count: int
    pages: list[PageInfo]
    language: str
    figures: list[FigureRecord]
    extraction: Extraction
    calls: list[CallRecord]
    warnings: list[str]
    timings: dict[str, float]
    document: dict[str, Any]  # DoclingDocument, with captions as picture descriptions
    # Text addressed to an AI system, cut before any model read it (docai.ingest.quarantine)
    quarantined: list[str] = []

    @property
    def cost_usd(self) -> Decimal:
        """Model cost of processing this file, cached calls included."""
        return sum((c.cost_usd or Decimal(0) for c in self.calls), Decimal(0))


@dataclass
class ReadResult:
    file_name: str
    mime_type: str
    file_sha256: str
    parse: ParseResult
    captions: list[Caption]
    language: str
    text: str
    descriptions: dict[str, str] = field(default_factory=dict)  # picture ref -> caption
    quarantined: list[str] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


def language_text(result: ParseResult, captions: dict[str, str]) -> str:
    """The text to detect the language from.

    On pages without a text layer, the page caption's transcription replaces the OCR
    text: Tesseract turns a poor photo into a few characters of noise, and the Polish
    model fills noise with Polish letters. The caption's closing sentence, always in
    English, is left out.
    """
    transcribed = {}
    for figure in result.figures:
        if figure.kind == "page" and figure.ref in captions:
            transcription = captions[figure.ref].strip().rsplit("\n\n", 1)[0]
            transcribed[figure.page_no] = transcription
    parts = []
    for item, _ in result.document.iterate_items():
        if any(p.page_no in transcribed for p in getattr(item, "prov", [])):
            continue
        if isinstance(item, TextItem):
            parts.append(item.text)
        elif isinstance(item, TableItem):  # a CSV file is one table
            parts.append(" ".join(cell.text for cell in item.data.table_cells))
    return "\n".join([*parts, *transcribed.values()])


class Pipeline:
    def __init__(
        self,
        settings: PipelineSettings,
        models: Models,
        cache: DiskCache | None = None,
        parser: Parser | None = None,
    ) -> None:
        self.settings, self.models, self.cache = settings, models, cache
        self.parser = parser or Parser(settings)

    def is_cached(self, data: bytes) -> bool:
        """Whether this file's parse is in the cache, so processing it again makes no
        model call."""
        if self.cache is None:
            return False
        return self.cache.parse_dir(hashlib.sha256(data).hexdigest()).exists()

    def parse(self, data: bytes, mime_type: str, file_name: str) -> ParseResult:
        digest = hashlib.sha256(data).hexdigest()
        if self.cache is not None and (hit := self.cache.get_parse(digest)) is not None:
            return hit
        result = self.parser.parse(data, mime_type, file_name)
        if self.cache is not None:
            self.cache.put_parse(digest, result)
        return result

    def read(
        self, data: bytes, mime_type: str, file_name: str, metadata: dict[str, Any]
    ) -> ReadResult:
        started = time.monotonic()
        mime_type = resolve_mime_type(mime_type, file_name)
        parsed = self.parse(data, mime_type, file_name)
        parsed_at = time.monotonic()

        to_caption, skipped = plan_captions(parsed.figures, parsed.pages)
        with ThreadPoolExecutor(CAPTION_THREADS) as pool:
            captioned = list(
                pool.map(
                    lambda figure: caption(
                        self.models, self.settings.vision_model, figure, metadata
                    ),
                    to_caption,
                )
            )
        captions = captioned + skipped
        by_id = {c.figure_id: c for c in captions}
        # Duplicates of a captioned image share its caption.
        first_by_data: dict[bytes, str] = {}
        for figure in parsed.figures:
            first_by_data.setdefault(figure.data, figure.figure_id)
        texts: dict[str, str] = {}
        for figure in parsed.figures:
            source = by_id.get(first_by_data[figure.data])
            if source is not None and source.text:
                texts[figure.ref] = source.text
        # Before any model reads the document: extraction, contexts, search, answers.
        quarantined = quarantine_document(parsed.document, texts)
        for picture in parsed.document.pictures:
            if picture.self_ref in texts:
                set_description(
                    picture, texts[picture.self_ref], self.settings.vision_model
                )

        language = detect_language(language_text(parsed, texts))
        return ReadResult(
            file_name=file_name,
            mime_type=mime_type,
            file_sha256=hashlib.sha256(data).hexdigest(),
            parse=parsed,
            captions=captions,
            language=language,
            text=document_text(parsed.document, texts),
            descriptions=texts,
            quarantined=quarantined,
            calls=[CallRecord.of("caption", c.usage) for c in captioned if c.usage],
            timings={
                "parse": round(parsed_at - started, 2),
                "captions": round(time.monotonic() - parsed_at, 2),
            },
        )

    def extract(
        self, read: ReadResult, metadata: dict[str, Any]
    ) -> tuple[Extraction, Usage]:
        started = time.monotonic()
        extraction, usage = extract(
            self.models, self.settings.extract_model, read.text, read.language, metadata
        )
        read.timings["extract"] = round(time.monotonic() - started, 2)
        return extraction, usage

    def assemble(
        self, read: ReadResult, extraction: Extraction, usage: Usage
    ) -> ParsedDocument:
        by_id = {c.figure_id: c for c in read.captions}
        figures = [
            FigureRecord(
                figure_id=f.figure_id,
                kind=f.kind,
                page_no=f.page_no,
                width=f.width,
                height=f.height,
                file_name=f.file_name,
                caption=read.descriptions.get(f.ref),
                caption_status=by_id[f.figure_id].status
                if f.figure_id in by_id
                else "duplicate",
            )
            for f in read.parse.figures
        ]
        return ParsedDocument(
            file_name=read.file_name,
            mime_type=read.mime_type,
            file_sha256=read.file_sha256,
            parser=read.parse.parser,
            page_count=len(read.parse.pages),
            pages=read.parse.pages,
            language=read.language,
            figures=figures,
            extraction=extraction,
            calls=[*read.calls, CallRecord.of("extract", usage)],
            warnings=[
                *read.parse.warnings,
                *extraction.warnings,
                *(
                    [
                        f"removed {len(read.quarantined)} passages addressed to an AI system"
                    ]
                    if read.quarantined
                    else []
                ),
            ],
            timings=read.timings,
            document=read.parse.document.export_to_dict(),
            quarantined=read.quarantined,
        )

    def process(
        self,
        data: bytes,
        mime_type: str,
        file_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ParsedDocument, list[FigureImage]]:
        read = self.read(data, mime_type, file_name, metadata or {})
        extraction, usage = self.extract(read, metadata or {})
        return self.assemble(read, extraction, usage), read.parse.figures


def set_description(picture: PictureItem, text: str, model: str) -> None:
    meta = picture.meta or PictureMeta()
    meta.description = DescriptionMetaField(text=text, created_by=model)
    picture.meta = meta
