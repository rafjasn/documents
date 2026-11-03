"""Parse an uploaded file into a DoclingDocument.

Routes, chosen per file and, for PDFs, per page:

- Born-digital PDF pages and DOCX: Docling (layout, reading order, TableFormer tables,
  embedded figures). Docling OCRs only regions without a text layer, such as images
  of text (docai.ingest.docling_ocr).
- Images and PDF pages without a text layer: Tesseract reads the whole page
  (docai.ingest.ocr), and the page image is kept as a figure for a vision caption,
  which also picks up stamps and handwriting.
- TXT and CSV: built directly, as paragraphs or one table.

Figure images are returned separately and referenced from the document by relative
path, so the document JSON stays small.
"""

import csv
import gc
import io
import logging
import mimetypes
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pypdfium2 as pdfium
from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DocItemLabel,
    DoclingDocument,
    ImageRef,
    PictureItem,
    ProvenanceItem,
    Size,
    TableCell,
    TableData,
)
from PIL import Image, ImageOps
from pydantic import BaseModel

from docai.core.settings import PipelineSettings
from docai.ingest.ocr import Ocr, OcrResult
from docai.ingest.setup import ensure_tessdata

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter

logger = logging.getLogger(__name__)

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
IMAGES = ("image/png", "image/jpeg", "image/webp")
TEXT = "text/plain"
CSV = "text/csv"
SUPPORTED = (PDF, DOCX, *IMAGES, TEXT, CSV)

SCAN_DPI = 300  # rendering resolution for OCR of pages without a text layer
MIN_TEXT_LAYER_CHARS = (
    20  # fewer characters than this on a page counts as no text layer
)
FIGURE_SCALE = 2.0  # Docling renders figure crops at 144 DPI
PAGE_IMAGE_MAX_SIDE = 1600  # page images kept for captions and previews
MAX_CSV_ROWS = 5000
# Docling's peak memory grows by about 30 MB per page of a document (1.5 GB at 14 pages,
# 1.9 GB at 30, measured in the worker container on 2026-10-12), so long PDFs are
# converted in windows of this many pages and joined.
PAGE_WINDOW = 10
DOCUMENT_TIMEOUT_SECONDS = 600.0


class UnsupportedFileError(ValueError):
    pass


class PageInfo(BaseModel):
    page_no: int
    width: float
    height: float
    text_layer: bool
    ocr_confidence: float | None = None
    ocr_page_mode: int | None = None


@dataclass(frozen=True)
class FigureImage:
    """A figure's image, stored next to the document as figures/<figure_id>.<ext>."""

    figure_id: str
    ref: str  # the DoclingDocument picture, e.g. "#/pictures/2"
    kind: Literal["figure", "page"]  # embedded figure, or a whole scanned page or photo
    page_no: int | None
    width: int
    height: int
    mime_type: str
    data: bytes

    @property
    def file_name(self) -> str:
        return f"{self.figure_id}.{'png' if self.mime_type == 'image/png' else 'jpg'}"


