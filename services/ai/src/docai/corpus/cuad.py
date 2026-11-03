"""CUAD v1 contracts (CC BY 4.0, The Atticus Project): selection and short answers.

Contracts are chosen deterministically from back-office agreement types with the
short answers the golden set needs, then split 24/8/8 by document.
"""

import ast
import csv
import hashlib
import io
import json
import zipfile
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

import pypdfium2 as pdfium

from docai.core.download import download
from docai.corpus.manifest import DATA_DIR, RAW_DIR, DocumentRecord, Split, sha256_of

URL = "https://zenodo.org/records/4595826/files/CUAD_v1.zip?download=1"
SHA256 = "88b694d99007d39777fa44cd72daf8297773d285dc3eab0091ba32078888d18e"
ARCHIVE = RAW_DIR / "CUAD_v1.zip"
LICENSE = "CC BY 4.0, CUAD v1 (The Atticus Project)"
ANNOTATIONS = DATA_DIR / "cuad" / "annotations.json"

TYPES = (
    "Service",
    "Maintenance",
    "Supply",
    "Outsourcing",
    "Hosting",
    "Transportation",
    "Consulting",
    "Manufacturing",
    "Distributor",
    "Reseller",
)
COUNT, MAX_PER_TYPE, PAGE_RANGE = 40, 6, (3, 30)
SPLITS: tuple[tuple[Split, int], ...] = (("train", 24), ("dev", 8), ("test", 8))
SHORT_ANSWERS = (
    "Document Name",
    "Parties",
    "Agreement Date",
    "Effective Date",
    "Expiration Date",
    "Renewal Term",
    "Governing Law",
)
YES_NO = (
    "Non-Compete",
    "Exclusivity",
    "Termination For Convenience",
    "Cap On Liability",
    "Audit Rights",
    "Insurance",
    "Minimum Commitment",
    "Anti-Assignment",
)


@dataclass(frozen=True)
class CuadContract:
    doc_id: str
    filename: str
    agreement_type: str
    split: Split
    pages: int
    answers: dict[str, str]
    clauses: dict[str, list[str]]
    yes_no: dict[str, str]


def stable_key(text: str) -> str:
    return hashlib.sha256(f"cuad:{text}".encode()).hexdigest()


def clause_list(value: str) -> list[str]:
    try:
        parsed = ast.literal_eval(value) if value.strip() else []
    except ValueError, SyntaxError:
        return [value]
    return [str(item) for item in parsed]


def build_cuad() -> list[DocumentRecord]:
    download(URL, ARCHIVE, SHA256)
    archive = zipfile.ZipFile(ARCHIVE)
    rows = list(
        csv.DictReader(
            io.TextIOWrapper(
                archive.open("CUAD_v1/master_clauses.csv"), encoding="utf-8"
            )
        )
    )
    pdfs = {
        PurePosixPath(name).name: PurePosixPath(name)
        for name in archive.namelist()
        if "/full_contract_pdf/" in name and name.lower().endswith(".pdf")
    }

    chosen: list[tuple[dict[str, str], PurePosixPath, str, int, bytes]] = []
    per_type: dict[str, int] = {}
    for row in sorted(rows, key=lambda r: stable_key(r["Filename"])):
        path = pdfs.get(row["Filename"])
        if path is None:
            continue
        agreement_type = next((t for t in TYPES if t in path.parts[-2]), None)
        answers = {name: row[f"{name}-Answer"].strip() for name in SHORT_ANSWERS}
        if (
            agreement_type is None
            or per_type.get(agreement_type, 0) >= MAX_PER_TYPE
            or not all(
                answers[n]
                for n in ("Document Name", "Parties", "Agreement Date", "Governing Law")
            )
            or not (answers["Expiration Date"] or answers["Renewal Term"])
        ):
            continue
        data = archive.read(str(path))
        document = pdfium.PdfDocument(data)
        try:
            pages = len(document)
            first_page_text = document[0].get_textpage().get_text_range()
        finally:
            document.close()
        if (
            not PAGE_RANGE[0] <= pages <= PAGE_RANGE[1]
            or len(first_page_text.strip()) < 200
        ):
            continue
        per_type[agreement_type] = per_type.get(agreement_type, 0) + 1
        chosen.append((row, path, agreement_type, pages, data))
        if len(chosen) == COUNT:
            break

    folder = DATA_DIR / "cuad"
    folder.mkdir(parents=True, exist_ok=True)
    splits = [split for split, count in SPLITS for _ in range(count)]
    records, contracts = [], []
    for (row, _, agreement_type, pages, data), split in zip(chosen, splits):
        doc_id = f"cuad-{stable_key(row['Filename'])[:8]}"
        file = folder / f"{doc_id}.pdf"
        file.write_bytes(data)
        answers = {name: row[f"{name}-Answer"].strip() for name in SHORT_ANSWERS}
        first_party = answers["Parties"].split(";")[0].split("(")[0].strip()
        records.append(
            DocumentRecord(
                doc_id=doc_id,
                source="cuad",
                title=f"{answers['Document Name']} – {first_party}",
                category="CONTRACT",
                language="en",
                format="pdf",
                path=f"cuad/{file.name}",
                split=split,
                group=doc_id,
                license=LICENSE,
                sha256=sha256_of(file),
                pages=pages,
            )
        )
        contracts.append(
            CuadContract(
                doc_id=doc_id,
                filename=row["Filename"],
                agreement_type=agreement_type,
                split=split,
                pages=pages,
                answers=answers,
                clauses={name: clause_list(row[name]) for name in SHORT_ANSWERS},
                yes_no={name: row[f"{name}-Answer"].strip() for name in YES_NO},
            )
        )
    ANNOTATIONS.write_text(
        json.dumps([asdict(c) for c in contracts], indent=1), encoding="utf-8"
    )
    return records


def load_annotations() -> list[CuadContract]:
    return [
        CuadContract(**item)
        for item in json.loads(ANNOTATIONS.read_text(encoding="utf-8"))
    ]
