"""Raster renderers: stamps, logos, table images, receipt photos and scanned PDFs."""

import random
import time
from datetime import date
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from docai.corpus.fonts import image_font
from docai.corpus.synthetic.text import STAMP_TEXT, amount, day, number, quantity
from docai.corpus.synthetic.world import Receipt

RED = (196, 32, 38)
# Written into scanned PDFs instead of the build time (2026-01-01), for identical rebuilds.
FILE_DATE = time.gmtime(1767225600)


def stamp(language: str, paid_on: date) -> Image.Image:
    """A red PAID/ZAPŁACONO stamp with the payment date, rotated, on a transparent layer."""
    title_font, date_font = image_font(64, bold=True), image_font(34, bold=True)
    title = STAMP_TEXT["pl" if language == "pl" else "en"]
    when = (
        day(paid_on, "pl") if language == "pl" else paid_on.strftime("%d %b %Y").upper()
    )
    width = int(max(title_font.getbbox(title)[2], date_font.getbbox(when)[2])) + 80
    layer = Image.new("RGBA", (width, 190), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    colour = (*RED, 215)
    draw.rounded_rectangle((4, 4, width - 4, 186), radius=18, outline=colour, width=7)
    draw.rounded_rectangle(
        (16, 16, width - 16, 174), radius=12, outline=colour, width=3
    )
    draw.text((width // 2, 70), title, font=title_font, fill=colour, anchor="mm")
    draw.text((width // 2, 138), when, font=date_font, fill=colour, anchor="mm")
    return layer.rotate(12, expand=True, resample=Image.Resampling.BICUBIC)


def logo(name: str, seed: str) -> Image.Image:
    rng = random.Random(seed)
    colour = (rng.randint(20, 120), rng.randint(40, 140), rng.randint(90, 180))
    initials = "".join(word[0] for word in name.split()[:2]).upper()
    image = Image.new("RGB", (420, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, 120, 120), radius=24, fill=colour)
    draw.text(
        (60, 60), initials, font=image_font(52, bold=True), fill="white", anchor="mm"
    )
    draw.text(
        (140, 60),
        name.split(" ")[0],
        font=image_font(40, bold=True),
        fill=colour,
        anchor="lm",
    )
    return image


def table_image(
    header: tuple[str, ...], rows: tuple[tuple[str, ...], ...]
) -> Image.Image:
    """A ruled table drawn as pixels: its text is not in the document's text layer."""
    font, bold = image_font(26), image_font(26, bold=True)
    cells = [header, *rows]
    widths = [
        int(
            max(
                bold.getbbox(row[i])[2] if r == 0 else font.getbbox(row[i])[2]
                for r, row in enumerate(cells)
            )
        )
        + 40
        for i in range(len(header))
    ]
    height = 56
    image = Image.new("RGB", (sum(widths) + 2, height * len(cells) + 2), "white")
    draw = ImageDraw.Draw(image)
    y = 0
    for r, row in enumerate(cells):
        x = 0
        if r == 0:
            draw.rectangle((0, 0, sum(widths), height), fill=(232, 236, 242))
        for i, text in enumerate(row):
            draw.rectangle(
                (x, y, x + widths[i], y + height), outline=(90, 90, 90), width=2
            )
            draw.text(
                (x + 20, y + height // 2),
                text,
                font=bold if r == 0 else font,
                fill="black",
                anchor="lm",
            )
            x += widths[i]
        y += height
    return image


def receipt_lines(receipt: Receipt) -> list[tuple[str, str]]:
    """(text, alignment) pairs; alignment is "c" (centre), "l" (left) or "lr" (left|right)."""
    m, lang = receipt.merchant, receipt.language
    rule = ("-" * 34, "c")
    lines: list[tuple[str, str]] = [
        (m.name, "c"),
        (m.street, "c"),
        (m.city, "c"),
        (f"NIP {m.tax_id}" if lang == "pl" else f"VAT Reg No. {m.tax_id}", "c"),
    ]
    when = receipt.issued.strftime("%d.%m.%Y" if lang == "pl" else "%d/%m/%Y")
    lines.append(
        (
            f"{when} {receipt.time}|{'nr' if lang == 'pl' else 'No.'} {receipt.number}",
            "lr",
        )
    )
    lines.append(("PARAGON FISKALNY" if lang == "pl" else "RECEIPT", "c"))
    lines.append(rule)
    for item in receipt.items:
        lines.append((item.description, "l"))
        unit = " l" if item.quantity != item.quantity.to_integral_value() else ""
        suffix = " A" if lang == "pl" else ""
        lines.append(
            (
                f"  {quantity(item.quantity, lang)}{unit} x {number(item.unit_price, lang)}|{number(item.amount, lang)}{suffix}",
                "lr",
            )
        )
    lines.append(rule)
    total = amount(receipt.total, receipt.currency, lang)
    if lang == "pl":
        lines += [
            (f"SPRZEDAŻ OPODATK. A|{number(receipt.total, lang)}", "lr"),
            (f"PTU A {receipt.vat_rate}%|{number(receipt.vat, lang)}", "lr"),
            (f"SUMA PTU|{number(receipt.vat, lang)}", "lr"),
            (f"SUMA PLN|{number(receipt.total, lang)}", "lr"),
            rule,
            (
                f"{'Karta płatnicza' if receipt.payment == 'card' else 'Gotówka'}|{number(receipt.total, lang)}",
                "lr",
            ),
        ]
    else:
        lines += [
            (f"TOTAL|{total}", "lr"),
            (
                f"VAT {receipt.vat_rate}% included|{amount(receipt.vat, receipt.currency, lang)}",
                "lr",
            ),
            (
                f"{'Paid by card' if receipt.payment == 'card' else 'Paid in cash'}|{total}",
                "lr",
            ),
            rule,
            ("Thank you for your visit", "c"),
        ]
    return lines


def receipt_paper(receipt: Receipt) -> Image.Image:
    font = image_font(24, mono=True)
    lines = receipt_lines(receipt)
    width, line_height, margin = 560, 34, 30
    paper = Image.new(
        "RGB", (width, margin * 2 + line_height * len(lines)), (250, 249, 244)
    )
    draw = ImageDraw.Draw(paper)
    for i, (text, align) in enumerate(lines):
        y = margin + i * line_height
        if align == "c":
            draw.text((width // 2, y), text, font=font, fill=(35, 35, 40), anchor="mt")
        elif align == "lr":
            left, right = text.split("|")
            draw.text((margin, y), left, font=font, fill=(35, 35, 40))
            draw.text(
                (width - margin, y), right, font=font, fill=(35, 35, 40), anchor="rt"
            )
        else:
            draw.text((margin, y), text, font=font, fill=(35, 35, 40))
    return paper


def photograph(paper: Image.Image, seed: str) -> Image.Image:
    """Turn a flat page into a handheld phone photo: desk, tilt, shadow, uneven light, blur."""
    rng = random.Random(seed)
    w, h = paper.size
    desk = Image.new(
        "RGB",
        (w + 360, h + 360),
        (rng.randint(95, 140), rng.randint(80, 110), rng.randint(60, 85)),
    )
    desk = Image.blend(desk, Image.effect_noise(desk.size, 30).convert("RGB"), 0.12)
    shadow = Image.new("L", desk.size, 0)
    ImageDraw.Draw(shadow).rectangle((190, 196, 190 + w, 196 + h), fill=110)
    desk.paste((30, 25, 20), mask=shadow.filter(ImageFilter.GaussianBlur(18)))
    desk.paste(paper, (180, 180))
    tilted = desk.rotate(
        rng.uniform(-6, 6), resample=Image.Resampling.BICUBIC, fillcolor=(70, 60, 50)
    )
    shear = rng.uniform(-0.05, 0.05)
    tilted = tilted.transform(
        tilted.size,
        Image.Transform.AFFINE,
        (1, shear, -shear * tilted.size[1] / 2, 0, 1, 0),
        resample=Image.Resampling.BICUBIC,
        fillcolor=(70, 60, 50),
    )
    direction = rng.choice(list(Image.Transpose)[:4])
    light = Image.linear_gradient("L").transpose(direction).resize(tilted.size)
    light = light.point(lambda p: 165 + p * 90 // 255)
    lit = ImageChops.multiply(tilted, Image.merge("RGB", (light, light, light)))
    return lit.filter(ImageFilter.GaussianBlur(rng.uniform(0.6, 1.2)))


def scan_pdf(source: Path, target: Path, seed: str) -> int:
    """Re-render a PDF as a slightly skewed, noisy grey scan with no text layer."""
    rng = random.Random(seed)
    document = pdfium.PdfDocument(source)
    pages = []
    for page in document:
        image = page.render(scale=150 / 72).to_pil().convert("L")
        image = image.rotate(
            rng.uniform(-1.2, 1.2), resample=Image.Resampling.BICUBIC, fillcolor=255
        )
        noise = Image.effect_noise(image.size, 22)
        image = Image.blend(image, noise, 0.08).filter(ImageFilter.GaussianBlur(0.7))
        pages.append(ImageOps.autocontrast(image, cutoff=1))
    document.close()
    pages[0].save(
        target,
        "PDF",
        resolution=150,
        save_all=True,
        append_images=pages[1:],
        creationDate=FILE_DATE,
        modDate=FILE_DATE,
    )
    return len(pages)
