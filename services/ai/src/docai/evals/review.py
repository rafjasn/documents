"""Draft the golden set for hand review, then turn the reviewed sheet into v1.jsonl."""

from docai.corpus.manifest import read_manifest
from docai.evals.golden import (
    GOLDEN_PATH,
    REVIEW_PATH,
    GoldenItem,
    apply_review,
    write_items,
    write_review,
)
from docai.evals.questions import draft_golden


def golden_problems(items: list[GoldenItem]) -> list[str]:
    """Every relevant document must exist and sit in the question's own split."""
    splits = {r.doc_id: r.split for r in read_manifest()}
    problems = []
    for item in items:
        for relevant in item.relevant:
            if relevant.doc_id not in splits:
                problems.append(f"{item.id}: unknown document {relevant.doc_id}")
            elif splits[relevant.doc_id] != item.split:
                problems.append(
                    f"{item.id}: {relevant.doc_id} is in {splits[relevant.doc_id]}, not {item.split}"
                )
    return problems


def draft_review(force: bool = False) -> int:
    if REVIEW_PATH.exists() and not force:
        raise FileExistsError(
            f"{REVIEW_PATH} exists; pass --force to overwrite your review"
        )
    records = read_manifest()
    items = draft_golden(records)
    problems = golden_problems(items)
    if problems:
        raise ValueError("; ".join(problems))
    write_review(
        items,
        titles={r.doc_id: r.title for r in records},
        files={r.doc_id: f"corpus/data/{r.path}" for r in records},
    )
    return len(items)


def finish_review() -> list[GoldenItem]:
    items = apply_review(draft_golden())
    problems = golden_problems(items)
    if problems:
        raise ValueError("; ".join(problems))
    write_items(items, GOLDEN_PATH)
    return items
