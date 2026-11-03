"""Render every synthetic world to files and manifest records."""

import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import pypdfium2 as pdfium

from docai.corpus.manifest import DATA_DIR, DocumentRecord, Split, sha256_of
from docai.corpus.synthetic.contract_text import contract_text
from docai.corpus.synthetic.render_docx import contract_docx
from docai.corpus.synthetic.render_image import photograph, receipt_paper, scan_pdf
from docai.corpus.synthetic.render_pdf import contract_pdf, invariant_pdfs, invoice_pdf
from docai.corpus.synthetic.text import day
from docai.corpus.synthetic.world import Contract, Invoice, Receipt, World, build_worlds

LICENSE = "Synthetic, generated for this project"
WORLD_SPLITS: dict[int, Split] = {
    0: "train",
    1: "train",
    2: "train",
    3: "dev",
    4: "test",
}


def contract_title(c: Contract) -> str:
    kind = "Umowa o świadczenie usług nr" if c.language == "pl" else "Service Agreement"
    return f"{kind} {c.number} – {c.vendor.name}"


def invoice_title(i: Invoice) -> str:
    kind = "Faktura VAT" if i.language == "pl" else "Invoice"
    return f"{kind} {i.number} – {i.vendor.name}"


def receipt_title(r: Receipt) -> str:
    kind = "Paragon" if r.language == "pl" else "Receipt"
    return f"{kind} – {r.merchant.name} – {day(r.issued, r.language)}"


def page_count(path: Path) -> int:
    document = pdfium.PdfDocument(path)
    try:
        return len(document)
    finally:
        document.close()


def to_json(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(type(value))


def build_world_files(world: World, root: Path) -> list[DocumentRecord]:
    folder = root / f"w{world.index}"
    folder.mkdir(parents=True, exist_ok=True)
    split = WORLD_SPLITS[world.index]
    records = []

    def record(
        doc_id: str,
        title: str,
        category: str,
        language: str,
        fmt: str,
        file: Path,
        pages: int | None,
    ) -> DocumentRecord:
        return DocumentRecord.model_validate(
            {
                "doc_id": doc_id,
                "source": "synthetic",
                "title": title,
                "category": category,
                "language": language,
                "format": fmt,
                "path": str(file.relative_to(DATA_DIR)),
                "split": split,
                "group": world.group,
                "license": LICENSE,
                "sha256": sha256_of(file),
                "pages": pages,
            }
        )

    numbers = {c.doc_id: c.number for c in world.contracts}
    for c in world.contracts:
        text = contract_text(c)
        if c.format == "pdf":
            file = folder / f"{c.doc_id}.pdf"
            contract_pdf(text, c.vendor.name, c.language, file)
            pages: int | None = page_count(file)
        else:
            file = folder / f"{c.doc_id}.docx"
            contract_docx(text, c.vendor.name, c.fee_table_as_image, file)
            pages = None
        records.append(
            record(
                c.doc_id,
                contract_title(c),
                "CONTRACT",
                c.language,
                c.format,
                file,
                pages,
            )
        )

    for i in world.invoices:
        file = folder / f"{i.doc_id}.pdf"
        if i.format == "scanned_pdf":
            digital = folder / f"{i.doc_id}.digital.pdf"
            invoice_pdf(i, numbers[i.contract_id], digital)
            pages = scan_pdf(digital, file, i.doc_id)
            digital.unlink()
        else:
            invoice_pdf(i, numbers[i.contract_id], file)
            pages = page_count(file)
        records.append(
            record(
                i.doc_id, invoice_title(i), "INVOICE", i.language, i.format, file, pages
            )
        )

    for r in world.receipts:
        file = folder / f"{r.doc_id}.jpg"
        photograph(receipt_paper(r), r.doc_id).save(file, "JPEG", quality=74)
        records.append(
            record(r.doc_id, receipt_title(r), "RECEIPT", r.language, "photo", file, 1)
        )

    (folder / "truth.json").write_text(
        json.dumps(asdict(world), default=to_json, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return records


def build_synthetic(root: Path = DATA_DIR / "synthetic") -> list[DocumentRecord]:
    """Render every world's files. A rebuild writes the same bytes, so the committed
    manifest's hashes hold on a fresh clone."""
    with invariant_pdfs():
        return [
            record
            for world in build_worlds()
            for record in build_world_files(world, root)
        ]
