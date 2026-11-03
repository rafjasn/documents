"""Document thumbnails: page 1 of PDFs and images, a labelled placeholder otherwise.

200x260 PNG, cropped from the top like the NestJS worker's thumbnails. The NestJS
worker drew placeholders for PDFs too; rendering page 1 is new.
"""

import io

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont, ImageOps

from docai.ingest.parse import DOCX, IMAGES, PDF

WIDTH, HEIGHT = 200, 260
PLACEHOLDERS = {
    PDF: ("#dc2626", "PDF"),
    DOCX: ("#2563eb", "DOCX"),
    "text/plain": ("#475569", "TXT"),
    "text/csv": ("#16a34a", "CSV"),
}


def cover(image: Image.Image) -> Image.Image:
    """Scale to fill 200x260, keeping the top of the page."""
    return ImageOps.fit(
        image.convert("RGB"),
        (WIDTH, HEIGHT),
        Image.Resampling.LANCZOS,
        centering=(0.5, 0),
    )


def placeholder(mime_type: str) -> Image.Image:
    background, label = PLACEHOLDERS.get(mime_type, ("#7c3aed", "FILE"))
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, WIDTH - 1, HEIGHT - 1), radius=10, fill=background)
    lines = [(120, 8), (150, 6), (130, 6), (140, 6), (110, 6), (150, 6), (120, 6)]
    y = 32
    for width, height in lines:
        draw.rounded_rectangle(
            (20, y, 20 + width, y + height), radius=3, fill=(255, 255, 255, 60)
        )
        y += 14 if height == 6 else 20
    draw.rounded_rectangle((55, 228, 145, 250), radius=6, fill=(0, 0, 0, 77))
    font = ImageFont.load_default(size=11)
    draw.text((100, 239), label, fill="white", font=font, anchor="mm")
    return image


def thumbnail(data: bytes, mime_type: str) -> bytes:
    if mime_type == PDF:
        pdf = pdfium.PdfDocument(data)
        try:
            page = pdf[0]
            image = page.render(scale=2 * WIDTH / page.get_width()).to_pil()
        finally:
            pdf.close()
        result = cover(image)
    elif mime_type in IMAGES:
        result = cover(ImageOps.exif_transpose(Image.open(io.BytesIO(data))))
    else:
        result = placeholder(mime_type)
    buffer = io.BytesIO()
    result.save(buffer, format="PNG")
    return buffer.getvalue()
