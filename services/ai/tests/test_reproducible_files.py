"""The corpus renderers write the same bytes on every build, so the committed manifest's
hashes hold on a fresh clone. These use built-in fonts: CI has no corpus fonts."""

import zipfile
from io import BytesIO
from pathlib import Path

from docx import Document
from reportlab.pdfgen.canvas import Canvas

from docai.corpus.synthetic.render_docx import FILE_DATE, save_reproducibly
from docai.corpus.synthetic.render_image import scan_pdf
from docai.corpus.synthetic.render_pdf import invariant_pdfs


def pdf(path: Path) -> bytes:
    canvas = Canvas(str(path))
    canvas.drawString(72, 720, "Invoice FV/2026/01/001, total 1 230,00 PLN")
    canvas.save()
    return path.read_bytes()


def test_reportlab_pdfs_repeat_byte_for_byte(tmp_path: Path) -> None:
    with invariant_pdfs():
        assert pdf(tmp_path / "a.pdf") == pdf(tmp_path / "b.pdf")


def test_docx_files_repeat_byte_for_byte(tmp_path: Path) -> None:
    def docx(path: Path) -> bytes:
        document = Document()
        document.core_properties.created = FILE_DATE
        document.core_properties.modified = FILE_DATE
        document.add_paragraph("Umowa nr 75/2025")
        save_reproducibly(document, path)
        return path.read_bytes()

    first = docx(tmp_path / "a.docx")
    assert first == docx(tmp_path / "b.docx")
    with zipfile.ZipFile(BytesIO(first)) as archive:
        assert {e.date_time for e in archive.infolist()} == {(2026, 1, 1, 0, 0, 0)}


def test_scanned_pdfs_carry_a_fixed_date(tmp_path: Path) -> None:
    with invariant_pdfs():
        pdf(tmp_path / "source.pdf")
    scan_pdf(tmp_path / "source.pdf", tmp_path / "scan.pdf", "seed")

    data = (tmp_path / "scan.pdf").read_bytes()
    assert b"/CreationDate (D:20260101000000Z)" in data
    assert b"/ModDate (D:20260101000000Z)" in data
