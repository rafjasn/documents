import io

import pytest
from docling_core.types.doc import CoordOrigin, PictureItem, TableItem, TextItem
from PIL import Image

from docai.ingest import parse as parse_module
from docai.ingest.ocr import OcrLine, group_lines
from docai.ingest.parse import (
    DOCX,
    PDF,
    Parser,
    UnsupportedFileError,
    parse_csv,
    parse_text,
    resolve_mime_type,
    text_layer_pages,
)
from tests.ingest_support import (
    image_pdf,
    mixed_pdf,
    pasted_scan_pdf,
    pipeline_settings,
    png,
    tessdata,
    text_image,
    text_pdf,
)


def texts(result: parse_module.ParseResult) -> list[str]:
    return [
        item.text
        for item, _ in result.document.iterate_items()
        if isinstance(item, TextItem)
    ]


def test_lines_group_into_paragraphs_at_gaps_and_column_changes() -> None:
    lines = [
        OcrLine("Sprzedawca", (100, 100, 400, 130), 95),
        OcrLine("Acme Sp. z o.o.", (100, 135, 420, 165), 90),
        OcrLine("Razem", (100, 300, 300, 330), 92),  # a gap: new paragraph
        OcrLine("Nabywca", (900, 300, 1200, 330), 94),  # another column
    ]
    paragraphs = group_lines(lines)
    assert [p.text for p in paragraphs] == [
        "Sprzedawca\nAcme Sp. z o.o.",
        "Razem",
        "Nabywca",
    ]
    assert paragraphs[0].box == (100, 100, 420, 165)
    assert paragraphs[0].confidence == 92.5


def test_text_files_become_paragraphs() -> None:
    result = parse_text("Notatka\r\n\r\nZapłacono 1 234,56 zł.\n\n\n", "memo")
    assert result.parser == "text"
    assert texts(result) == ["Notatka", "Zapłacono 1 234,56 zł."]
    assert result.pages == []


def test_csv_files_become_one_table_with_a_header() -> None:
    result = parse_csv(
        "Data;Kontrahent;Kwota\n2026-01-20;Świech;1 234,56\n", "payments"
    )
    tables = result.document.tables
    assert len(tables) == 1
    data = tables[0].data
    assert (data.num_rows, data.num_cols) == (2, 3)
    assert [c.text for c in data.table_cells if c.column_header] == [
        "Data",
        "Kontrahent",
        "Kwota",
    ]
    assert "| 2026-01-20 | Świech" in tables[0].export_to_markdown(doc=result.document)


def test_long_csv_files_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parse_module, "MAX_CSV_ROWS", 3)
    result = parse_csv("a,b\n" + "1,2\n" * 10, "big")
    assert result.document.tables[0].data.num_rows == 3
    assert result.warnings == ["kept the first 3 of 11 rows"]


def test_content_type_falls_back_to_the_file_extension() -> None:
    assert resolve_mime_type("application/octet-stream", "a.pdf") == PDF
    assert resolve_mime_type("application/pdf; charset=binary", "x") == PDF
    with pytest.raises(UnsupportedFileError):
        resolve_mime_type("application/zip", "a.zip")


def test_text_layer_is_detected_per_page() -> None:
    assert text_layer_pages(text_pdf(["Faktura VAT nr FV/2026/01/319", "Razem"])) == [
        True
    ]
    assert text_layer_pages(image_pdf(text_image(["Faktura VAT"]))) == [False]


def test_photos_are_read_with_ocr_and_kept_as_a_page_image() -> None:
    tessdata()
    image = text_image(["INVOICE FV/2026/01/319", "TOTAL 1234.56 PLN"])
    result = Parser(pipeline_settings()).parse(png(image), "image/png", "photo.png")

    assert result.parser == "ocr"
    joined = " ".join(texts(result))
    assert "FV/2026/01/319" in joined and "1234.56" in joined
    page = result.pages[0]
    assert (page.width, page.height, page.text_layer) == (1200, 700, False)
    assert page.ocr_confidence is not None and page.ocr_confidence > 80
    # OCR paragraphs carry boxes for citations, in page units from the top left.
    item = next(
        i for i, _ in result.document.iterate_items() if isinstance(i, TextItem)
    )
    box = item.prov[0].bbox
    assert box.coord_origin == CoordOrigin.TOPLEFT and 0 <= box.l < box.r <= 1200
    # The whole page is a figure, for a vision caption.
    [figure] = result.figures
    assert (figure.kind, figure.page_no, figure.mime_type) == ("page", 1, "image/jpeg")
    assert Image.open(io.BytesIO(figure.data)).size == (1200, 700)


