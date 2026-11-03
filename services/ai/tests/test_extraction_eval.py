from collections import Counter
from decimal import Decimal

import pytest

from docai.corpus.manifest import DocumentRecord
from docai.evals.extraction import (
    Truth,
    fold,
    match_lines,
    matches,
    outcome,
    prf,
    score_document,
)
from docai.ingest.extract import (
    Extraction,
    InvoiceFields,
    InvoiceLine,
    Metadata,
    Party,
    ReceiptFields,
)


@pytest.fixture(scope="module")
def truth() -> Truth:
    return Truth.load()


def record(doc_id: str, category: str = "INVOICE") -> DocumentRecord:
    return DocumentRecord(
        doc_id=doc_id,
        source="synthetic" if doc_id.startswith("syn") else "sroie",
        title="t",
        category=category,
        language="pl",
        format="pdf",
        path="x.pdf",
        split="train",
        group="g",
        license="l",
        sha256="0" * 64,
    )


def extraction(
    fields: InvoiceFields | ReceiptFields | None, category: str = "INVOICE"
) -> Extraction:
    return Extraction(
        category=category,
        confidence=1.0,
        summary="",
        metadata=Metadata(),
        fields=fields,
    )


def perfect_invoice(truth: Truth, doc_id: str) -> InvoiceFields:
    invoice = truth.invoices[doc_id]
    return InvoiceFields(
        vendor=Party(name=invoice.vendor.name, tax_id=invoice.vendor.tax_id),
        buyer=Party(name=invoice.buyer.name, tax_id=invoice.buyer.tax_id),
        invoice_number=invoice.number,
        issue_date=invoice.issued.isoformat(),
        due_date=invoice.due.isoformat(),
        currency=invoice.currency,
        net_total=invoice.net,
        tax_total=invoice.vat,
        gross_total=invoice.gross,
        paid_date=invoice.paid_on.isoformat() if invoice.paid_on else None,
        line_items=[
            InvoiceLine(description=line.description, net_amount=line.net)
            for line in invoice.lines
        ],
    )


def test_field_comparison_rules() -> None:
    assert fold("Świech Usługi Sp. z o.o.") == "swiechuslugispzoo"
    assert matches("id", "885-772-17-27", "8857721727")
    assert matches("amount", Decimal("5166.00"), Decimal("5166"))
    assert not matches("amount", Decimal("5166.00"), Decimal("5166.01"))
    assert matches("law", "prawo polskie", "Prawo polskie (Polish law)")
    assert matches("law", "the laws of Ireland", "laws of Ireland")
    assert not matches("law", "the laws of Ireland", "the laws of England and Wales")
    # Names tolerate a label typo and an added trading name, not another company.
    assert matches(
        "text",
        "HAXINCONE RESOURCES SDN BHD",
        "FamilyMart (Maxincone Resources Sdn Bhd)",
    )
    assert not matches("text", "Świech Usługi Sp. z o.o.", "Herba Usługi Sp. z o.o.")


def test_outcomes_separate_wrong_missed_spurious_and_empty() -> None:
    assert outcome("date", "2026-02-16", "2026-02-16") == "correct"
    assert outcome("date", "2026-02-16", "2026-02-17") == "wrong"
    assert outcome("date", "2026-02-16", None) == "missed"
    assert outcome("date", None, "2026-02-16") == "spurious"
    assert outcome("date", None, None) == "empty"
    counts = Counter({"correct": 8, "wrong": 1, "missed": 1, "spurious": 2})
    assert prf(counts) == {
        "fields": 10,
        "correct": 8,
        "precision": 0.727,
        "recall": 0.8,
        "f1": 0.762,
    }


def test_a_perfect_invoice_scores_every_field(truth: Truth) -> None:
    doc_id = "syn-w0-inv-0-q1"  # scanned, with a paid stamp
    fields, expected, predicted, matched = score_document(
        record(doc_id), extraction(perfect_invoice(truth, doc_id)), truth
    )
    assert {f.outcome for f in fields} == {"correct"}
    assert len(fields) == 12 and expected == predicted == matched == 2


def test_mistakes_and_wrong_categories_are_counted(truth: Truth) -> None:
    doc_id = "syn-w0-inv-0-q1"
    wrong = perfect_invoice(truth, doc_id).model_copy(
        update={"gross_total": Decimal("1.00"), "paid_date": None, "due_date": None}
    )
    fields, *_ = score_document(record(doc_id), extraction(wrong), truth)
    outcomes = {f.field: f.outcome for f in fields}
    assert outcomes["grossTotal"] == "wrong"
    assert outcomes["paidDate"] == outcomes["dueDate"] == "missed"

    as_receipt = extraction(None, category="RECEIPT")
    fields, *_ = score_document(record(doc_id), as_receipt, truth)
    # Filed as a receipt, the invoice yields no invoice fields at all.
    assert {f.outcome for f in fields} == {"missed"}


def test_an_invoice_without_a_stamp_expects_no_paid_date(truth: Truth) -> None:
    doc_id = next(d for d, i in truth.invoices.items() if i.paid_on is None)
    guessed = perfect_invoice(truth, doc_id).model_copy(
        update={"paid_date": "2026-03-01"}
    )
    fields, *_ = score_document(record(doc_id), extraction(guessed), truth)
    assert {f.field: f.outcome for f in fields}["paidDate"] == "spurious"


def test_sroie_receipts_filed_as_invoices_are_scored_on_the_same_fields(
    truth: Truth,
) -> None:
    truth.sroie["sroie-test"] = {
        "doc_id": "sroie-test",
        "company": "GARDENIA BAKERIES (KL) SDN BHD",
        "date": "2017-07-21",
        "total": "33.05",
    }
    filed_as_invoice = InvoiceFields(
        vendor=Party(name="Gardenia Bakeries (KL) Sdn Bhd"),
        issue_date="2017-07-21",
        gross_total=Decimal("33.05"),
    )
    fields, *_ = score_document(
        record("sroie-test", "RECEIPT"), extraction(filed_as_invoice), truth
    )
    assert [f.outcome for f in fields] == ["correct"] * 3


def test_line_items_match_one_to_one_by_description_and_amount() -> None:
    expected = [("Usługa", Decimal("10.00")), ("Usługa", Decimal("10.00"))]
    assert match_lines(expected, [("uslu-ga", Decimal("10")), ("Usługa", None)]) == 1
    assert match_lines(expected, [("Usługa", Decimal("10.00"))] * 3) == 2


def test_extraction_runs_get_their_own_results_table(truth: Truth) -> None:
    from docai.evals import report
    from docai.evals.extraction import DocumentResult, summarise

    doc_id = "syn-w0-inv-0-q1"
    fields, expected, predicted, matched = score_document(
        record(doc_id), extraction(perfect_invoice(truth, doc_id)), truth
    )
    document = DocumentResult(
        doc_id=doc_id,
        source="synthetic",
        language="pl",
        format="scanned_pdf",
        category="INVOICE",
        predicted_category="INVOICE",
        fields=fields,
        lines_expected=expected,
        lines_predicted=predicted,
        lines_matched=matched,
        cost_usd="0.0026",
        cached=True,
    )
    run = {
        "kind": "extraction",
        "run_id": "20261012-000000-extraction-dev",
        "split": "dev",
        "summary": summarise([document]),
    }

    text = report.render([run])

    assert "## Extraction\n" in text and "## Dev split" not in text
    table = [line for line in text.splitlines() if line.startswith("| dev |")]
    assert table[0].count("|") == 15  # 14 columns
    assert "1.000 (12/12)" in table[0] and "$0.0026" in table[0]
