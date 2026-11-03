"""Tesseract OCR (Polish and English) for photos and pages without a text layer.

Pages are read whole, here rather than inside Docling: Docling's Tesseract stage runs
orientation detection on each layout region, and on small crops it reports upright
text as upside down, which turns the OCR into noise (checked 2026-10-12, Docling
2.138). Uploads are assumed to be upright.
"""

import threading
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import tesserocr
from PIL import Image

LANGUAGES = "pol+eng"
# Automatic page segmentation keeps columns apart, but on some receipt photos it
# classifies most of the receipt as an image and skips it. Reading the page as a
# single block recovers that text. Both run, and the reading with more confident
# words wins.
PAGE_MODES = (tesserocr.PSM.AUTO, tesserocr.PSM.SINGLE_BLOCK)
CONFIDENT = 60  # Tesseract word confidence, 0-100

Box = tuple[int, int, int, int]  # left, top, right, bottom in image pixels


@dataclass(frozen=True)
class OcrLine:
    text: str
    box: Box
    confidence: float


@dataclass(frozen=True)
class OcrParagraph:
    text: str
    box: Box
    confidence: float


@dataclass(frozen=True)
class OcrResult:
    paragraphs: list[OcrParagraph]
    page_mode: int
    confidence: float  # mean word confidence, 0-100
    confident_words: int

    @property
    def text(self) -> str:
        return "\n\n".join(paragraph.text for paragraph in self.paragraphs)


def group_lines(lines: list[OcrLine]) -> list[OcrParagraph]:
    """Join lines into paragraphs, breaking at vertical gaps and column changes."""
    if not lines:
        return []
    height = median(line.box[3] - line.box[1] for line in lines)
    groups: list[list[OcrLine]] = [[lines[0]]]
    for previous, line in zip(lines, lines[1:]):
        gap = line.box[1] - previous.box[3]
        overlaps = line.box[0] < previous.box[2] and previous.box[0] < line.box[2]
        if gap > 0.75 * height or not overlaps:
            groups.append([])
        groups[-1].append(line)
    return [
        OcrParagraph(
            text="\n".join(line.text for line in group),
            box=(
                min(line.box[0] for line in group),
                min(line.box[1] for line in group),
                max(line.box[2] for line in group),
                max(line.box[3] for line in group),
            ),
            confidence=round(sum(line.confidence for line in group) / len(group), 1),
        )
        for group in groups
    ]


class Ocr:
    """One Tesseract engine per process. Calls are serialised; the engine isn't thread-safe."""

    def __init__(self, tessdata: Path) -> None:
        self.tessdata = tessdata
        self._api: tesserocr.PyTessBaseAPI | None = None
        self._lock = threading.Lock()

    def read(self, image: Image.Image) -> OcrResult:
        with self._lock:
            readings = [self._read(image, mode) for mode in PAGE_MODES]
        return max(readings, key=lambda r: (r.confident_words, r.confidence))

    def _engine(self) -> tesserocr.PyTessBaseAPI:
        if self._api is None:
            self._api = tesserocr.PyTessBaseAPI(path=str(self.tessdata), lang=LANGUAGES)
        return self._api

    def _read(self, image: Image.Image, mode: int) -> OcrResult:
        api = self._engine()
        # Tesseract adapts to the pages it has read; clearing that keeps results
        # independent of processing order, so reprocessing a file gives the same text.
        api.ClearAdaptiveClassifier()
        api.SetPageSegMode(mode)
        api.SetImage(image)
        api.Recognize()
        confidences = list(api.AllWordConfidences())
        level = tesserocr.RIL.TEXTLINE
        lines = []
        for item in tesserocr.iterate_level(api.GetIterator(), level):
            try:
                text = (item.GetUTF8Text(level) or "").strip()
            except RuntimeError:  # tesserocr raises for elements without text
                continue
            box = item.BoundingBox(level)
            if text and box:
                lines.append(OcrLine(text, box, round(item.Confidence(level), 1)))
        return OcrResult(
            paragraphs=group_lines(lines),
            page_mode=mode,
            confidence=round(sum(confidences) / len(confidences), 1)
            if confidences
            else 0.0,
            confident_words=sum(c >= CONFIDENT for c in confidences),
        )
