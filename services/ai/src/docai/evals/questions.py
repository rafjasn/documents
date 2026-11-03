"""Golden question drafts, generated from ground truth without any LLM calls.

Synthetic worlds give exact answers for every question type; CUAD contributes contract
facts from its short-answer annotations; SROIE contributes receipt totals and dates.
Only dev and test documents get questions; train documents are kept for fine-tuning.
"""

import random
import re
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

import pypdfium2 as pdfium

from docai.corpus.cuad import CuadContract, load_annotations
from docai.corpus.manifest import DocumentRecord, Language, Split, read_manifest
from docai.corpus.sroie import SroieReceipt, load_keys
from docai.corpus.synthetic.build import WORLD_SPLITS
from docai.corpus.synthetic.text import amount, day, long_day
from docai.corpus.synthetic.world import (
    Anomaly,
    Contract,
    Invoice,
    Party,
    World,
    build_world,
)
from docai.evals.golden import AnswerKind, GoldenItem, QuestionType, Relevant

REFUSAL = {"en": "Not stated in the documents.", "pl": "Dokumenty tego nie podają."}
YES_NO = {"en": ("Yes", "No"), "pl": ("Tak", "Nie")}
EVAL_SPLITS: tuple[Split, ...] = ("dev", "test")


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip().casefold()


class PageFinder:
    """Finds the page of a PDF that contains a phrase; images count as page 1."""

    def __init__(self, records: dict[str, DocumentRecord]) -> None:
        self.records = records
        self.texts: dict[str, list[str]] = {}

    def __call__(self, doc_id: str, *needles: str) -> int | None:
        record = self.records[doc_id]
        if record.format == "docx":
            return None
        if record.pages == 1 or not needles:
            return 1 if record.pages == 1 else None
        if doc_id not in self.texts:
            document = pdfium.PdfDocument(record.file)
            self.texts[doc_id] = [
                squash(page.get_textpage().get_text_range()) for page in document
            ]
            document.close()
        wanted = [squash(needle)[:60] for needle in needles if needle.strip()]
        for number, text in enumerate(self.texts[doc_id], start=1):
            if any(needle in text for needle in wanted):
                return number
        return None


class Drafts:
    def __init__(self, split: Split, prefix: str, pages: PageFinder) -> None:
        self.split, self.prefix, self.pages = split, prefix, pages
        self.items: list[GoldenItem] = []

    def add(
        self,
        key: str,
        type_: QuestionType,
        language: Language,
        question: str,
        answer: str,
        kind: AnswerKind,
        value: object,
        docs: list[tuple[str, tuple[str, ...]]],
        accepted: tuple[str, ...] = (),
        source: str = "template",
    ) -> None:
        self.items.append(
            GoldenItem(
                id=f"{self.prefix}-{key}",
                split=self.split,
                type=type_,
                language=language,
                question=question,
                answer=answer,
                kind=kind,
                value=normal_value(value),
                accepted=accepted,
                relevant=tuple(
                    Relevant(doc_id=doc_id, page=self.pages(doc_id, *needles))
                    for doc_id, needles in docs
                ),
                source=source,
            )
        )


def phrase(description: str) -> str:
    """Lower-case a service name for mid-sentence use, keeping acronyms and place names."""
    first, _, rest = description.partition(" ")
    first = first if first.isupper() and len(first) > 1 else first.lower()
    return f"{first} {rest}".strip()


