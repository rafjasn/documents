from dataclasses import replace
from pathlib import Path

from qdrant_client import QdrantClient

from docai.agent.metadata import Record
from docai.core.contracts import ChatScope
from docai.core.retrieval import RetrievalConfig, Retriever
from docai.core.sparse import SparseEncoder
from docai.evals.ci import (
    BUNDLE,
    COLLECTION,
    SLACK,
    FileRecords,
    choose_questions,
    config,
    read_jsonl,
    render,
    restore,
    summarise,
    write_jsonl,
)
from docai.evals.golden import read_items
from tests.test_retrieval import FakeEmbedder
from tests.test_runner import golden


class SmallEmbedder(FakeEmbedder):
    """FakeEmbedder's three-dimensional vectors, with a model that says so."""

    def __init__(self) -> None:
        super().__init__()
        self.model = replace(self.model, dim=3)


def test_the_bundle_restores_into_a_searchable_collection(tmp_path: Path) -> None:
    write_jsonl(
        tmp_path / "chunks.jsonl.gz",
        [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "user_id": "corpus",
                "doc_id": "doc-1",
                "chunk_id": 0,
                "text": "Invoice FV/1 total 100.00",
                "context": "An invoice from Herba.",
                "headings": ["Totals"],
                "pages": [1],
            }
        ],
    )
    client = QdrantClient(location=":memory:")

    assert restore(client, tmp_path, SmallEmbedder(), SparseEncoder()) == 1
    assert restore(client, tmp_path, SmallEmbedder(), SparseEncoder()) == 1

    retriever = Retriever(client, COLLECTION, SmallEmbedder(), SparseEncoder())
    [hit] = retriever.search("invoice FV/1", "corpus", RetrievalConfig(sparse=True))
    assert hit.doc_id == "doc-1" and hit.payload["context"] == "An invoice from Herba."


def test_file_records_serve_only_the_evaluation_tenant_in_scope() -> None:
    records = FileRecords(
        [
            Record("doc-1", "1.pdf", "invoice", {"kind": "invoice"}, "pl"),
            Record("doc-2", "2.pdf", "contract", {"kind": "contract"}, "en"),
        ]
    )

    assert [r.doc_id for r in records.load("corpus")] == ["doc-1", "doc-2"]
    assert [r.doc_id for r in records.load("corpus", ChatScope(languages=["en"]))] == [
        "doc-2"
    ]
    assert records.load("someone-else") == []


def test_below_the_threshold_the_job_fails_and_lists_the_failures() -> None:
    results = [
        {"id": f"q{i}", "type": "aggregate", "reference": "5", "answer": "4", "correct": i < 2, "error": None, "cost_usd": 0.003}
        for i in range(3)
    ]  # fmt: skip

    report = summarise(results, {"min_correct": 3, "baseline": "3/3"})

    assert report["passed"] is False and report["correct"] == 2
    assert "FAILED" in render(report, results) and "| q2 | 5 | 4 |" in render(
        report, results
    )


def test_the_committed_bundle_is_dev_questions_on_documents_it_holds() -> None:
    setup = config()
    items = {i.id: i for i in read_items()}
    documents = {r["doc_id"] for r in read_jsonl(BUNDLE / "records.jsonl.gz")}
    assert 25 <= len(setup["questions"]) <= 35
    assert setup["min_correct"] < len(setup["questions"])
    for question in setup["questions"]:
        assert items[question].split == "dev"  # never the held-out test split
        assert not question.startswith("sroie-")
    assert len(documents) == 140  # CUAD and synthetic; SROIE stays out


def test_questions_are_chosen_however_they_are_scored() -> None:
    """CI judges what its rules can't, so a question the baseline run's judge decided
    is chosen like any other (it used to be skipped)."""
    items = [
        golden("agg-1", "aggregate", "number", "1"),
        golden("agg-2", "aggregate", "number", "2"),
        golden("law-1", "single_fact", "text", "England", docs=("doc-2",)),
        golden("law-2", "single_fact", "text", "England", docs=("elsewhere",)),
    ]
    run = {
        "run_id": "r-A1-dev",
        "items": [
            {"id": "agg-1", "correct": True, "method": "judge"},
            {"id": "agg-2", "correct": False, "method": "rule"},
            {"id": "law-1", "correct": True, "method": "rule"},
        ],
    }

    setup = choose_questions(items, {"doc-1", "doc-2"}, run)

    assert setup["questions"] == ["agg-1", "agg-2", "law-1"]  # law-2: not in the bundle
    assert setup["baseline"] == "2/3 in r-A1-dev"
    assert setup["min_correct"] == 2 - SLACK
