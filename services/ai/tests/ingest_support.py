"""Shared helpers for the ingestion tests: sample files, fake models, settings."""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel

from docai.core.settings import SERVICE_ROOT, PipelineSettings
from docai.ingest.models import Reply, Usage
from docai.ingest.setup import ensure_tessdata

TESSDATA = SERVICE_ROOT / ".cache" / "tessdata"


def pipeline_settings(tmp_path: Path | None = None) -> PipelineSettings:
    return PipelineSettings(
        gemini_api_key="test-key",
        vision_model="google_genai:gemini-3.1-flash-lite",
        extract_model="google_genai:gemini-3.1-flash-lite",
        tessdata_dir=TESSDATA,
        ingest_cache_dir=tmp_path,
    )


def tessdata() -> Path:
    """The OCR models (27 MB, downloaded once into services/ai/.cache/tessdata)."""
    try:
        return ensure_tessdata(TESSDATA)
    except OSError as error:
        pytest.skip(f"OCR models unavailable: {error}")


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def text_image(lines: list[str], size: tuple[int, int] = (1200, 700)) -> Image.Image:
    """Black text on white, large enough for OCR."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(lines):
        draw.text((60, 60 + row * 90), line, fill="black", font=font(48))
    return image


def png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def image_pdf(image: Image.Image) -> bytes:
    """A PDF page that is only an image: a scan without a text layer."""
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=150)
    return buffer.getvalue()


def text_pdf(lines: list[str], stamp: bytes | None = None) -> bytes:
    """A born-digital PDF with a text layer, optionally with an embedded image."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for row, line in enumerate(lines):
        pdf.drawString(72, A4[1] - 72 - row * 18, line)
    if stamp is not None:
        pdf.drawImage(ImageReader(io.BytesIO(stamp)), 300, 300, width=180, height=90)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def mixed_pdf(lines: list[str], scan: Image.Image) -> bytes:
    """Page 1 has a text layer; page 2 is only an image, like a scanned attachment."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for row, line in enumerate(lines):
        pdf.drawString(72, A4[1] - 72 - row * 18, line)
    pdf.showPage()
    width = A4[0] - 72
    height = width * scan.height / scan.width
    pdf.drawImage(
        ImageReader(scan), 36, A4[1] - 36 - height, width=width, height=height
    )
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def pasted_scan_pdf(body_lines: int, scanned: list[str]) -> bytes:
    """A born-digital page with a scanned paragraph pasted in as an image."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    image = Image.new("RGB", (1700, 40 + 80 * len(scanned)), (250, 250, 248))
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(scanned):
        draw.text((20, 20 + row * 80), line, fill=(30, 30, 30), font=font(56))
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for row in range(body_lines):
        pdf.drawString(
            72, A4[1] - 72 - row * 16, f"{row + 1}. Clause {row + 1} of the agreement."
        )
    width = A4[0] - 144
    pdf.drawImage(
        ImageReader(image), 72, 380, width=width, height=width * image.height / 1700
    )
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


EXTRACTION: dict[str, Any] = {
    "category": "INVOICE",
    "confidence": 0.97,
    "summary": "Faktura FV/1 od Acme dla Beta na 123,00 PLN.",
    "metadata": {
        "keywords": ["faktura", "Acme"],
        "tags": ["finanse"],
        "people": ["Jan Kowalski"],
        "organizations": ["Acme Sp. z o.o.", "Beta S.A."],
        "locations": ["Kraków"],
    },
    "invoice": {
        "vendor": {"name": "Acme Sp. z o.o.", "tax_id": "123-456-32-18"},
        "buyer": {"name": "Beta S.A.", "tax_id": None},
        "invoice_number": "FV/1",
        "issue_date": "2026-01-20",
        "due_date": "2026-02-19",
        "currency": "zł",
        "net_total": 100.0,
        "tax_total": 23.0,
        "gross_total": 123.0,
        "paid_date": None,
        "line_items": [
            {
                "description": "Usługa",
                "quantity": 1,
                "unit_price": 100.0,
                "net_amount": 100.0,
                "tax_rate": 23,
            }
        ],
    },
    "receipt": None,
    "contract": None,
}


STAMP_CAPTION = "ZAPŁACONO 16.02.2026. A red stamp marking the invoice as paid."


class FakeModels:
    """Captions by figure ID (STAMP_CAPTION by default) and a fixed extraction.

    Records every call; captions run in threads, so lookups don't depend on order.
    """

    def __init__(
        self,
        captions: dict[str, str] | None = None,
        extraction: dict[str, Any] | None = None,
    ) -> None:
        self.captions = captions or {}
        self.extraction = extraction or EXTRACTION
        self.calls: list[tuple[str, list[BaseMessage], dict[str, Any]]] = []

    def call(
        self,
        spec: str,
        messages: list[BaseMessage],
        *,
        max_tokens: int,
        run_name: str,
        metadata: dict[str, Any],
        schema: type[BaseModel] | None = None,
    ) -> Reply:
        self.calls.append((run_name, messages, metadata))
        if schema is None:
            text = self.captions.get(metadata.get("figure_id", ""), STAMP_CAPTION)
        else:
            text = json.dumps(self.extraction)
        return Reply(text, Usage(spec, 1000, 100, 0.1, cached=False))

    def runs(self, name: str) -> int:
        return sum(run == name for run, _, _ in self.calls)
