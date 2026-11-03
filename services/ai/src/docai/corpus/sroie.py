"""SROIE receipts (ICDAR 2019): scanned Malaysian receipts with labelled key fields.

Downloaded from a pinned commit of the zzzDavid/ICDAR-2019-SROIE mirror. The data
is for research use, so it is used for local evaluation only, never in the public demo.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date

from docai.core.download import download
from docai.corpus.manifest import DATA_DIR, RAW_DIR, DocumentRecord, Split, sha256_of

COMMIT = "27be4271b251c256f695acbade9a801bffe85994"
BASE_URL = f"https://raw.githubusercontent.com/zzzDavid/ICDAR-2019-SROIE/{COMMIT}/data"
LICENSE = "ICDAR 2019 SROIE; research use only, excluded from the public demo"
KEYS = DATA_DIR / "sroie" / "keys.json"
FILE_COUNT, COUNT = 626, 60
SPLITS: tuple[tuple[Split, int], ...] = (("train", 36), ("dev", 12), ("test", 12))
DATE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$")


@dataclass(frozen=True)
class SroieReceipt:
    doc_id: str
    source_file: str
    split: Split
    company: str
    date: str  # ISO 8601
    address: str
    total: str


def parse_date(text: str) -> date | None:
    match = DATE.match(text.strip())
    if not match:
        return None
    day_, month, year = (int(part) for part in match.groups())
    year = year + 2000 if year < 100 else year
    try:
        return date(year, month, day_)
    except ValueError:
        return None


def build_sroie() -> list[DocumentRecord]:
    order = sorted(
        range(FILE_COUNT),
        key=lambda n: hashlib.sha256(f"sroie:{n}".encode()).hexdigest(),
    )
    folder = DATA_DIR / "sroie"
    folder.mkdir(parents=True, exist_ok=True)
    seen: set[tuple[str, str, str]] = set()
    chosen: list[tuple[str, dict[str, str], date]] = []
    for n in order:
        stem = f"{n:03d}"
        key_file = download(
            f"{BASE_URL}/key/{stem}.json", RAW_DIR / "sroie" / "key" / f"{stem}.json"
        )
        key = json.loads(key_file.read_text(encoding="utf-8"))
        issued = parse_date(key.get("date", ""))
        # The dataset repeats some receipts with differently formatted dates.
        fields = (
            key.get("company", "").strip(),
            issued.isoformat() if issued else "",
            key.get("total", "").strip(),
        )
        if (
            issued is None
            or not all(fields)
            or not key.get("address", "").strip()
            or not re.fullmatch(r"\d+(\.\d{2})?", fields[2])
            or fields in seen
        ):
            continue
        seen.add(fields)
        chosen.append((stem, key, issued))
        if len(chosen) == COUNT:
            break

    splits = [split for split, count in SPLITS for _ in range(count)]
    records, receipts = [], []
    for (stem, key, issued), split in zip(chosen, splits):
        doc_id = f"sroie-{stem}"
        image = download(
            f"{BASE_URL}/img/{stem}.jpg", RAW_DIR / "sroie" / "img" / f"{stem}.jpg"
        )
        file = folder / f"{doc_id}.jpg"
        file.write_bytes(image.read_bytes())
        company = key["company"].strip()
        records.append(
            DocumentRecord(
                doc_id=doc_id,
                source="sroie",
                title=f"Receipt – {company} – {issued:%d/%m/%Y}",
                category="RECEIPT",
                language="en",
                format="image",
                path=f"sroie/{file.name}",
                split=split,
                group=doc_id,
                license=LICENSE,
                sha256=sha256_of(file),
                pages=1,
            )
        )
        receipts.append(
            SroieReceipt(
                doc_id=doc_id,
                source_file=f"{stem}.jpg",
                split=split,
                company=company,
                date=issued.isoformat(),
                address=key["address"].strip(),
                total=key["total"].strip(),
            )
        )
    KEYS.write_text(
        json.dumps([asdict(r) for r in receipts], indent=1), encoding="utf-8"
    )
    return records


def load_keys() -> list[SroieReceipt]:
    return [
        SroieReceipt(**item) for item in json.loads(KEYS.read_text(encoding="utf-8"))
    ]