@dataclass
class ParseResult:
    document: DoclingDocument
    parser: str
    pages: list[PageInfo]
    figures: list[FigureImage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def resolve_mime_type(mime_type: str, file_name: str) -> str:
    """The upload's content type, or a guess from the file name for generic types."""
    mime_type = mime_type.split(";")[0].strip().lower()
    if mime_type in SUPPORTED:
        return mime_type
    guessed = mimetypes.guess_type(file_name)[0]
    if guessed in SUPPORTED:
        return guessed
    raise UnsupportedFileError(f"Unsupported file type {mime_type!r} ({file_name})")


def figure_id(ref: str) -> str:
    """'#/pictures/3' -> 'picture-3'."""
    return "picture-" + ref.rsplit("/", 1)[-1]


def encode(image: Image.Image, mime_type: str) -> bytes:
    buffer = io.BytesIO()
    if mime_type == "image/png":
        image.save(buffer, format="PNG")
    else:
        image.convert("RGB").save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def shrink(image: Image.Image, max_side: int = PAGE_IMAGE_MAX_SIDE) -> Image.Image:
    if max(image.size) <= max_side:
        return image
    scale = max_side / max(image.size)
    return image.resize(
        (round(image.width * scale), round(image.height * scale)),
        Image.Resampling.LANCZOS,
    )


def text_layer_pages(data: bytes) -> list[bool]:
    """For each PDF page, whether it has a usable text layer."""
    pdf = pdfium.PdfDocument(data)
    try:
        result = []
        for page in pdf:
            text = page.get_textpage().get_text_bounded()
            result.append(len("".join(text.split())) >= MIN_TEXT_LAYER_CHARS)
        return result
    finally:
        pdf.close()


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1250"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


class Parser:
    """Docling and Tesseract, loaded on first use and reused for every document."""

    def __init__(self, settings: PipelineSettings) -> None:
        self.settings = settings
        self._converter: DocumentConverter | None = None
        self._ocr: Ocr | None = None
        self._lock = threading.Lock()

    @property
    def ocr(self) -> Ocr:
        if self._ocr is None:
            self._ocr = Ocr(ensure_tessdata(self.settings.tessdata_dir))
        return self._ocr

    def converter(self) -> DocumentConverter:
        with self._lock:
            if self._converter is None:
                self._converter = self._build_converter()
            return self._converter

    def _build_converter(self) -> DocumentConverter:
        from docling.datamodel.accelerator_options import AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.datamodel.settings import settings as docling_settings
        from docling.document_converter import (
            DocumentConverter,
            PdfFormatOption,
            WordFormatOption,
        )

        from docai.ingest.docling_ocr import UprightTesseractOcrOptions, register

        register()
        # One page at a time: batches of four raised peak memory on a 30-page contract
        # from 1.7 GB to 2.5 GB and were no faster on CPU (measured 2026-10-12).
        docling_settings.perf.page_batch_size = 1
        options = PdfPipelineOptions(
            # OCR only regions without a text layer, such as images of text.
            do_ocr=True,
            ocr_options=UprightTesseractOcrOptions(
                lang=["pol", "eng"],
                path=str(ensure_tessdata(self.settings.tessdata_dir)),
            ),
            ocr_batch_size=1,
            do_table_structure=True,
            generate_picture_images=True,
            images_scale=FIGURE_SCALE,
            layout_batch_size=1,
            table_batch_size=1,
            document_timeout=DOCUMENT_TIMEOUT_SECONDS,
            artifacts_path=self.settings.docling_artifacts_path,
            accelerator_options=AcceleratorOptions(
                num_threads=self.settings.ingest_threads, device="cpu"
            ),
        )
        # Pipelines load on first use: DOCX conversion needs none of the PDF models.
        return DocumentConverter(
            allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=options),
                InputFormat.DOCX: WordFormatOption(),
            },
        )

    def parse(self, data: bytes, mime_type: str, file_name: str) -> ParseResult:
        mime_type = resolve_mime_type(mime_type, file_name)
        name = Path(file_name).stem or "document"
        if mime_type == PDF:
            return self._parse_pdf(data, file_name, name)
        if mime_type == DOCX:
            return self._docling(data, file_name, parser="docling", pages=[])
        if mime_type in IMAGES:
            return self._parse_image(data, name)
        if mime_type == CSV:
            return parse_csv(decode_text(data), name)
        return parse_text(decode_text(data), name)

    # PDF -----------------------------------------------------------------------

    def _parse_pdf(self, data: bytes, file_name: str, name: str) -> ParseResult:
        text_layer = text_layer_pages(data)
        if not text_layer:
            raise ValueError("The PDF has no pages")
        if not any(text_layer):
            return self._ocr_pdf(data, name, text_layer)
        pdf = pdfium.PdfDocument(data)
        try:
            pages = [
                PageInfo(
                    page_no=number,
                    width=page.get_width(),
                    height=page.get_height(),
                    text_layer=has_text,
                )
                for number, (page, has_text) in enumerate(zip(pdf, text_layer), 1)
            ]
        finally:
            pdf.close()
        result = self._docling(data, file_name, parser="docling", pages=pages)
        if all(text_layer):
            return result
        # Mixed PDF: replace Docling's (empty) items on scanned pages with OCR text.
        result.parser = "docling+ocr"
        scanned = {page.page_no for page in pages if not page.text_layer}
        stale = [
            item
            for item, _ in result.document.iterate_items()
            if any(p.page_no in scanned for p in getattr(item, "prov", []))
        ]
        stale_refs = {item.self_ref for item in stale}
        result.figures = [f for f in result.figures if f.ref not in stale_refs]
        # Deleting an item deletes its children, so only delete the outermost ones.
        outermost = [
            item
            for item in stale
            if item.parent is None or item.parent.cref not in stale_refs
        ]
        if outermost:
            result.document.delete_items(node_items=outermost)
        pdf = pdfium.PdfDocument(data)
        try:
            for page_info in pages:
                if page_info.page_no in scanned:
                    image = (
                        pdf[page_info.page_no - 1].render(scale=SCAN_DPI / 72).to_pil()
                    )
                    self._add_ocr_page(result, page_info, image, SCAN_DPI / 72)
        finally:
            pdf.close()
        # Picture refs shift after deletion; re-read them from the document.
        result.figures = self._renumber(result)
        return result

    def _ocr_pdf(self, data: bytes, name: str, text_layer: list[bool]) -> ParseResult:
        result = ParseResult(
            document=DoclingDocument(name=name), parser="ocr", pages=[]
        )
        pdf = pdfium.PdfDocument(data)
        try:
            for number, page in enumerate(pdf, 1):
                info = PageInfo(
                    page_no=number,
                    width=page.get_width(),
                    height=page.get_height(),
                    text_layer=text_layer[number - 1],
                )
                result.pages.append(info)
                result.document.add_page(
                    page_no=number, size=Size(width=info.width, height=info.height)
                )
                image = page.render(scale=SCAN_DPI / 72).to_pil()
                self._add_ocr_page(result, info, image, SCAN_DPI / 72)
        finally:
            pdf.close()
        return result

    # Images ----------------------------------------------------------------------

    def _parse_image(self, data: bytes, name: str) -> ParseResult:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        info = PageInfo(
            page_no=1, width=image.width, height=image.height, text_layer=False
        )
        document = DoclingDocument(name=name)
        document.add_page(page_no=1, size=Size(width=image.width, height=image.height))
        result = ParseResult(document=document, parser="ocr", pages=[info])
        self._add_ocr_page(result, info, image, scale=1.0)
        return result

    def _add_ocr_page(
        self, result: ParseResult, page: PageInfo, image: Image.Image, scale: float
    ) -> None:
        """Add OCR paragraphs and the page image. `scale` converts pixels to page units."""
        reading: OcrResult = self.ocr.read(image)
        page.ocr_confidence = reading.confidence
        page.ocr_page_mode = reading.page_mode
        for paragraph in reading.paragraphs:
            left, top, right, bottom = (value / scale for value in paragraph.box)
            result.document.add_text(
                label=DocItemLabel.TEXT,
                text=paragraph.text,
                prov=ProvenanceItem(
                    page_no=page.page_no,
                    bbox=BoundingBox(
                        l=left,
                        t=top,
                        r=right,
                        b=bottom,
                        coord_origin=CoordOrigin.TOPLEFT,
                    ),
                    charspan=(0, len(paragraph.text)),
                ),
            )
        if not reading.paragraphs:
            result.warnings.append(f"page {page.page_no}: OCR found no text")
        page_image = shrink(image)
        picture = result.document.add_picture(
            prov=ProvenanceItem(
                page_no=page.page_no,
                bbox=BoundingBox(
                    l=0,
                    t=0,
                    r=page.width,
                    b=page.height,
                    coord_origin=CoordOrigin.TOPLEFT,
                ),
                charspan=(0, 0),
            )
        )
        self._attach(result, picture, page_image, kind="page", mime_type="image/jpeg")

    # Docling -----------------------------------------------------------------------

    def _docling(
        self, data: bytes, file_name: str, parser: str, pages: list[PageInfo]
    ) -> ParseResult:
        """Convert with Docling; PDFs in windows of PAGE_WINDOW pages, then joined."""
        from docling.datamodel.base_models import ConversionStatus, DocumentStream

        windows: list[tuple[int, int] | None] = [
            (start, min(start + PAGE_WINDOW - 1, len(pages)))
            for start in range(1, len(pages) + 1, PAGE_WINDOW)
        ] or [None]
        documents = []
        warnings = []
        for window in windows:
            options: Any = {"page_range": window} if window else {}
            conversion = self.converter().convert(
                DocumentStream(name=file_name, stream=io.BytesIO(data)),
                raises_on_error=False,
                **options,
            )
            if conversion.status not in (
                ConversionStatus.SUCCESS,
                ConversionStatus.PARTIAL_SUCCESS,
            ):
                errors = "; ".join(e.error_message for e in conversion.errors)
                raise ValueError(f"Docling could not convert the file: {errors}")
            if conversion.status == ConversionStatus.PARTIAL_SUCCESS:
                warnings.append(f"pages {window}: Docling converted them only partly")
            documents.append(conversion.document)
            del conversion
            gc.collect()
        document = (
            documents[0]
            if len(documents) == 1
            else DoclingDocument.concatenate(documents)
        )
        result = ParseResult(
            document=document, parser=parser, pages=pages, warnings=warnings
        )
        for picture in document.pictures:
            image = picture.get_image(document)
            if image is not None:
                self._attach(
                    result, picture, image, kind="figure", mime_type="image/png"
                )
        return result

    @staticmethod
    def _attach(
        result: ParseResult,
        picture: PictureItem,
        image: Image.Image,
        kind: Literal["figure", "page"],
        mime_type: str,
    ) -> None:
        """Keep the image's bytes aside and point the document at figures/<id>."""
        figure = FigureImage(
            figure_id=figure_id(picture.self_ref),
            ref=picture.self_ref,
            kind=kind,
            page_no=picture.prov[0].page_no if picture.prov else None,
            width=image.width,
            height=image.height,
            mime_type=mime_type,
            data=encode(image, mime_type),
        )
        picture.image = ImageRef(
            mimetype=mime_type,
            dpi=round(72 * FIGURE_SCALE) if kind == "figure" else 72,
            size=Size(width=image.width, height=image.height),
            uri=Path("figures") / figure.file_name,
        )
        result.figures = [f for f in result.figures if f.ref != figure.ref] + [figure]

    @staticmethod
    def _renumber(result: ParseResult) -> list[FigureImage]:
        """Match figures to pictures again after items were deleted from the document."""
        by_uri = {Path("figures") / f.file_name: f for f in result.figures}
        figures = []
        for picture in result.document.pictures:
            old = by_uri.get(picture.image.uri) if picture.image else None  # type: ignore[arg-type]
            if old is None:
                continue
            new_id = figure_id(picture.self_ref)
            figure = FigureImage(
                figure_id=new_id,
                ref=picture.self_ref,
                kind=old.kind,
                page_no=old.page_no,
                width=old.width,
                height=old.height,
                mime_type=old.mime_type,
                data=old.data,
            )
            assert picture.image is not None
            picture.image.uri = Path("figures") / figure.file_name
            figures.append(figure)
        return figures


