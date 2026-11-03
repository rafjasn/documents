"""Docling's Tesseract stage without orientation detection, for born-digital PDFs.

Docling OCRs only the regions of a page that have no text layer, such as a scanned
signature page or a screenshot pasted into a PDF. Its Tesseract stage runs orientation
detection on every region, and on small crops that reports upright text as upside
down, which turns the OCR into noise (checked 2026-10-12, Docling 2.138). This variant
treats every region as upright. Pages without any text layer don't come here; they are
read whole by docai.ingest.ocr.
"""

from typing import Any, ClassVar, Literal

from docling.datamodel.pipeline_options import OcrOptions, TesseractOcrOptions
from docling.models.factories import get_ocr_factory
from docling.models.stages.ocr.tesseract_ocr_model import TesseractOcrModel


class UprightTesseractOcrOptions(TesseractOcrOptions):
    kind: ClassVar[Literal["tesserocr_upright"]] = "tesserocr_upright"  # type: ignore[assignment]


class _Upright:
    """Stands in for Tesseract's orientation detector: every region is upright."""

    def SetImage(self, image: object) -> None:  # noqa: N802 - tesserocr's API
        pass

    def DetectOrientationScript(self) -> dict[str, Any]:  # noqa: N802
        return {"orient_deg": 0, "script_name": "Latin"}

    def End(self) -> None:  # noqa: N802
        pass


class UprightTesseractOcrModel(TesseractOcrModel):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Docling sets the reader only when OCR is enabled.
        reader = getattr(self, "_osd_reader", None)
        if reader is not None:
            reader.End()
            self._osd_reader = _Upright()

    @classmethod
    def get_options_type(cls) -> type[OcrOptions]:
        return UprightTesseractOcrOptions


def register() -> None:
    """Make the variant available to Docling pipelines. Safe to call more than once."""
    factory = get_ocr_factory(allow_external_plugins=False)
    if UprightTesseractOcrOptions not in factory.classes:
        factory.register(UprightTesseractOcrModel, "docai", __name__)
