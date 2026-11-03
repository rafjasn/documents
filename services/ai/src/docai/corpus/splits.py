"""Split checks: no document or group may appear in more than one split."""

from collections import Counter, defaultdict
from collections.abc import Sequence

from docai.corpus.manifest import DocumentRecord


def split_problems(records: Sequence[DocumentRecord]) -> list[str]:
    problems = [
        f"duplicate doc_id {doc_id}"
        for doc_id, count in Counter(r.doc_id for r in records).items()
        if count > 1
    ]
    splits_by_group: dict[str, set[str]] = defaultdict(set)
    for record in records:
        splits_by_group[record.group].add(record.split)
        if record.source != "synthetic" and record.group != record.doc_id:
            problems.append(f"{record.doc_id}: public documents are split one by one")
    problems += [
        f"group {group} is in several splits: {sorted(splits)}"
        for group, splits in splits_by_group.items()
        if len(splits) > 1
    ]
    return problems


def split_summary(records: Sequence[DocumentRecord]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for record in records:
        summary[record.source][record.split] += 1
    return {source: dict(counts) for source, counts in summary.items()}