# Plain text and CSV -----------------------------------------------------------------


def parse_text(text: str, name: str) -> ParseResult:
    document = DoclingDocument(name=name)
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if block.strip():
            document.add_text(label=DocItemLabel.TEXT, text=block.strip())
    return ParseResult(document=document, parser="text", pages=[])


def parse_csv(text: str, name: str) -> ParseResult:
    sample = text[:4096]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
            sample, delimiters=",;\t|"
        )
    except csv.Error:
        dialect = csv.excel
    rows = [row for row in csv.reader(io.StringIO(text), dialect) if any(row)]
    result = ParseResult(document=DoclingDocument(name=name), parser="csv", pages=[])
    if len(rows) > MAX_CSV_ROWS:
        result.warnings.append(f"kept the first {MAX_CSV_ROWS} of {len(rows)} rows")
        rows = rows[:MAX_CSV_ROWS]
    width = max((len(row) for row in rows), default=0)
    cells = [
        TableCell(
            text=value.strip(),
            start_row_offset_idx=r,
            end_row_offset_idx=r + 1,
            start_col_offset_idx=c,
            end_col_offset_idx=c + 1,
            column_header=r == 0,
        )
        for r, row in enumerate(rows)
        for c, value in enumerate(row + [""] * (width - len(row)))
    ]
    result.document.add_table(
        data=TableData(num_rows=len(rows), num_cols=width, table_cells=cells)
    )
    return result
