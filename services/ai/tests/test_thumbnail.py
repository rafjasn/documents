import io

import pytest
from PIL import Image, ImageStat

from docai.ingest.parse import DOCX, PDF
from docai.ingest.thumbnail import HEIGHT, WIDTH, thumbnail
from tests.ingest_support import png, text_image, text_pdf


def size_and_mode(data: bytes) -> tuple[tuple[int, int], str]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size, image.format or ""


@pytest.mark.parametrize(
    ("data", "mime_type"),
    [
        (text_pdf(["Faktura VAT nr FV/2026/01/319"]), PDF),
        (png(text_image(["Paragon"], (920, 964))), "image/png"),
        (b"PK\x03\x04", DOCX),
        (b"Data;Kwota\n", "text/csv"),
    ],
    ids=["pdf-page-1", "photo", "docx-placeholder", "csv-placeholder"],
)
def test_every_type_gets_a_200x260_png(data: bytes, mime_type: str) -> None:
    assert size_and_mode(thumbnail(data, mime_type)) == ((WIDTH, HEIGHT), "PNG")


def test_pdf_thumbnails_show_the_top_of_page_one() -> None:
    with Image.open(io.BytesIO(thumbnail(text_pdf(["Faktura"] * 3), PDF))) as image:
        top, bottom = image.crop((0, 0, WIDTH, 60)), image.crop((0, 200, WIDTH, HEIGHT))
        # The text sits at the top of the page; the bottom of the crop is blank.
        assert ImageStat.Stat(top.convert("L")).extrema[0][0] < 128
        assert ImageStat.Stat(bottom.convert("L")).extrema[0][0] > 200
