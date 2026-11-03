"""Extraction accuracy: the pipeline's fields against ground truth.

Synthetic documents have complete ground truth, rebuilt from the seeded generator.
SROIE receipts have human labels for merchant, date and total. Every expected field is
`correct`, `wrong`, `missed` (no value extracted) or `spurious` (a value where the
truth has none); `empty` (both empty, e.g. no paid stamp) counts toward neither.
Precision, recall and F1 are micro-averaged over fields. Line items are matched
separately, by description and amount.
"""

import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal

from docai.corpus.manifest import DocumentRecord
from docai.corpus.synthetic.world import (
    WORLD_COUNT,
    Contract,
    Invoice,
    Receipt,
    build_world,
)
from docai.evals.runner import RUNS_DIR, SCRATCH_DIR
from docai.ingest.extract import (
    ContractFields,
    Extraction,
    Fields,
    InvoiceFields,
    ReceiptFields,
)

Outcome = Literal["correct", "wrong", "missed", "spurious", "empty"]
Kind = Literal["text", "id", "date", "amount", "int", "bool", "code", "law"]
Value = str | Decimal | int | bool | date | None


def fold(text: str) -> str:
    """Lower case, no accents, letters and digits only: 'Świech Sp. z o.o.' -> 'swiechspzoo'."""
    decomposed = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L"))
    return "".join(c for c in decomposed.casefold() if c.isalnum())


LAW_WORDS = {"the", "laws", "law", "of", "prawo", "and", "governed", "by"}
LAW_SYNONYMS = {"polish": "polskie", "poland": "polskie", "england": "england"}


def law_tokens(text: str) -> set[str]:
    words = re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())
    return {LAW_SYNONYMS.get(w, w) for w in words} - LAW_WORDS


def similarity(expected: str, predicted: str) -> float:
    """Best match of the expected text against any same-length stretch of the prediction.

    Names count as right when the prediction contains the expected name with at most
    small differences: SROIE labels have typos ("HAXINCONE" for MAXINCONE), and models
    add trading names ("FamilyMart (Maxincone Resources Sdn Bhd)").
    """
    if not expected or not predicted:
        return 0.0
    if len(predicted) <= len(expected):
        return SequenceMatcher(None, expected, predicted).ratio()
    return max(
        SequenceMatcher(
            None, expected, predicted[start : start + len(expected)]
        ).ratio()
        for start in range(len(predicted) - len(expected) + 1)
    )


NAME_SIMILARITY = 0.9


def matches(kind: Kind, expected: Value, predicted: Value) -> bool:
    if kind == "text":
        return similarity(fold(str(expected)), fold(str(predicted))) >= NAME_SIMILARITY
    if kind in ("id", "code"):
        return fold(str(expected)) == fold(str(predicted))
    if kind == "date":
        return str(expected) == str(predicted)
    if kind == "amount":
        return abs(Decimal(str(expected)) - Decimal(str(predicted))) < Decimal("0.005")
    if kind in ("int", "bool"):
        return expected == predicted
    # Governing law: the truth's distinctive words ("ireland", "polskie") all appear.
    return law_tokens(str(expected)) <= law_tokens(str(predicted))


def outcome(kind: Kind, expected: Value, predicted: Value) -> Outcome:
    if expected is None:
        return "empty" if predicted is None else "spurious"
    if predicted is None or predicted == "":
        return "missed"
    return "correct" if matches(kind, expected, predicted) else "wrong"


@dataclass(frozen=True)
class FieldResult:
    doc_id: str
    field: str
    expected: str | None
    predicted: str | None
    outcome: Outcome


@dataclass(frozen=True)
class DocumentResult:
    doc_id: str
    source: str
    language: str
    format: str
    category: str
    predicted_category: str
    fields: list[FieldResult]
    lines_expected: int
    lines_predicted: int
    lines_matched: int
    cost_usd: str
    cached: bool


Spec = tuple[str, Kind, Value, Value]  # field, kind, expected, predicted


def invoice_specs(truth: Invoice, got: InvoiceFields | None) -> list[Spec]:
    vendor = got.vendor if got else None
    buyer = got.buyer if got else None
    return [
        ("vendor.name", "text", truth.vendor.name, vendor.name if vendor else None),
        ("vendor.taxId", "id", truth.vendor.tax_id, vendor.tax_id if vendor else None),
        ("buyer.name", "text", truth.buyer.name, buyer.name if buyer else None),
        ("buyer.taxId", "id", truth.buyer.tax_id, buyer.tax_id if buyer else None),
        ("invoiceNumber", "id", truth.number, got.invoice_number if got else None),
        (
            "issueDate",
            "date",
            truth.issued.isoformat(),
            got.issue_date if got else None,
        ),
        ("dueDate", "date", truth.due.isoformat(), got.due_date if got else None),
        ("currency", "code", truth.currency, got.currency if got else None),
        ("netTotal", "amount", truth.net, got.net_total if got else None),
        ("taxTotal", "amount", truth.vat, got.tax_total if got else None),
        ("grossTotal", "amount", truth.gross, got.gross_total if got else None),
        (
            "paidDate",
            "date",
            truth.paid_on.isoformat() if truth.paid_on else None,
            got.paid_date if got else None,
        ),
    ]


