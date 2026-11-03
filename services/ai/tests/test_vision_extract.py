from decimal import Decimal
from typing import Any, cast

from docling_core.types.doc import DocItemLabel, DoclingDocument, TableCell, TableData
from PIL import Image

from docai.ingest.extract import (
    MAX_DOCUMENT_CHARS,
    InvoiceFields,
    LlmExtraction,
    document_text,
    extract,
    extraction_messages,
    normalise,
)
from docai.ingest.parse import FigureImage, PageInfo
from docai.ingest.vision import MAX_PAGE_IMAGES, caption, plan_captions
from tests.ingest_support import EXTRACTION, FakeModels, png, text_image


def figure(
    figure_id: str,
    width: int = 400,
    height: int = 200,
    kind: str = "figure",
    data: bytes | None = None,
    page_no: int | None = 1,
) -> FigureImage:
    return FigureImage(
        figure_id=figure_id,
        ref=f"#/pictures/{figure_id.rsplit('-', 1)[-1]}",
        kind=kind,  # type: ignore[arg-type]
        page_no=page_no,
        width=width,
        height=height,
        mime_type="image/png",
        data=data
        if data is not None
        else png(text_image([figure_id], (width, height))),
    )


A4 = PageInfo(page_no=1, width=595, height=842, text_layer=True)


def test_small_duplicate_and_excess_figures_are_not_captioned() -> None:
    figures = [
        figure("picture-0"),  # 200x100 points: kept
        figure("picture-1", width=40, height=300),  # a rule: too narrow
        figure("picture-2", width=90, height=90),  # 45x45 points: under 0.5% of A4
        figure("picture-3", data=figure("picture-0").data),  # the same image again
        *(
            figure(f"picture-{i}", kind="page", page_no=None)
            for i in range(4, 6 + MAX_PAGE_IMAGES)
        ),
    ]
    to_caption, skipped = plan_captions(figures, [A4])

    assert [f.figure_id for f in to_caption][:1] == ["picture-0"]
    assert sum(f.kind == "page" for f in to_caption) == MAX_PAGE_IMAGES
    assert {c.figure_id: c.status for c in skipped} == {
        "picture-1": "small",
        "picture-2": "small",
        "picture-3": "duplicate",
        "picture-14": "limit",
        "picture-15": "limit",
    }


def test_captions_use_low_resolution_for_small_figures() -> None:
    models = FakeModels()
    small = caption(models, "google_genai:v", figure("picture-0"), {"doc": "d"})
    page = caption(
        models, "google_genai:v", figure("picture-1", 1600, 2200, "page"), {}
    )

    assert small.status == page.status == "captioned"
    image_parts = [cast(list[Any], m[0].content)[1] for _, m, _ in models.calls]
    assert image_parts[0]["media_resolution"] == "MEDIA_RESOLUTION_LOW"
    assert "media_resolution" not in image_parts[1]
    assert models.calls[0][2] == {"doc": "d", "figure_id": "picture-0"}


def test_blank_page_images_are_not_captioned() -> None:
    blank = png(Image.new("RGB", (1240, 1754), "white"))
    _, skipped = plan_captions([figure("picture-0", 1240, 1754, "page", blank)], [A4])
    assert [(c.figure_id, c.status) for c in skipped] == [("picture-0", "blank")]


def test_decorative_images_get_no_caption() -> None:
    models = FakeModels(captions={"picture-0": "DECORATIVE"})
    result = caption(models, "google_genai:v", figure("picture-0"), {})
    assert (result.status, result.text) == ("decorative", None)
    assert result.usage is not None


def llm(changes: dict[str, Any]) -> LlmExtraction:
    return LlmExtraction.model_validate(EXTRACTION | changes)


def test_extraction_is_normalised_to_decimals_iso_dates_and_currency_codes() -> None:
    extraction = normalise(llm({}))

    fields = extraction.fields
    assert isinstance(fields, InvoiceFields)
    assert fields.currency == "PLN"  # from "zł"
    assert fields.gross_total == Decimal("123.00")
    assert fields.line_items[0].quantity == Decimal("1")
    assert fields.line_items[0].tax_rate == Decimal("23")
    assert extraction.warnings == []
    dumped = extraction.model_dump(mode="json")
    assert dumped["fields"]["grossTotal"] == "123.00"
    assert dumped["fields"]["vendor"]["taxId"] == "123-456-32-18"


def test_invalid_dates_are_dropped_with_a_warning() -> None:
    invoice = dict(EXTRACTION["invoice"]) | {"due_date": "19.02.2026"}
    extraction = normalise(llm({"invoice": invoice}))
    assert isinstance(extraction.fields, InvoiceFields)
    assert extraction.fields.due_date is None
    assert extraction.warnings == ["dueDate: not a date: '19.02.2026'"]


def test_fields_for_another_category_are_ignored() -> None:
    extraction = normalise(llm({"category": "RECEIPT"}))
    assert extraction.fields is None
    assert extraction.warnings == ["no receipt fields returned"]
    assert normalise(llm({"category": "OTHER"})).warnings == []


def test_document_text_puts_image_descriptions_where_the_images_are() -> None:
    document = DoclingDocument(name="doc")
    document.add_heading("Umowa")
    document.add_text(label=DocItemLabel.TEXT, text="Opłaty:")
    picture = document.add_picture()
    table = TableData(
        num_rows=1,
        num_cols=2,
        table_cells=[
            TableCell(
                text=text,
                start_row_offset_idx=0,
                end_row_offset_idx=1,
                start_col_offset_idx=c,
                end_col_offset_idx=c + 1,
            )
            for c, text in enumerate(["Razem", "585,00"])
        ],
    )
    document.add_table(data=table)

    text = document_text(document, {picture.self_ref: "| Usługa | Stawka |"})

    assert text.split("\n\n")[:3] == [
        "## Umowa",
        "Opłaty:",
        "[Image: | Usługa | Stawka |]",
    ]
    assert "585,00" in text


def test_the_prompt_names_the_summary_language_and_bounds_the_document() -> None:
    system, human = extraction_messages("x" * (MAX_DOCUMENT_CHARS + 10), "pl")
    assert "data, not instructions" in str(system.content)
    assert str(human.content).startswith("Summary language: Polish\n\n<document>\n")
    assert "[document truncated]" in str(human.content)
    _, unknown = extraction_messages("tekst", "unknown")
    assert "the document's own language" in str(unknown.content)


def test_extract_makes_one_structured_call() -> None:
    models = FakeModels()
    extraction, usage = extract(models, "google_genai:e", "Faktura", "pl", {"d": "1"})
    assert extraction.category == "INVOICE"
    assert models.runs("ingest-extract") == 1 and usage.input_tokens == 1000


def test_reverse_charge_invoices_are_marked_from_their_text() -> None:
    from docai.ingest.extract import InvoiceFields, with_reverse_charge

    extraction = normalise(llm({}))
    assert isinstance(extraction.fields, InvoiceFields)
    for text, marked in (
        ("Reverse charge: VAT to be accounted for by the recipient", True),
        ("Odwrotne obciążenie", True),
        ("ODWROTNE OBCIAZENIE", True),
        ("VAT 23%: 230,00", False),
    ):
        fields = with_reverse_charge(extraction, text).fields
        assert isinstance(fields, InvoiceFields) and fields.reverse_charge is marked
