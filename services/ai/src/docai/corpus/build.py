"""`docai corpus build`: download, select and generate the corpus, then write the manifest."""

from docai.core.download import download
from docai.corpus.cuad import build_cuad
from docai.corpus.fonts import ARCHIVE, ARCHIVE_SHA256, ARCHIVE_URL
from docai.corpus.manifest import DocumentRecord, write_manifest
from docai.corpus.splits import split_problems
from docai.corpus.sroie import build_sroie
from docai.corpus.synthetic.build import build_synthetic


def build_corpus() -> list[DocumentRecord]:
    download(ARCHIVE_URL, ARCHIVE, ARCHIVE_SHA256)
    records = build_cuad() + build_sroie() + build_synthetic()
    problems = split_problems(records)
    if problems:
        raise ValueError("Split leakage: " + "; ".join(problems))
    write_manifest(records)
    return records
