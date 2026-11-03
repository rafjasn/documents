"""DOCX renderer for synthetic contracts (python-docx), with a logo and the fee table
either as a real table or, for one vendor per world, only as an image."""

import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

from docai.corpus.synthetic.contract_text import ContractText
from docai.corpus.synthetic.render_image import logo, table_image

# Instead of the build time, so a rebuild writes the same bytes.
FILE_DATE = datetime(2026, 1, 1)


def png(image: object) -> BytesIO:
    buffer = BytesIO()
    image.save(buffer, format="PNG")  # type: ignore[attr-defined]
    buffer.seek(0)
    return buffer


def contract_docx(
    text: ContractText, vendor_name: str, fee_table_as_image: bool, path: Path
) -> None:
    document = Document()
    style = document.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(11)
    document.core_properties.title = text.title
    document.core_properties.author = vendor_name
    document.core_properties.created = FILE_DATE
    document.core_properties.modified = FILE_DATE

    header = document.sections[0].header.paragraphs[0]
    header.add_run().add_picture(png(logo(vendor_name, vendor_name)), width=Cm(5))

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(text.title)
    run.bold = True
    run.font.size = Pt(14)
    for line in text.preamble:
        document.add_paragraph(line)
    for section in text.sections:
        document.add_paragraph().add_run(section.heading).bold = True
        for i, paragraph in enumerate(section.paragraphs):
            document.add_paragraph(paragraph)
            if section.fee_table_after == i:
                if fee_table_as_image:
                    document.add_picture(
                        png(table_image(text.fee_header, text.fee_rows)), width=Cm(14)
                    )
                else:
                    table = document.add_table(rows=1, cols=3)
                    table.style = "Table Grid"
                    for cell, value in zip(table.rows[0].cells, text.fee_header):
                        cell.paragraphs[0].add_run(value).bold = True
                    for row in text.fee_rows:
                        for cell, value in zip(table.add_row().cells, row):
                            cell.text = value
    document.add_paragraph()
    signatures = document.add_table(rows=2, cols=2)
    for cell in signatures.rows[0].cells:
        cell.text = "........................................"
    for cell, label in zip(signatures.rows[1].cells, text.signatures):
        cell.text = label
    save_reproducibly(document, path)


def save_reproducibly(document: object, path: Path) -> None:
    """Save with every zip entry dated FILE_DATE; python-docx uses the current time."""
    buffer = BytesIO()
    document.save(buffer)  # type: ignore[attr-defined]
    with (
        zipfile.ZipFile(buffer) as source,
        zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for entry in source.infolist():
            info = zipfile.ZipInfo(entry.filename, FILE_DATE.timetuple()[:6])
            info.compress_type = entry.compress_type
            info.external_attr = entry.external_attr
            target.writestr(info, source.read(entry.filename))