def receipt_specs(truth: Receipt, got: ReceiptFields | None) -> list[Spec]:
    merchant = got.merchant if got else None
    return [
        (
            "merchant.name",
            "text",
            truth.merchant.name,
            merchant.name if merchant else None,
        ),
        (
            "merchant.taxId",
            "id",
            truth.merchant.tax_id,
            merchant.tax_id if merchant else None,
        ),
        ("receiptNumber", "id", truth.number, got.receipt_number if got else None),
        ("date", "date", truth.issued.isoformat(), got.date if got else None),
        ("currency", "code", truth.currency, got.currency if got else None),
        ("total", "amount", truth.total, got.total if got else None),
        ("taxTotal", "amount", truth.vat, got.tax_total if got else None),
        ("paymentMethod", "code", truth.payment, got.payment_method if got else None),
    ]


def contract_specs(truth: Contract, got: ContractFields | None) -> list[Spec]:
    names = {fold(p.name) for p in got.parties} if got else set()
    cap = got.value_cap if got else None
    return [
        # Each party is a field: found among the extracted parties, or missed.
        (
            "parties.vendor",
            "text",
            truth.vendor.name,
            truth.vendor.name if fold(truth.vendor.name) in names else None,
        ),
        (
            "parties.buyer",
            "text",
            truth.buyer.name,
            truth.buyer.name if fold(truth.buyer.name) in names else None,
        ),
        ("contractNumber", "id", truth.number, got.contract_number if got else None),
        (
            "effectiveDate",
            "date",
            truth.start.isoformat(),
            got.effective_date if got else None,
        ),
        ("endDate", "date", truth.end.isoformat(), got.end_date if got else None),
        ("autoRenewal", "bool", truth.auto_renewal, got.auto_renewal if got else None),
        (
            "noticePeriodDays",
            "int",
            truth.notice_days,
            got.notice_period_days if got else None,
        ),
        (
            "paymentTermsDays",
            "int",
            truth.payment_days,
            got.payment_terms_days if got else None,
        ),
        ("valueCap.amount", "amount", truth.cap, cap.amount if cap else None),
        ("valueCap.currency", "code", truth.currency, cap.currency if cap else None),
        (
            "governingLaw",
            "law",
            truth.governing_law,
            got.governing_law if got else None,
        ),
    ]


def sroie_specs(truth: dict[str, str], got: Fields | None) -> list[Spec]:
    """SROIE labels merchant, date and total. Some of its receipts are headed "tax
    invoice"; if the model filed one as an invoice, its vendor, issue date and gross
    total are scored instead, and the category shows up in category accuracy."""
    name: str | None = None
    day: str | None = None
    total: Decimal | None = None
    if isinstance(got, ReceiptFields):
        name, day, total = got.merchant.name, got.date, got.total
    elif isinstance(got, InvoiceFields):
        name, day, total = got.vendor.name, got.issue_date, got.gross_total
    return [
        ("merchant.name", "text", truth["company"], name),
        ("date", "date", truth["date"], day),
        ("total", "amount", truth["total"], total),
    ]


def match_lines(
    expected: list[tuple[str, Decimal]], predicted: list[tuple[str, Decimal | None]]
) -> int:
    """Lines matched one-to-one by folded description and amount."""
    remaining = list(predicted)
    matched = 0
    for description, amount in expected:
        for index, (other, other_amount) in enumerate(remaining):
            if (
                fold(description) == fold(other)
                and other_amount is not None
                and abs(other_amount - amount) < Decimal("0.005")
            ):
                matched += 1
                del remaining[index]
                break
    return matched


@dataclass
class Truth:
    """Ground truth by corpus document ID."""

    invoices: dict[str, Invoice] = field(default_factory=dict)
    receipts: dict[str, Receipt] = field(default_factory=dict)
    contracts: dict[str, Contract] = field(default_factory=dict)
    sroie: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls, sroie_keys: Path | None = None) -> Truth:
        truth = cls()
        for index in range(WORLD_COUNT):
            world = build_world(index)
            truth.invoices |= {i.doc_id: i for i in world.invoices}
            truth.receipts |= {r.doc_id: r for r in world.receipts}
            truth.contracts |= {c.doc_id: c for c in world.contracts}
        if sroie_keys is not None and sroie_keys.exists():
            keys = json.loads(sroie_keys.read_text(encoding="utf-8"))
            truth.sroie = {k["doc_id"]: k for k in keys}
        return truth

    def has(self, doc_id: str) -> bool:
        return any(
            doc_id in table
            for table in (self.invoices, self.receipts, self.contracts, self.sroie)
        )