def test_scanned_pdfs_are_rendered_and_read_with_ocr() -> None:
    tessdata()
    scan = image_pdf(text_image(["Faktura VAT nr FV/2026/01/319", "Razem 1234,56"]))
    result = Parser(pipeline_settings()).parse(scan, PDF, "scan.pdf")

    assert result.parser == "ocr"
    assert "FV/2026/01/319" in " ".join(texts(result))
    [page] = result.pages
    # 1200x700 pixels at 150 DPI: 576x336 points; boxes are in points.
    assert (round(page.width), round(page.height)) == (576, 336)
    item = next(
        i for i, _ in result.document.iterate_items() if isinstance(i, TextItem)
    )
    assert item.prov[0].bbox.r <= page.width
    assert [f.kind for f in result.figures] == ["page"]


def test_docx_files_keep_tables_and_embedded_images() -> None:
    from docx import Document
    from docx.shared import Cm

    document = Document()
    document.add_heading("Umowa o świadczenie usług", level=1)
    document.add_paragraph("Wynagrodzenie określa tabela poniżej.")
    table = document.add_table(rows=2, cols=2)
    for (row, col), text in {
        (0, 0): "Usługa",
        (0, 1): "Stawka",
        (1, 0): "Sprzątanie",
        (1, 1): "585,00",
    }.items():
        table.cell(row, col).text = text
    document.add_picture(
        io.BytesIO(png(text_image(["Fee table"], (400, 120)))), width=Cm(8)
    )
    buffer = io.BytesIO()
    document.save(buffer)

    result = Parser(pipeline_settings()).parse(buffer.getvalue(), DOCX, "umowa.docx")

    assert result.parser == "docling" and result.pages == []
    assert "Wynagrodzenie określa tabela poniżej." in texts(result)
    assert any(isinstance(i, TableItem) for i, _ in result.document.iterate_items())
    [figure] = result.figures
    assert figure.kind == "figure" and figure.mime_type == "image/png"
    picture = result.document.pictures[0]
    assert isinstance(picture, PictureItem) and picture.image is not None
    assert str(picture.image.uri) == f"figures/{figure.file_name}"


@pytest.mark.docling
def test_born_digital_pdfs_also_read_scanned_paragraphs_pasted_in() -> None:
    tessdata()
    pdf = pasted_scan_pdf(
        body_lines=12,
        scanned=[
            "Governing law. This Agreement is governed by the laws",
            "of Ireland. Any dispute shall be settled in Dublin.",
        ],
    )
    result = Parser(pipeline_settings()).parse(pdf, PDF, "contract.pdf")

    assert result.parser == "docling" and result.pages[0].text_layer
    joined = " ".join(texts(result))
    assert "Clause 12 of the agreement." in joined
    # The pasted paragraph has no text layer; Docling's OCR stage reads it.
    assert "laws of Ireland" in joined


@pytest.mark.docling
def test_mixed_pdfs_get_ocr_only_on_pages_without_text() -> None:
    tessdata()
    pdf = mixed_pdf(
        ["Contract SA-2025-94, page one"], text_image(["Schedule 1: FEES 7100.00 EUR"])
    )
    result = Parser(pipeline_settings()).parse(pdf, PDF, "mixed.pdf")

    assert result.parser == "docling+ocr"
    assert [p.text_layer for p in result.pages] == [True, False]
    joined = " ".join(texts(result))
    assert "Contract SA-2025-94" in joined and "7100.00" in joined
    page_two = [f for f in result.figures if f.page_no == 2]
    assert [f.kind for f in page_two] == ["page"]
    refs = {p.self_ref for p in result.document.pictures}
    assert {f.ref for f in result.figures} <= refs


def test_doclings_tesseract_stage_runs_without_orientation_detection() -> None:
    from docling.datamodel.accelerator_options import AcceleratorOptions

    from docai.ingest.docling_ocr import (
        UprightTesseractOcrModel,
        UprightTesseractOcrOptions,
        register,
    )

    register()
    register()  # safe to repeat
    options = UprightTesseractOcrOptions(lang=["pol", "eng"], path=str(tessdata()))
    model = UprightTesseractOcrModel(
        enabled=True,
        artifacts_path=None,
        options=options,
        accelerator_options=AcceleratorOptions(),
    )
    reader = model._osd_reader
    assert reader is not None
    assert reader.DetectOrientationScript()["orient_deg"] == 0
    # With OCR off, Docling never creates the detector; the variant must still build.
    UprightTesseractOcrModel(
        enabled=False,
        artifacts_path=None,
        options=options,
        accelerator_options=AcceleratorOptions(),
    )
