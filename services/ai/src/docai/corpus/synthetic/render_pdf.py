"""PDF renderers for synthetic invoices and contracts (reportlab)."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from docai.corpus.fonts import register_pdf_fonts
from docai.corpus.synthetic.contract_text import ContractText
from docai.corpus.synthetic.render_image import stamp
from docai.corpus.synthetic.text import INVOICE_LABELS, amount, day, number
from docai.corpus.synthetic.world import Invoice, Party

GRID = colors.Color(0.55, 0.55, 0.55)
HEADER_FILL = colors.Color(0.91, 0.93, 0.95)


@contextmanager
def invariant_pdfs() -> Iterator[None]:
    """ReportLab's invariant mode: fixed dates and document IDs, so a rebuild writes
    the same bytes."""
    previous = rl_config.invariant
    rl_config.invariant = 1
    try:
        yield
    finally:
        rl_config.invariant = previous


def styles() -> dict[str, ParagraphStyle]:
    register_pdf_fonts()
    base = ParagraphStyle("base", fontName="DejaVu", fontSize=9, leading=12)
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10),
        "right": ParagraphStyle("right", parent=base, alignment=TA_RIGHT),
        "bold": ParagraphStyle("bold", parent=base, fontName="DejaVu-Bold"),
        "title": ParagraphStyle(
            "title",
            parent=base,
            fontName="DejaVu-Bold",
            fontSize=16,
            leading=20,
            spaceAfter=8,
        ),
        "centre_title": ParagraphStyle(
            "centre_title",
            parent=base,
            fontName="DejaVu-Bold",
            fontSize=13,
            leading=17,
            alignment=TA_CENTER,
            spaceAfter=10,
        ),
        "heading": ParagraphStyle(
            "heading",
            parent=base,
            fontName="DejaVu-Bold",
            fontSize=10,
            leading=13,
            spaceBefore=8,
            spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "body",
            parent=base,
            fontName="DejaVu-Serif",
            fontSize=10,
            leading=14,
            spaceAfter=5,
        ),
    }


def party_block(label: str, party: Party, tax_label: str, show_country: bool) -> str:
    lines = [
        f"<b>{escape(label)}</b>",
        escape(party.name),
        escape(party.street),
        escape(party.city),
    ]
    if show_country:
        lines.append(escape(party.country))
    lines.append(f"{escape(tax_label)}: {escape(party.tax_id)}")
    return "<br/>".join(lines)


def invoice_pdf(
    invoice: Invoice, contract_number: str, path: Path, notes: Sequence[str] = ()
) -> None:
    """`notes` are extra paragraphs at the end (the prompt-injection fixtures)."""
    s, lang = styles(), invoice.language
    labels = INVOICE_LABELS[lang]
    vendor, buyer = invoice.vendor, invoice.buyer
    foreign = lang == "en"

    meta = Table(
        [
            [labels["issued"], day(invoice.issued, lang)],
            [labels["sale"], day(invoice.issued, lang)],
            [labels["due"], day(invoice.due, lang)],
            [labels["method"], labels["method_value"]],
        ],
        colWidths=(38 * mm, 40 * mm),
        hAlign="RIGHT",
        style=TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "DejaVu", 9),
                ("FONT", (0, 0), (0, -1), "DejaVu-Bold", 9),
            ]
        ),
    )
    parties = Table(
        [
            [
                Paragraph(
                    party_block(labels["seller"], vendor, labels["tax_id"], foreign),
                    s["base"],
                ),
                Paragraph(
                    party_block(labels["buyer"], buyer, "NIP", foreign), s["base"]
                ),
            ]
        ],
        colWidths=(87 * mm, 87 * mm),
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX", (0, 0), (0, 0), 0.5, GRID),
                ("BOX", (1, 0), (1, 0), 0.5, GRID),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        ),
    )
    header_style = ParagraphStyle(
        "column", parent=s["small"], fontName="DejaVu-Bold", fontSize=7, leading=8
    )
    rows: list[list[object]] = [
        [
            Paragraph(escape(label), header_style)
            for label in labels["columns"].split("|")
        ]
    ]
    for i, line in enumerate(invoice.lines, start=1):
        rows.append(
            [
                str(i),
                Paragraph(escape(line.description), s["small"]),
                str(line.quantity),
                line.unit,
                number(line.unit_price, lang),
                number(line.net, lang),
                f"{line.vat_rate}%",
                number(line.vat, lang),
                number(line.gross, lang),
            ]
        )
    items = Table(
        rows,
        colWidths=(
            8 * mm,
            50 * mm,
            12 * mm,
            20 * mm,
            19 * mm,
            19 * mm,
            12 * mm,
            16 * mm,
            19 * mm,
        ),
        repeatRows=1,
        style=TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "DejaVu", 8),
                ("FONT", (0, 0), (-1, 0), "DejaVu-Bold", 7),
                ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
                ("GRID", (0, 0), (-1, -1), 0.4, GRID),
                ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        ),
    )
    totals = Table(
        [
            [labels["total_net"], amount(invoice.net, invoice.currency, lang)],
            [labels["total_vat"], amount(invoice.vat, invoice.currency, lang)],
            [labels["total_gross"], amount(invoice.gross, invoice.currency, lang)],
        ],
        colWidths=(40 * mm, 38 * mm),
        hAlign="RIGHT",
        style=TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "DejaVu", 9),
                ("FONT", (0, 2), (-1, 2), "DejaVu-Bold", 9),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEABOVE", (0, 2), (-1, 2), 0.6, colors.black),
            ]
        ),
    )
    story: list[object] = [
        Paragraph(escape(labels["title"].format(number=invoice.number)), s["title"]),
        meta,
        Spacer(1, 6 * mm),
        parties,
        Spacer(1, 6 * mm),
        items,
        Spacer(1, 4 * mm),
        totals,
        Spacer(1, 5 * mm),
        Paragraph(
            f"<b>{escape(labels['to_pay'])}: {escape(amount(invoice.gross, invoice.currency, lang))}</b>",
            s["base"],
        ),
        Paragraph(
            f"{escape(labels['account'])}: {escape(vendor.bank_account)}", s["base"]
        ),
        Paragraph(escape(labels["contract"].format(number=contract_number)), s["base"]),
    ]
    if invoice.reverse_charge:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(escape(labels["reverse_charge"]), s["small"]))
    for note in notes:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(escape(note), s["small"]))

    def first_page(canvas: Canvas, _: object) -> None:
        if invoice.paid_on is not None:
            image = stamp(lang, invoice.paid_on)
            width = 58 * mm
            height = width * image.height / image.width
            # Below the totals, where it covers no other text.
            canvas.drawImage(
                ImageReader(image),
                A4[0] - width - 30 * mm,
                A4[1] - height - 132 * mm,
                width,
                height,
                mask="auto",
            )

    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=labels["title"].format(number=invoice.number),
        author=vendor.name,
    )
    document.build(story, onFirstPage=first_page)


def contract_pdf(
    text: ContractText, vendor_name: str, language: str, path: Path
) -> None:
    s = styles()
    page_label = "Strona" if language == "pl" else "Page"
    story: list[object] = [Paragraph(escape(text.title), s["centre_title"])]
    story += [Paragraph(escape(line), s["body"]) for line in text.preamble]
    for section in text.sections:
        story.append(Paragraph(escape(section.heading), s["heading"]))
        for i, paragraph in enumerate(section.paragraphs):
            story.append(Paragraph(escape(paragraph), s["body"]))
            if section.fee_table_after == i:
                story.append(fee_table(text))
                story.append(Spacer(1, 3 * mm))
    story.append(Spacer(1, 18 * mm))
    story.append(
        Table(
            [
                [
                    "........................................",
                    "........................................",
                ],
                list(text.signatures),
            ],
            colWidths=(85 * mm, 85 * mm),
            style=TableStyle(
                [
                    ("FONT", (0, 0), (-1, -1), "DejaVu", 9),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ]
            ),
        )
    )

    def footer(canvas: Canvas, document: SimpleDocTemplate) -> None:
        canvas.setFont("DejaVu", 8)
        canvas.drawRightString(
            A4[0] - 20 * mm, 12 * mm, f"{page_label} {document.page}"
        )

    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=22 * mm,
        rightMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=text.title,
        author=vendor_name,
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)


def fee_table(text: ContractText) -> Table:
    return Table(
        [list(text.fee_header), *[list(row) for row in text.fee_rows]],
        colWidths=(80 * mm, 40 * mm, 40 * mm),
        style=TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "DejaVu", 9),
                ("FONT", (0, 0), (-1, 0), "DejaVu-Bold", 9),
                ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
                ("GRID", (0, 0), (-1, -1), 0.4, GRID),
                ("ALIGN", (2, 1), (2, -1), "RIGHT"),
            ]
        ),
    )
