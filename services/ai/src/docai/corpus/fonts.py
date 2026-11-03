"""DejaVu fonts, which cover Polish characters, for PDFs and rendered images."""

import tarfile
from functools import cache
from pathlib import Path

from PIL import ImageFont

from docai.corpus.manifest import RAW_DIR

ARCHIVE = RAW_DIR / "dejavu-fonts-ttf-2.37.tar.bz2"
ARCHIVE_URL = (
    "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/"
    "version_2_37/dejavu-fonts-ttf-2.37.tar.bz2"
)
ARCHIVE_SHA256 = "fa9ca4d13871dd122f61258a80d01751d603b4d3ee14095d65453b4e846e17d7"
FONT_DIR = RAW_DIR / "fonts"
FONTS = (
    "DejaVuSans.ttf",
    "DejaVuSans-Bold.ttf",
    "DejaVuSansMono.ttf",
    "DejaVuSerif.ttf",
)


def ensure_fonts() -> Path:
    """Extract the fonts from the downloaded archive (see `docai corpus build`)."""
    if all((FONT_DIR / name).exists() for name in FONTS):
        return FONT_DIR
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(ARCHIVE, "r:bz2") as archive:
        for member in archive.getmembers():
            name = Path(member.name).name
            if name in FONTS:
                extracted = archive.extractfile(member)
                assert extracted is not None
                (FONT_DIR / name).write_bytes(extracted.read())
    return FONT_DIR


@cache
def register_pdf_fonts() -> None:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    directory = ensure_fonts()
    pdfmetrics.registerFont(TTFont("DejaVu", str(directory / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(
        TTFont("DejaVu-Bold", str(directory / "DejaVuSans-Bold.ttf"))
    )
    pdfmetrics.registerFont(TTFont("DejaVu-Serif", str(directory / "DejaVuSerif.ttf")))


@cache
def image_font(
    size: int, *, bold: bool = False, mono: bool = False
) -> ImageFont.FreeTypeFont:
    name = (
        "DejaVuSansMono.ttf"
        if mono
        else "DejaVuSans-Bold.ttf"
        if bold
        else "DejaVuSans.ttf"
    )
    return ImageFont.truetype(str(ensure_fonts() / name), size)