def normal_value(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    return str(value)


def world_drafts(world: World, pages: PageFinder) -> list[GoldenItem]:
    rng = random.Random(f"golden-{world.index}")
    split = WORLD_SPLITS[world.index]
    d = Drafts(split, world.group, pages)
    b = world.buyer
    v0, v1, v2, v3 = world.vendors
    c0, c1, c2, c3 = world.contracts
    inv = {v: world.invoices_for(v) for v in world.vendors}

    def digital(vendor: Party) -> list[Invoice]:
        return [i for i in inv[vendor] if i.format == "pdf"]

    def doc(i: Invoice | Contract, *needles: str) -> tuple[str, tuple[str, ...]]:
        return (i.doc_id, needles)

    def cap_text(c: Contract) -> str:
        return amount(c.cap, c.currency, c.language)

    def pick(options: list[Invoice]) -> Invoice:
        return rng.choice(options)

    def amt(value: Decimal, currency: str, language: Language) -> str:
        return amount(value, currency, language)

    pl_digital = digital(v0) + digital(v1)
    en_digital = digital(v2) + digital(v3)

    # single_fact
    first_pl = i = pick(pl_digital)
    d.add(
        "due-date",
        "single_fact",
        "pl",
        f"Jaki jest termin płatności faktury {i.number} wystawionej przez {i.vendor.name}?",
        day(i.due, "pl"),
        "date",
        i.due,
        [doc(i)],
    )
    i = pick(en_digital)
    d.add(
        "amount-due",
        "single_fact",
        "en",
        f"What is the total amount due on invoice {i.number} from {i.vendor.name}?",
        amt(i.gross, i.currency, "en"),
        "number",
        i.gross,
        [doc(i)],
    )
    i = pick([x for x in pl_digital if x.vendor != first_pl.vendor] or pl_digital)
    d.add(
        "seller-nip",
        "single_fact",
        "pl",
        f"Jaki NIP sprzedawcy podano na fakturze {i.number} wystawionej przez {i.vendor.name}?",
        i.vendor.tax_id,
        "text",
        i.vendor.tax_id,
        [doc(i)],
        accepted=(i.vendor.tax_id, i.vendor.tax_id.replace("-", "")),
    )
    d.add(
        "notice-period",
        "single_fact",
        "pl",
        f"Jaki okres wypowiedzenia przewiduje umowa nr {c0.number} zawarta z {v0.name}?",
        f"{c0.notice_days} dni",
        "number",
        c0.notice_days,
        [(c0.doc_id, (f"{c0.notice_days}-dniowego okresu wypowiedzenia",))],
    )
    d.add(
        "payment-days",
        "single_fact",
        "en",
        f"Within how many days of the invoice date must invoices under the service agreement {c2.number} with {v2.name} be paid?",
        f"{c2.payment_days} days",
        "number",
        c2.payment_days,
        [(c2.doc_id, ("days of the invoice date",))],
    )
    d.add(
        "end-date",
        "single_fact",
        "pl",
        f"Do kiedy obowiązuje umowa nr {c1.number} zawarta z {v1.name}?",
        long_day(c1.end, "pl"),
        "date",
        c1.end,
        [doc(c1)],
    )
    yes, no = YES_NO["en"]
    d.add(
        "auto-renewal",
        "single_fact",
        "en",
        f"Does the service agreement {c3.number} with {v3.name} renew automatically?",
        yes if c3.auto_renewal else no,
        "boolean",
        c3.auto_renewal,
        [doc(c3)],
    )
    law = "England and Wales" if "England" in c2.governing_law else "Ireland"
    d.add(
        "governing-law",
        "single_fact",
        "en",
        f"Which law governs the service agreement {c2.number} with {v2.name}?",
        law,
        "text",
        law,
        [(c2.doc_id, ("are governed by",))],
        accepted=(law,),
    )

    # table
    two_line_en = [x for x in en_digital if len(x.lines) > 1] or en_digital
    i = pick(two_line_en)
    line = i.lines[-1]
    d.add(
        "line-quantity",
        "table",
        "en",
        f"How many {line.unit}s of {phrase(line.description)} were billed on invoice {i.number} from {i.vendor.name}?",
        str(line.quantity),
        "number",
        line.quantity,
        [doc(i)],
    )
    i2 = pick([x for x in two_line_en if x != i] or two_line_en)
    line = i2.lines[0]
    d.add(
        "line-net",
        "table",
        "en",
        f"What is the net amount for {phrase(line.description)} on invoice {i2.number} from {i2.vendor.name}?",
        amt(line.net, i2.currency, "en"),
        "number",
        line.net,
        [doc(i2)],
    )
    two_line_pl = [x for x in pl_digital if len(x.lines) > 1] or pl_digital
    i = pick(two_line_pl)
    line = i.lines[-1]
    d.add(
        "line-unit-price",
        "table",
        "pl",
        f"Jaka jest cena netto za jednostkę usługi „{line.description}” na fakturze {i.number} wystawionej przez {i.vendor.name}?",
        amt(line.unit_price, i.currency, "pl"),
        "number",
        line.unit_price,
        [doc(i)],
    )
    i2 = pick([x for x in two_line_pl if x != i] or two_line_pl)
    line = i2.lines[0]
    d.add(
        "line-vat",
        "table",
        "pl",
        f"Jaka jest kwota VAT dla pozycji „{line.description}” na fakturze {i2.number} wystawionej przez {i2.vendor.name}?",
        amt(line.vat, i2.currency, "pl"),
        "number",
        line.vat,
        [doc(i2)],
    )
    s = c0.services[-1]
    d.add(
        "contract-rate",
        "table",
        "pl",
        f"Jaka jest stawka netto za usługę „{s.description}” w umowie nr {c0.number} z {v0.name}?",
        amt(s.unit_price, c0.currency, "pl"),
        "number",
        s.unit_price,
        [(c0.doc_id, (s.description,))],
    )
    s = c2.services[0]
    d.add(
        "contract-rate-en",
        "table",
        "en",
        f"What rate does the service agreement {c2.number} with {v2.name} set for {phrase(s.description)}?",
        amt(s.unit_price, c2.currency, "en"),
        "number",
        s.unit_price,
        [(c2.doc_id, (s.description,))],
    )
    s = c1.services[0]
    d.add(
        "contract-rate-docx",
        "table",
        "pl",
        f"Jaka stawka netto obowiązuje za usługę „{s.description}” zgodnie z umową nr {c1.number} z {v1.name}?",
        amt(s.unit_price, c1.currency, "pl"),
        "number",
        s.unit_price,
        [doc(c1)],
    )

    # image: stamps, scans, receipt photos and the image-only fee table
    paid = [x for x in world.invoices if x.paid_on is not None]
    i = pick([x for x in paid if x.format == "pdf"] or paid)
    assert i.paid_on is not None
    if i.language == "pl":
        d.add(
            "paid-date",
            "image",
            "pl",
            f"Kiedy zapłacono fakturę {i.number} wystawioną przez {i.vendor.name}?",
            day(i.paid_on, "pl"),
            "date",
            i.paid_on,
            [doc(i)],
        )
    else:
        d.add(
            "paid-date",
            "image",
            "en",
            f"On what date was invoice {i.number} from {i.vendor.name} paid?",
            day(i.paid_on, "en"),
            "date",
            i.paid_on,
            [doc(i)],
        )
    i = pick([x for x in world.invoices if x.format == "scanned_pdf"])
    if i.language == "pl":
        d.add(
            "scan-gross",
            "image",
            "pl",
            f"Jaka jest kwota brutto faktury {i.number} wystawionej przez {i.vendor.name}?",
            amt(i.gross, i.currency, "pl"),
            "number",
            i.gross,
            [doc(i)],
        )
    else:
        d.add(
            "scan-gross",
            "image",
            "en",
            f"What is the total amount on invoice {i.number} from {i.vendor.name}?",
            amt(i.gross, i.currency, "en"),
            "number",
            i.gross,
            [doc(i)],
        )
    r = world.receipts[2]
    d.add(
        "receipt-total",
        "image",
        "en",
        f"What was the total on the receipt from {r.merchant.name} dated {day(r.issued, 'en')}?",
        amt(r.total, r.currency, "en"),
        "number",
        r.total,
        [(r.doc_id, ())],
    )
    s = c3.services[0]
    d.add(
        "image-fee-table",
        "image",
        "en",
        f"What rate does the service agreement {c3.number} with {v3.name} set for {phrase(s.description)}?",
        amt(s.unit_price, c3.currency, "en"),
        "number",
        s.unit_price,
        [doc(c3)],
    )

    # cross_doc: the planted anomalies, each paired with a compliant counterpart
    def contract_docs(
        c: Contract, invoices: list[Invoice]
    ) -> list[tuple[str, tuple[str, ...]]]:
        return [doc(c, cap_text(c)), *(doc(x) for x in invoices)]

    def other(anomaly: Anomaly) -> Contract:
        return rng.choice(
            [c for c in world.contracts if c != world.anomaly_contract(anomaly)]
        )

    for key, c, exceeded in (
        ("cap-exceeded", world.anomaly_contract("cap_exceeded"), True),
        ("cap-within", other("cap_exceeded"), False),
    ):
        question = (
            f"Did the net amounts invoiced by {c.vendor.name} in 2026 exceed the value cap in their contract?"
            if c.language == "en"
            else f"Czy łączna wartość netto faktur wystawionych przez {c.vendor.name} w 2026 r. przekroczyła limit wartości umowy?"
        )
        d.add(
            key,
            "cross_doc",
            c.language,
            question,
            YES_NO[c.language][0 if exceeded else 1],
            "boolean",
            exceeded,
            contract_docs(c, inv[c.vendor]),
        )

    bad_terms = world.anomaly_contract("wrong_payment_terms")
    for key, c, invoice, ok in (
        ("terms-wrong", bad_terms, inv[bad_terms.vendor][1], False),
        ("terms-ok", (oc := other("wrong_payment_terms")), inv[oc.vendor][1], True),
    ):
        question = (
            f"Does the due date on invoice {invoice.number} from {c.vendor.name} follow the payment terms of their contract?"
            if c.language == "en"
            else f"Czy termin płatności na fakturze {invoice.number} wystawionej przez {c.vendor.name} jest zgodny z warunkami płatności z umowy?"
        )
        d.add(
            key,
            "cross_doc",
            c.language,
            question,
            YES_NO[c.language][0 if ok else 1],
            "boolean",
            ok,
            [
                doc(
                    c, f"{c.payment_days} dni od daty", f"within {c.payment_days} days"
                ),
                doc(invoice),
            ],
        )

    ended = world.anomaly_contract("after_contract_end")
    for key, c, invoice, ok in (
        ("after-term", ended, inv[ended.vendor][2], False),
        ("within-term", (oc := other("after_contract_end")), inv[oc.vendor][2], True),
    ):
        question = (
            f"Was invoice {invoice.number} from {c.vendor.name} issued within the term of their contract?"
            if c.language == "en"
            else f"Czy faktura {invoice.number} wystawiona przez {c.vendor.name} została wystawiona w okresie obowiązywania umowy?"
        )
        d.add(
            key,
            "cross_doc",
            c.language,
            question,
            YES_NO[c.language][0 if ok else 1],
            "boolean",
            ok,
            [doc(c, long_day(c.end, c.language)), doc(invoice)],
        )

    mismatch = world.anomaly_contract("price_mismatch")
    for key, c, invoice, ok in (
        ("price-mismatch", mismatch, inv[mismatch.vendor][1], False),
        ("price-match", (oc := other("price_mismatch")), inv[oc.vendor][1], True),
    ):
        line = invoice.lines[0]
        question = (
            f"Does the unit price for {phrase(line.description)} on invoice {invoice.number} from {c.vendor.name} match the rate in their contract?"
            if c.language == "en"
            else f"Czy cena jednostkowa za „{line.description}” na fakturze {invoice.number} wystawionej przez {c.vendor.name} jest zgodna ze stawką z umowy?"
        )
        d.add(
            key,
            "cross_doc",
            c.language,
            question,
            YES_NO[c.language][0 if ok else 1],
            "boolean",
            ok,
            [doc(c, line.description), doc(invoice)],
        )

    c = other("cap_exceeded")
    first_two = inv[c.vendor][:2]
    remaining = c.cap - sum((x.net for x in first_two), Decimal("0.00"))
    question = (
        f"How much of the value cap in the contract with {c.vendor.name} remained unused after invoice {first_two[1].number}?"
        if c.language == "en"
        else f"Jaka kwota limitu wartości umowy z {c.vendor.name} pozostała do wykorzystania po fakturze {first_two[1].number}?"
    )
    d.add(
        "cap-remaining",
        "cross_doc",
        c.language,
        question,
        amt(remaining, c.currency, c.language),
        "number",
        remaining,
        contract_docs(c, first_two),
    )
    i = pick(digital(v1) + digital(v2))
    contract = world.contract_for(i.vendor)
    question = (
        f"What is the value cap of the contract under which invoice {i.number} from {i.vendor.name} was issued?"
        if i.language == "en"
        else f"Jaki jest limit wartości umowy, na podstawie której {i.vendor.name} wystawił fakturę {i.number}?"
    )
    d.add(
        "cap-of-contract",
        "cross_doc",
        i.language,
        question,
        amt(contract.cap, contract.currency, i.language),
        "number",
        contract.cap,
        [doc(i), doc(contract, cap_text(contract))],
    )

    # aggregate, scoped to this world's buyer or vendor names
    def total(invoices: list[Invoice], field: Callable[[Invoice], Decimal]) -> Decimal:
        return sum((field(x) for x in invoices), Decimal("0.00"))

    def docs_of(invoices: list[Invoice]) -> list[tuple[str, tuple[str, ...]]]:
        return [doc(x) for x in invoices]

    d.add(
        "vendor-net",
        "aggregate",
        "en",
        f"What is the total net amount invoiced by {v2.name} in 2026?",
        amt(total(inv[v2], lambda x: x.net), v2.currency, "en"),
        "number",
        total(inv[v2], lambda x: x.net),
        docs_of(inv[v2]),
    )
    d.add(
        "vendor-gross",
        "aggregate",
        "pl",
        f"Jaka jest łączna kwota brutto faktur wystawionych przez {v0.name} w 2026 r.?",
        amt(total(inv[v0], lambda x: x.gross), "PLN", "pl"),
        "number",
        total(inv[v0], lambda x: x.gross),
        docs_of(inv[v0]),
    )
    d.add(
        "vendor-vat",
        "aggregate",
        "pl",
        f"Ile łącznie podatku VAT naliczył {v1.name} na swoich fakturach w 2026 r.?",
        amt(total(inv[v1], lambda x: x.vat), "PLN", "pl"),
        "number",
        total(inv[v1], lambda x: x.vat),
        docs_of(inv[v1]),
    )
    d.add(
        "vendor-count",
        "aggregate",
        "en",
        f"How many invoices did {v3.name} issue in 2026?",
        "3",
        "number",
        3,
        docs_of(inv[v3]),
    )
    q1_pln = [x for x in inv[v0] + inv[v1] if x.quarter == 1]
    d.add(
        "q1-pln-gross",
        "aggregate",
        "pl",
        f"Jaka jest łączna kwota brutto faktur w PLN wystawionych dla {b.name} w pierwszym kwartale 2026 r.?",
        amt(total(q1_pln, lambda x: x.gross), "PLN", "pl"),
        "number",
        total(q1_pln, lambda x: x.gross),
        docs_of(q1_pln),
    )
    d.add(
        "eur-total",
        "aggregate",
        "en",
        f"What is the total amount invoiced to {b.name} in EUR in 2026?",
        amt(total(inv[v3], lambda x: x.gross), "EUR", "en"),
        "number",
        total(inv[v3], lambda x: x.gross),
        docs_of(inv[v3]),
    )
    q2 = [x for x in inv[v0] if x.quarter == 2]
    d.add(
        "q2-net",
        "aggregate",
        "pl",
        f"Ile wyniosła łączna wartość netto faktur od {v0.name} w drugim kwartale 2026 r.?",
        amt(total(q2, lambda x: x.net), "PLN", "pl"),
        "number",
        total(q2, lambda x: x.net),
        docs_of(q2),
    )
    pl_receipts = [r for r in world.receipts if r.language == "pl"]
    receipts_total = sum((r.total for r in pl_receipts), Decimal("0.00"))
    d.add(
        "receipts-pln",
        "aggregate",
        "pl",
        f"Jaka jest łączna kwota dwóch paragonów w PLN: od sprzedawcy „{pl_receipts[0].merchant.name}” z dnia {day(pl_receipts[0].issued, 'pl')} i od sprzedawcy „{pl_receipts[1].merchant.name}” z dnia {day(pl_receipts[1].issued, 'pl')}?",
        amt(receipts_total, "PLN", "pl"),
        "number",
        receipts_total,
        [(r.doc_id, ()) for r in pl_receipts],
    )
    largest = max(inv[v2], key=lambda x: x.gross)
    d.add(
        "largest-invoice",
        "aggregate",
        "en",
        f"Which invoice from {v2.name} had the highest total amount?",
        largest.number,
        "text",
        largest.number,
        docs_of(inv[v2]),
        accepted=(largest.number,),
    )
    reverse = [x for x in world.invoices if x.reverse_charge]
    d.add(
        "reverse-charge-count",
        "aggregate",
        "en",
        f"How many invoices addressed to {b.name} were issued under the reverse charge mechanism?",
        str(len(reverse)),
        "number",
        len(reverse),
        docs_of(reverse),
    )
    q3 = [x for x in inv[v2] if x.quarter == 3]
    d.add(
        "q3-net",
        "aggregate",
        "en",
        f"What was the total net amount invoiced by {v2.name} in Q3 2026?",
        amt(total(q3, lambda x: x.net), v2.currency, "en"),
        "number",
        total(q3, lambda x: x.net),
        docs_of(q3),
    )
    year_end = [c for c in world.contracts if c.end == date(2026, 12, 31)]
    d.add(
        "contracts-to-year-end",
        "aggregate",
        "pl",
        f"Ile umów zawartych przez {b.name} obowiązuje do 31 grudnia 2026 r.?",
        str(len(year_end)),
        "number",
        len(year_end),
        [doc(c, long_day(c.end, c.language)) for c in world.contracts],
    )

    # cross_lingual: Polish questions about English documents and the reverse
    i = pick(digital(v2))
    d.add(
        "pl-on-en-invoice",
        "cross_lingual",
        "pl",
        f"Jaka jest kwota do zapłaty na fakturze {i.number} od {v2.name}?",
        amt(i.gross, i.currency, "pl"),
        "number",
        i.gross,
        [doc(i)],
    )
    d.add(
        "pl-on-en-payment-days",
        "cross_lingual",
        "pl",
        f"W ciągu ilu dni należy opłacić faktury zgodnie z umową {c3.number} z {v3.name}?",
        f"{c3.payment_days} dni",
        "number",
        c3.payment_days,
        [doc(c3)],
    )
    law_pl = (
        ("England and Wales", "Anglii i Walii", "angielskie")
        if "England" in c2.governing_law
        else ("Ireland", "Irlandii", "irlandzkie")
    )
    d.add(
        "pl-on-en-law",
        "cross_lingual",
        "pl",
        f"Jakie prawo reguluje umowę {c2.number} z {v2.name}?",
        law_pl[1],
        "text",
        law_pl[0],
        [(c2.doc_id, ("are governed by",))],
        accepted=law_pl,
    )
    d.add(
        "pl-on-en-end",
        "cross_lingual",
        "pl",
        f"Do kiedy obowiązuje umowa {c3.number} z {v3.name}?",
        long_day(c3.end, "pl"),
        "date",
        c3.end,
        [doc(c3)],
    )
    i = pick(digital(v0))
    d.add(
        "en-on-pl-due",
        "cross_lingual",
        "en",
        f"What is the due date of invoice {i.number} from {v0.name}?",
        day(i.due, "en"),
        "date",
        i.due,
        [doc(i)],
    )
    d.add(
        "en-on-pl-cap",
        "cross_lingual",
        "en",
        f"What is the value cap in agreement no. {c1.number} with {v1.name}?",
        amt(c1.cap, "PLN", "en"),
        "number",
        c1.cap,
        [doc(c1)],
    )
    d.add(
        "en-on-pl-notice",
        "cross_lingual",
        "en",
        f"What notice period applies to agreement no. {c1.number} with {v1.name}?",
        f"{c1.notice_days} days",
        "number",
        c1.notice_days,
        [doc(c1)],
    )
    i = pick(digital(v1) or inv[v1])
    d.add(
        "en-on-pl-vat",
        "cross_lingual",
        "en",
        f"How much VAT is charged on invoice {i.number} from {v1.name}?",
        amt(i.vat, "PLN", "en"),
        "number",
        i.vat,
        [doc(i)],
    )
    r = world.receipts[0]
    d.add(
        "en-on-pl-receipt",
        "cross_lingual",
        "en",
        f"What was the total on the receipt from {r.merchant.name} dated {day(r.issued, 'en')}?",
        amt(r.total, "PLN", "en"),
        "number",
        r.total,
        [(r.doc_id, ())],
    )

    # unanswerable: facts these documents do not contain
    used = {x.number for x in world.invoices}
    fake = next(
        f"INV-26{n:04d}"
        for n in rng.sample(range(1000, 9999), 50)
        if f"INV-26{n:04d}" not in used
    )
    unanswerable: list[tuple[str, Language, str, list[tuple[str, tuple[str, ...]]]]] = [
        (
            "late-interest",
            "en",
            f"What late-payment interest rate does the service agreement {c2.number} with {v2.name} specify?",
            [doc(c2)],
        ),
        (
            "invoice-signatory",
            "pl",
            f"Kto podpisał fakturę {inv[v0][0].number} wystawioną przez {v0.name} w imieniu sprzedawcy?",
            [doc(inv[v0][0])],
        ),
        (
            "delivery-address",
            "en",
            f"What is the delivery address on invoice {inv[v3][0].number} from {v3.name}?",
            [doc(inv[v3][0])],
        ),
        (
            "missing-invoice",
            "en",
            f"What is the total of invoice {fake} from {v2.name}?",
            [],
        ),
        (
            "seller-phone",
            "pl",
            f"Jaki numer telefonu sprzedawcy podano na fakturze {inv[v1][2].number} wystawionej przez {v1.name}?",
            [doc(inv[v1][2])],
        ),
        (
            "termination-penalty",
            "en",
            f"What penalty does the service agreement {c3.number} with {v3.name} set for early termination?",
            [doc(c3)],
        ),
        (
            "early-payment-discount",
            "pl",
            f"Jaki rabat za wcześniejszą płatność przewiduje umowa nr {c1.number} z {v1.name}?",
            [doc(c1)],
        ),
    ]
    for key, language, question, docs in unanswerable:
        d.add(
            key,
            "unanswerable",
            language,
            question,
            REFUSAL[language],
            "refusal",
            "refusal",
            docs,
        )
    return d.items


def clean_party(text: str) -> str:
    """'iBio Inc. ("iBio")' -> 'iBio Inc.'; placeholders such as '[]' become ''."""
    name = re.sub(r"\s*\([^)]*\)", "", text)
    name = name.replace('"', "").replace("“", "").replace("”", "").strip(" ,;")
    return name if len(re.findall(r"[A-Za-z]", name)) >= 2 else ""


NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def law_forms(law: str) -> tuple[str, ...]:
    """'British Columbia, Canada' also accepts 'British Columbia'."""
    first = law.split(",")[0].strip()
    return (law, first) if first != law else (law,)


def term_forms(term: str) -> tuple[str, ...]:
    """'successive 1 year' also accepts '1 year', 'one year', 'one-year' and '12 months'."""
    forms = [term]
    for count, unit in re.findall(r"(\d+)\s*(year|month)", term):
        n = int(count)
        word = NUMBER_WORDS.get(n, count)
        forms += [f"{n} {unit}", f"{word} {unit}", f"{word}-{unit}", f"{n}-{unit}"]
        if unit == "year":
            forms.append(f"{12 * n} months")
    return tuple(dict.fromkeys(forms))


def parse_us_date(text: str) -> date | None:
    text = text.strip()
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", text)
    if match:
        month, day_, year = (int(part) for part in match.groups())
        year = year + (2000 if year < 50 else 1900) if year < 100 else year
        try:
            return date(year, month, day_)
        except ValueError:
            return None
    for pattern in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def clauses(c: CuadContract, field: str) -> tuple[str, ...]:
    return tuple(c.clauses.get(field, [])[:3])


def cuad_drafts(contracts: list[CuadContract], pages: PageFinder) -> list[GoldenItem]:
    items: list[GoldenItem] = []
    for split in EVAL_SPLITS:
        chosen = [c for c in contracts if c.split == split]
        d = Drafts(split, "cuad", pages)
        cross_lingual_done = False
        for n, c in enumerate(chosen):
            parties = [
                clean_party(p)
                for p in c.answers["Parties"].split(";")
                if clean_party(p)
            ]
            if not parties:
                continue
            name = c.answers["Document Name"].title()
            subject = (
                f"the {name} between {parties[0]} and {parties[1]}"
                if len(parties) > 1
                else f"the {name} of {parties[0]}"
            )
            agreement = parse_us_date(c.answers["Agreement Date"])
            expiry = parse_us_date(c.answers["Expiration Date"])
            key = c.doc_id.removeprefix("cuad-")
            kinds = ["law", "agreement-date", "term", "party"]
            kind = kinds[n % len(kinds)]
            if kind == "agreement-date" and agreement is None:
                kind = "law"
            if kind == "term" and expiry is None and not c.answers["Renewal Term"]:
                kind = "law"
            if kind == "party" and len(parties) < 2:
                kind = "law"
            if kind == "law":
                law = c.answers["Governing Law"]
                d.add(
                    f"{key}-law",
                    "single_fact",
                    "en",
                    f"Which jurisdiction's law governs {subject}?",
                    law,
                    "text",
                    law,
                    [(c.doc_id, clauses(c, "Governing Law"))],
                    accepted=law_forms(law),
                )
            elif kind == "agreement-date":
                assert agreement is not None
                d.add(
                    f"{key}-agreement-date",
                    "single_fact",
                    "en",
                    f"On what date was {subject} entered into?",
                    day(agreement, "en"),
                    "date",
                    agreement,
                    [(c.doc_id, clauses(c, "Agreement Date"))],
                )
            elif kind == "term" and expiry is not None:
                d.add(
                    f"{key}-expiry",
                    "single_fact",
                    "en",
                    f"When does {subject} expire?",
                    day(expiry, "en"),
                    "date",
                    expiry,
                    [(c.doc_id, clauses(c, "Expiration Date"))],
                )
            elif kind == "term":
                renewal = c.answers["Renewal Term"]
                d.add(
                    f"{key}-renewal",
                    "single_fact",
                    "en",
                    f"What is the renewal term of {subject}?",
                    renewal,
                    "text",
                    renewal,
                    [(c.doc_id, clauses(c, "Renewal Term"))],
                    accepted=term_forms(renewal),
                )
            else:
                d.add(
                    f"{key}-party",
                    "single_fact",
                    "en",
                    f"Who is the other party to the {name} with {parties[0]}?",
                    parties[1],
                    "text",
                    parties[1],
                    [(c.doc_id, clauses(c, "Parties"))],
                    accepted=(parties[1],),
                )
            if (
                not cross_lingual_done
                and kind != "agreement-date"
                and agreement is not None
                and len(parties) > 1
            ):
                d.add(
                    f"{key}-pl-agreement-date",
                    "cross_lingual",
                    "pl",
                    f"Kiedy zawarto umowę „{name}” pomiędzy {parties[0]} a {parties[1]}?",
                    long_day(agreement, "pl"),
                    "date",
                    agreement,
                    [(c.doc_id, clauses(c, "Agreement Date"))],
                )
                cross_lingual_done = True
        items += d.items
    return items


def sroie_drafts(receipts: list[SroieReceipt], pages: PageFinder) -> list[GoldenItem]:
    """Six receipts per split. A question names the receipt by company and date (asking
    for the total) or company and total (asking for the date); a receipt is skipped
    when that pair would match another receipt anywhere in the corpus."""
    by_date = Counter((r.company, r.date) for r in receipts)
    by_total = Counter((r.company, r.total) for r in receipts)
    items: list[GoldenItem] = []
    for split in EVAL_SPLITS:
        d = Drafts(split, "sroie", pages)
        n = 0
        for r in (x for x in receipts if x.split == split):
            if n == 6:
                break
            ask_total = n % 2 == 0
            identifier = (r.company, r.date if ask_total else r.total)
            if (by_date if ask_total else by_total)[identifier] > 1:
                continue
            n += 1
            issued = date.fromisoformat(r.date)
            key = r.doc_id.removeprefix("sroie-")
            if ask_total:
                d.add(
                    f"{key}-total",
                    "image",
                    "en",
                    f"What is the total amount on the receipt from {r.company} dated {issued:%d/%m/%Y}?",
                    f"RM {r.total}",
                    "number",
                    r.total,
                    [(r.doc_id, ())],
                )
            else:
                d.add(
                    f"{key}-date",
                    "image",
                    "en",
                    f"On what date was the receipt from {r.company} with a total of RM {r.total} issued?",
                    day(issued, "en"),
                    "date",
                    issued,
                    [(r.doc_id, ())],
                )
        items += d.items
    return items


def draft_golden(records: list[DocumentRecord] | None = None) -> list[GoldenItem]:
    records = records or read_manifest()
    pages = PageFinder({r.doc_id: r for r in records})
    items = []
    for index, split in WORLD_SPLITS.items():
        if split in ("dev", "test"):
            items += world_drafts(build_world(index), pages)
    items += cuad_drafts(load_annotations(), pages)
    items += sroie_drafts(load_keys(), pages)
    return items