def score_document(
    record: DocumentRecord, extraction: Extraction, truth: Truth
) -> tuple[list[FieldResult], int, int, int]:
    """Field results and (expected, predicted, matched) line counts for one document."""
    got = extraction.fields
    specs: list[Spec]
    lines_expected: list[tuple[str, Decimal]] = []
    lines_predicted: list[tuple[str, Decimal | None]] = []
    if record.doc_id in truth.invoices:
        invoice = truth.invoices[record.doc_id]
        got_invoice = got if isinstance(got, InvoiceFields) else None
        specs = invoice_specs(invoice, got_invoice)
        lines_expected = [(line.description, line.net) for line in invoice.lines]
        if got_invoice:
            lines_predicted = [
                (line.description, line.net_amount) for line in got_invoice.line_items
            ]
    elif record.doc_id in truth.receipts:
        receipt = truth.receipts[record.doc_id]
        got_receipt = got if isinstance(got, ReceiptFields) else None
        specs = receipt_specs(receipt, got_receipt)
        lines_expected = [(item.description, item.amount) for item in receipt.items]
        if got_receipt:
            lines_predicted = [(i.description, i.amount) for i in got_receipt.items]
    elif record.doc_id in truth.contracts:
        contract = truth.contracts[record.doc_id]
        specs = contract_specs(
            contract, got if isinstance(got, ContractFields) else None
        )
    else:
        specs = sroie_specs(truth.sroie[record.doc_id], got)
    results = [
        FieldResult(
            doc_id=record.doc_id,
            field=name,
            expected=None if expected is None else str(expected),
            predicted=None if predicted is None else str(predicted),
            outcome=outcome(kind, expected, predicted),
        )
        for name, kind, expected, predicted in specs
    ]
    matched = match_lines(lines_expected, lines_predicted)
    return results, len(lines_expected), len(lines_predicted), matched


def prf(counts: Counter[str]) -> dict[str, Any]:
    correct = counts["correct"]
    predicted = correct + counts["wrong"] + counts["spurious"]
    expected = correct + counts["wrong"] + counts["missed"]
    precision = correct / predicted if predicted else None
    recall = correct / expected if expected else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision and recall
        else (0.0 if predicted or expected else None)
    )
    return {
        "fields": expected,
        "correct": correct,
        "precision": None if precision is None else round(precision, 3),
        "recall": None if recall is None else round(recall, 3),
        "f1": None if f1 is None else round(f1, 3),
    }


def summarise(documents: list[DocumentResult]) -> dict[str, Any]:
    def group(key: Callable[[DocumentResult], str]) -> dict[str, Any]:
        counts: dict[str, Counter[str]] = defaultdict(Counter)
        for doc in documents:
            for result in doc.fields:
                counts[key(doc)][result.outcome] += 1
        return {name: prf(c) for name, c in sorted(counts.items())}

    by_field: dict[str, Counter[str]] = defaultdict(Counter)
    overall: Counter[str] = Counter()
    for doc in documents:
        for result in doc.fields:
            by_field[f"{doc.category.lower()}.{result.field}"][result.outcome] += 1
            overall[result.outcome] += 1
    lines = [d for d in documents if d.lines_expected]
    matched = sum(d.lines_matched for d in lines)
    line_precision = matched / max(sum(d.lines_predicted for d in lines), 1)
    line_recall = matched / max(sum(d.lines_expected for d in lines), 1)
    return {
        "documents": len(documents),
        "overall": prf(overall),
        "by_category": group(lambda d: d.category),
        "by_source": group(lambda d: d.source),
        "by_language": group(lambda d: d.language),
        "by_format": group(lambda d: d.format),
        "by_field": {name: prf(c) for name, c in sorted(by_field.items())},
        "category_accuracy": round(
            sum(d.category == d.predicted_category for d in documents) / len(documents),
            3,
        )
        if documents
        else None,
        "line_items": {
            "expected": sum(d.lines_expected for d in lines),
            "matched": matched,
            "f1": round(
                2 * line_precision * line_recall / (line_precision + line_recall), 3
            )
            if matched
            else 0.0,
        },
        "cost_usd": str(sum((Decimal(d.cost_usd) for d in documents), Decimal(0))),
    }


@dataclass
class ExtractionRun:
    run_id: str
    split: str
    extract_model: str
    vision_model: str
    created: str
    documents: list[DocumentResult]
    kind: str = "extraction"

    def summary(self) -> dict[str, Any]:
        return summarise(self.documents)

    def save(self, record: bool) -> Path:
        directory = RUNS_DIR if record else SCRATCH_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id}.json"
        payload = {k: v for k, v in asdict(self).items() if k != "documents"}
        payload |= {
            "summary": self.summary(),
            "documents": [asdict(d) for d in self.documents],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return path


def new_run(split: str, extract_model: str, vision_model: str) -> ExtractionRun:
    created = datetime.now(UTC)
    return ExtractionRun(
        run_id=f"{created:%Y%m%d-%H%M%S}-extraction-{split}",
        split=split,
        extract_model=extract_model,
        vision_model=vision_model,
        created=created.isoformat(timespec="seconds"),
        documents=[],
    )


def evaluable(records: Iterable[DocumentRecord], truth: Truth) -> list[DocumentRecord]:
    return [r for r in records if truth.has(r.doc_id)]
