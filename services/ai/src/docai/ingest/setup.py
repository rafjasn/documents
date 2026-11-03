"""Model files the pipeline needs: Tesseract's Polish and English models, Docling's
models, and the tokenizer that sizes chunks.

Self-contained (it imports only docai.core.download) so the Docker build can run it,
`python -m docai.ingest.setup TESSDATA_DIR [DOCLING_DIR [HF_HUB_CACHE]]`, before the
rest of the code is copied in, and model layers survive code changes.
"""

import sys
from pathlib import Path

from docai.core.download import download

# Chunk sizes are counted in this model's tokens, whatever the embedding model.
TOKENIZER_ID = "intfloat/multilingual-e5-small"

# tesseract-ocr/tessdata_best at a pinned commit (2024-03-09): the most accurate models.
TESSDATA_COMMIT = "e12c65a915945e4c28e237a9b52bc4a8f39a0cec"
TESSDATA_SHA256 = {
    "eng": "8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba",
    "pol": "e80cc4cefbdface06e9223f43f089556b9dcf104020fbc0a200f6863c57d4405",
    # Orientation detection. Unused, but Docling's Tesseract stage won't start without it.
    "osd": "9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff",
}


def ensure_tessdata(directory: Path) -> Path:
    """Download the pinned Polish and English models into `directory` if missing."""
    for language, sha256 in TESSDATA_SHA256.items():
        download(
            "https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/"
            f"{TESSDATA_COMMIT}/{language}.traineddata",
            directory / f"{language}.traineddata",
            sha256,
        )
    return directory


# Downloaded alongside the models the pipeline uses (PyTorch layout model, accurate
# TableFormer), but never loaded: about 300 MB.
UNUSED_DOCLING_FILES = (
    "docling-project--docling-layout-heron-onnx",
    "docling-project--docling-models/model_artifacts/tableformer/fast",
)


def download_docling_models(directory: Path) -> Path:
    """Docling's layout and table-structure models; the pipeline uses no others."""
    import shutil

    from docling.utils.model_downloader import download_models

    download_models(
        output_dir=directory,
        with_layout=True,
        with_tableformer=True,
        with_code_formula=False,
        with_picture_classifier=False,
        with_rapidocr=False,
    )
    for unused in UNUSED_DOCLING_FILES:
        shutil.rmtree(directory / unused, ignore_errors=True)
    return directory


def download_tokenizer(cache: Path | None = None) -> Path:
    """The chunking tokenizer, into a Hugging Face cache (default: the user's), where
    `Tokenizer.from_pretrained` finds it with the hub offline."""
    from huggingface_hub import hf_hub_download

    return Path(hf_hub_download(TOKENIZER_ID, "tokenizer.json", cache_dir=cache))


if __name__ == "__main__":
    ensure_tessdata(Path(sys.argv[1]))
    if len(sys.argv) > 2:
        download_docling_models(Path(sys.argv[2]))
    if len(sys.argv) > 3:
        download_tokenizer(Path(sys.argv[3]))
