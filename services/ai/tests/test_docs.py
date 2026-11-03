"""Documentation: docs/RESULTS.md is what the recorded runs render, the
README's numbers come from it, and every relative link and image resolves."""

import re
from pathlib import Path

import pytest

from docai.evals.report import report_text

REPO = Path(__file__).resolve().parents[3]
DOCS = [
    REPO / "README.md",
    *sorted((REPO / "docs").rglob("*.md")),
    REPO / "services" / "ai" / "README.md",
    REPO / "infrastructure" / "cdk" / "README.md",
]
# What a measurement looks like: money, percentages, fractions, scores, durations and
# counts with thousands separators. Plain integers (72 questions, 18 panels) are labels.
NUMBER = re.compile(
    r"\$\d+(?:\.\d+)?|\d+(?:\.\d+)?%|\b\d+/\d+\b|\b0\.\d{3}\b"
    r"|\b\d+(?:\.\d+)? m?s\b|\b\d{1,3}(?:,\d{3})+\b"
)
# RESULTS.md writes latency pairs as "17 / 24 ms".
PAIR = re.compile(r"\b(\d+(?:\.\d+)?) / (\d+(?:\.\d+)?) (m?s)\b")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section(text: str, title: str) -> str:
    start = text.index(f"\n## {title}\n")
    end = text.find("\n## ", start + 1)
    return text[start:end]


def measurements(text: str) -> set[str]:
    found = set(NUMBER.findall(text))
    for low, high, unit in PAIR.findall(text):
        found |= {f"{low} {unit}", f"{high} {unit}"}
    return found


def anchors(path: Path) -> set[str]:
    """GitHub's heading anchors: lower case, punctuation dropped, spaces as hyphens."""
    slugs: set[str] = set()
    for line in FENCE.sub("", text_of(path)).splitlines():
        if heading := re.match(r"#{1,6} (.+)", line):
            slug = re.sub(r"[^\w\- ]", "", heading.group(1).lower()).replace(" ", "-")
            slugs |= {slug, *(f"{slug}-{n}" for n in range(1, 4))}
    return slugs


def test_results_md_is_rendered_from_the_recorded_runs() -> None:
    assert text_of(REPO / "docs" / "RESULTS.md") == report_text(), (
        "run `uv run docai evals report`"
    )


@pytest.mark.parametrize("title", ["Results", "How it is operated"])
def test_the_readmes_numbers_are_in_results_md(title: str) -> None:
    readme = measurements(section(text_of(REPO / "README.md"), title))
    results = measurements(text_of(REPO / "docs" / "RESULTS.md"))
    assert readme, title
    assert sorted(readme - results) == []


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_relative_link_and_image_resolves(path: Path) -> None:
    broken = []
    for target in LINK.findall(FENCE.sub("", text_of(path))):
        if re.match(r"[a-z]+:", target):
            continue  # http(s), mailto
        file, _, anchor = target.partition("#")
        linked = (path.parent / file).resolve() if file else path
        if not linked.exists():
            broken.append(target)
        elif anchor and linked.suffix == ".md" and anchor not in anchors(linked):
            broken.append(target)
    assert broken == []
