import csv
from datetime import date
from pathlib import Path

import pytest

from docai.corpus.manifest import DocumentRecord
from docai.corpus.splits import split_problems
from docai.evals.golden import (
    GoldenItem,
    Relevant,
    apply_review,
    read_items,
    write_items,
    write_review,
)
from docai.evals.questions import (
    clean_party,
    law_forms,
    parse_us_date,
    phrase,
    term_forms,
)


def golden(id_: str, question: str = "What is the total?") -> GoldenItem:
    return GoldenItem(
        id=id_,
        split="dev",
        type="single_fact",
        language="en",
        question=question,
        answer="£10.00",
        kind="number",
        value="10.00",
        relevant=(Relevant(doc_id="d", page=1),),
        source="template",
    )


def test_items_round_trip(tmp_path: Path) -> None:
    items = [golden("b"), golden("a")]
    write_items(items, tmp_path / "v1.jsonl")
    assert [i.id for i in read_items(tmp_path / "v1.jsonl")] == ["a", "b"]


def test_review_applies_drops_and_edits(tmp_path: Path) -> None:
    drafts = [golden("a"), golden("b"), golden("c")]
    sheet = tmp_path / "review.csv"
    write_review(
        drafts, titles={"d": "Invoice 1"}, files={"d": "corpus/data/d.pdf"}, path=sheet
    )
    rows = list(csv.DictReader(sheet.open(encoding="utf-8-sig")))
    assert {r["decision"] for r in rows} == {"keep"}
    assert rows[0]["documents"] == "d p.1 Invoice 1"
    rows[1]["decision"] = "drop"
    rows[2]["question"] = "What is the amount due?"
    rows[2]["accepted"] = "10 | ten"
    with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    kept = {i.id: i for i in apply_review(drafts, sheet)}
    assert set(kept) == {"a", "c"}
    assert kept["c"].question == "What is the amount due?"
    assert kept["c"].accepted == ("10", "ten")


def test_review_rejects_unknown_decisions(tmp_path: Path) -> None:
    sheet = tmp_path / "review.csv"
    write_review([golden("a")], titles={}, files={}, path=sheet)
    sheet.write_text(
        sheet.read_text(encoding="utf-8").replace("keep,", "maybe,"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="keep or drop"):
        apply_review([golden("a")], sheet)


def record(
    doc_id: str, split: str, group: str, source: str = "synthetic"
) -> DocumentRecord:
    return DocumentRecord.model_validate(
        {
            "doc_id": doc_id,
            "source": source,
            "title": doc_id,
            "category": "INVOICE",
            "language": "en",
            "format": "pdf",
            "path": f"{doc_id}.pdf",
            "split": split,
            "group": group,
            "license": "x",
            "sha256": "0",
        }
    )


def test_split_problems_catch_leakage() -> None:
    assert split_problems([record("a", "dev", "w1"), record("b", "dev", "w1")]) == []
    assert split_problems([record("a", "dev", "w1"), record("b", "test", "w1")]) == [
        "group w1 is in several splits: ['dev', 'test']"
    ]
    assert (
        "public documents"
        in split_problems([record("c", "dev", "x", source="cuad")])[0]
    )


def test_question_helpers() -> None:
    assert phrase("Road transport Warsaw-Berlin") == "road transport Warsaw-Berlin"
    assert phrase("IT support") == "IT support"
    assert clean_party('iBio Inc. ("iBio")') == "iBio Inc."
    assert clean_party("[]") == ""
    assert law_forms("British Columbia, Canada") == (
        "British Columbia, Canada",
        "British Columbia",
    )
    assert "one-year" in term_forms("successive 1 year") and "12 months" in term_forms(
        "successive 1 year"
    )
    assert parse_us_date("6/16/04") == date(2004, 6, 16)
    assert parse_us_date("perpetual") is None
