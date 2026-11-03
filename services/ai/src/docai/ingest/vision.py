"""Figure selection and Gemini captions.

Embedded figures and page images (scans and photos) are described by VISION_MODEL so
their content can be searched and cited: text is transcribed, tables become Markdown,
stamps are read with their dates. Small and blank images are skipped before any call;
logos and other images without information come back as DECORATIVE and get no caption.
"""

import base64
import io
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import HumanMessage
from PIL import Image, ImageStat

from docai.ingest.models import Models, Usage
from docai.ingest.parse import FigureImage, PageInfo

DECORATIVE = "DECORATIVE"
MIN_SIDE_PX = 48  # smaller figures are icons, bullets or rules
MIN_PAGE_AREA = 0.005  # figures under 0.5% of the page area are decorative
BLANK_STDDEV = 4.0  # grayscale standard deviation below which an image is blank
MAX_FIGURES = 20  # per document, to bound the cost of image-heavy files
MAX_PAGE_IMAGES = 10  # scanned pages and photos captioned per document
# Gemini 3 bills an image by resolution level. Small figures read as well at the low
# level (about 250 tokens instead of about 1,100 for a stamp or a fee table, checked
# 2026-10-12); page images keep the default so dense scans stay legible.
LOW_RESOLUTION_MAX_SIDE = 1024

PROMPT = """You describe one image from a business document so it can be found by search and used to answer questions.
The image is {where}.
- Transcribe all visible text exactly as written, in its original language, with every number, date and amount.
- If the image is a table, transcribe it as a Markdown table.
- If it is a stamp, give its text, date and colour.
- If it is a photo or scan of a whole page, transcribe it from top to bottom, then mention any stamps, signatures or handwriting.
- End with one sentence in English saying what the image shows.
- If the image carries no information (a logo without text, a decorative graphic or a blank area), reply with exactly: DECORATIVE
Reply with the description only."""

WHERE = {
    "figure": "a figure embedded in the document",
    "page": "a scanned page or photo of a whole document page",
}


@dataclass(frozen=True)
class Caption:
    figure_id: str
    text: str | None  # None when decorative or skipped
    status: Literal["captioned", "decorative", "small", "blank", "duplicate", "limit"]
    usage: Usage | None = None


def is_blank(figure: FigureImage) -> bool:
    """Nearly uniform pixels: a blank page or an empty box."""
    with Image.open(io.BytesIO(figure.data)) as image:
        return max(ImageStat.Stat(image.convert("L")).stddev) < BLANK_STDDEV


def skip_reason(
    figure: FigureImage, page: PageInfo | None
) -> Literal["small", "blank"] | None:
    if min(figure.width, figure.height) < MIN_SIDE_PX:
        return "small"
    if is_blank(figure):
        return "blank"
    if figure.kind == "figure" and page is not None and page.width and page.height:
        # Figure pixels are rendered at 2x page units.
        area = (figure.width / 2) * (figure.height / 2)
        if area < MIN_PAGE_AREA * page.width * page.height:
            return "small"
    return None


def caption_messages(figure: FigureImage) -> list[HumanMessage]:
    image: dict[str, str] = {
        "type": "image",
        "base64": base64.b64encode(figure.data).decode(),
        "mime_type": figure.mime_type,
    }
    if max(figure.width, figure.height) <= LOW_RESOLUTION_MAX_SIDE:
        image["media_resolution"] = "MEDIA_RESOLUTION_LOW"
    text = {"type": "text", "text": PROMPT.format(where=WHERE[figure.kind])}
    return [HumanMessage(content=[text, image])]


def plan_captions(
    figures: list[FigureImage], pages: list[PageInfo]
) -> tuple[list[FigureImage], list[Caption]]:
    """Split figures into those to caption and those skipped, with the reasons."""
    by_page = {page.page_no: page for page in pages}
    to_caption: list[FigureImage] = []
    skipped: list[Caption] = []
    seen: set[bytes] = set()
    counts = {"figure": 0, "page": 0}
    limits = {"figure": MAX_FIGURES, "page": MAX_PAGE_IMAGES}
    for figure in figures:
        page = by_page.get(figure.page_no) if figure.page_no else None
        reason = skip_reason(figure, page)
        if reason is not None:
            skipped.append(Caption(figure.figure_id, None, reason))
        elif figure.data in seen:
            skipped.append(Caption(figure.figure_id, None, "duplicate"))
        elif counts[figure.kind] >= limits[figure.kind]:
            skipped.append(Caption(figure.figure_id, None, "limit"))
        else:
            seen.add(figure.data)
            counts[figure.kind] += 1
            to_caption.append(figure)
    return to_caption, skipped


def caption(
    models: Models, spec: str, figure: FigureImage, metadata: dict[str, Any]
) -> Caption:
    reply = models.call(
        spec,
        list(caption_messages(figure)),
        max_tokens=2048 if figure.kind == "page" else 1024,
        run_name="ingest-caption",
        metadata={**metadata, "figure_id": figure.figure_id},
    )
    text = reply.text.strip()
    if not text or text.strip(".").upper() == DECORATIVE:
        return Caption(figure.figure_id, None, "decorative", reply.usage)
    return Caption(figure.figure_id, text, "captioned", reply.usage)
