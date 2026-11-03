from decimal import Decimal
from pathlib import Path

from docling_core.types.doc import DocItemLabel, DoclingDocument
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from docai.core.tracing import Tracing
from docai.ingest.cache import DiskCache
from docai.ingest.models import IngestModels
from docai.ingest.parse import PageInfo, Parser, ParseResult
from docai.ingest.pipeline import Pipeline
from tests.ingest_support import (
    STAMP_CAPTION,
    FakeModels,
    pipeline_settings,
    png,
    text_image,
)


class FakeParser(Parser):
    """Two copies of a stamp and a page image; counts how often it parses."""

    def __init__(self) -> None:
        super().__init__(pipeline_settings())
        self.parses = 0

    def parse(self, data: bytes, mime_type: str, file_name: str) -> ParseResult:
        self.parses += 1
        document = DoclingDocument(name="invoice")
        document.add_text(
            label=DocItemLabel.TEXT, text="Faktura VAT nr FV/1 dla Beta S.A. z Krakowa"
        )
        result = ParseResult(
            document=document,
            parser="docling",
            pages=[PageInfo(page_no=1, width=595, height=842, text_layer=True)],
        )
        stamp = text_image(["PAID"], (320, 160))
        for _ in range(2):
            picture = document.add_picture()
            Parser._attach(result, picture, stamp, kind="figure", mime_type="image/png")
        return result


def test_figures_are_captioned_once_and_attached_to_the_document() -> None:
    models = FakeModels()
    pipeline = Pipeline(pipeline_settings(), models, parser=FakeParser())

    parsed, figures = pipeline.process(b"%PDF", "application/pdf", "fv.pdf", {"d": "1"})

    # The repeated stamp is captioned once and both pictures carry the caption.
    assert models.runs("ingest-caption") == 1
    assert [f.caption_status for f in parsed.figures] == ["captioned", "duplicate"]
    assert [f.caption for f in parsed.figures] == [STAMP_CAPTION] * 2
    pictures = parsed.document["pictures"]
    assert [p["meta"]["description"]["text"] for p in pictures] == [STAMP_CAPTION] * 2
    assert pictures[0]["image"]["uri"] == "figures/picture-0.png"
    assert [f.file_name for f in figures] == ["picture-0.png", "picture-1.png"]

    assert parsed.language == "pl"
    assert parsed.extraction.category == "INVOICE"
    assert [c.role for c in parsed.calls] == ["caption", "extract"]
    assert parsed.cost_usd == 2 * Decimal("0.0004")  # 1,000 in + 100 out at $0.25/$1.50
    # The extraction call saw the caption where the image is.
    extract_messages = next(m for run, m, _ in models.calls if run == "ingest-extract")
    assert f"[Image: {STAMP_CAPTION}]" in str(extract_messages[1].content)


def test_a_cached_file_is_neither_parsed_nor_sent_to_a_model_again(
    tmp_path: Path,
) -> None:
    cache = DiskCache(tmp_path)
    parser = FakeParser()
    first = Pipeline(pipeline_settings(), FakeModels(), cache, parser)
    first.parse(b"same bytes", "application/pdf", "a.pdf")
    second = first.parse(b"same bytes", "application/pdf", "a.pdf")

    assert parser.parses == 1
    assert [f.figure_id for f in second.figures] == ["picture-0", "picture-1"]
    assert second.figures[0].data == png(text_image(["PAID"], (320, 160)))
    assert second.pages[0].text_layer


def test_model_replies_are_cached_on_disk_by_content(tmp_path: Path) -> None:
    usage = {"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280}
    fake = GenericFakeChatModel(
        messages=iter([AIMessage(content="A red stamp.", usage_metadata=usage)])
    )
    models = IngestModels(pipeline_settings(), Tracing(), DiskCache(tmp_path))
    models._models["google_genai:v", 1024] = fake  # a second call would exhaust it
    messages: list[BaseMessage] = [HumanMessage(content="Describe the image")]

    first = models.call(
        "google_genai:v", messages, max_tokens=1024, run_name="t", metadata={}
    )
    again = models.call(
        "google_genai:v", messages, max_tokens=1024, run_name="t", metadata={}
    )

    assert (first.text, first.usage.cached) == ("A red stamp.", False)
    assert (again.text, again.usage.cached, again.usage.input_tokens) == (
        "A red stamp.",
        True,
        1200,
    )
    assert again.usage.cost_usd == first.usage.cost_usd


def test_language_comes_from_the_transcription_on_pages_without_a_text_layer() -> None:
    from docling_core.types.doc import BoundingBox, ProvenanceItem

    from docai.ingest.pipeline import language_text

    document = DoclingDocument(name="receipt")
    noise = "RYDZE Zp TWK) WĘOOąrEćECC (TE"  # Tesseract on a poor photo
    box = BoundingBox(l=0, t=0, r=10, b=10)
    document.add_text(
        label=DocItemLabel.TEXT,
        text=noise,
        prov=ProvenanceItem(page_no=1, bbox=box, charspan=(0, len(noise))),
    )
    result = ParseResult(
        document=document,
        parser="ocr",
        pages=[PageInfo(page_no=1, width=900, height=1200, text_layer=False)],
    )
    picture = document.add_picture(
        prov=ProvenanceItem(page_no=1, bbox=box, charspan=(0, 0))
    )
    Parser._attach(
        result, picture, text_image(["x"]), kind="page", mime_type="image/jpeg"
    )
    caption = (
        "TESCO STORES SDN BHD\nTOTAL RM 30.90\nCASH RM 51.00"
        "\n\nThis image shows a printed store receipt."
    )

    text = language_text(result, {picture.self_ref: caption})

    assert noise not in text
    assert text == "TESCO STORES SDN BHD\nTOTAL RM 30.90\nCASH RM 51.00"


def test_table_cells_count_toward_the_language() -> None:
    from docai.ingest.parse import parse_csv
    from docai.ingest.pipeline import language_text

    result = parse_csv("Kontrahent;Status\nŚwiech Usługi;zapłacona\n", "platnosci")
    assert language_text(result, {}) == "Kontrahent Status Świech Usługi zapłacona"


def test_input_from_geminis_prefix_cache_costs_a_tenth() -> None:
    from docai.core.prices import cost

    spec = "google_genai:gemini-3.1-flash-lite"
    assert cost(spec, 1_000_000, 0) == Decimal("0.25")
    # 800,000 of the input tokens served from the cache: 200,000 + 80,000 billed.
    assert cost(spec, 1_000_000, 0, 800_000) == Decimal("0.07")
    assert cost("google_genai:unknown-model", 1, 1) is None
