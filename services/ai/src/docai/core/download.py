"""Checksummed downloads of pinned source files."""

import hashlib
import time
import urllib.request
from pathlib import Path


def download(
    url: str, path: Path, sha256: str | None = None, attempts: int = 3
) -> Path:
    """Download `url` to `path` unless it already exists with the expected checksum."""
    if path.exists() and (sha256 is None or file_sha256(path) == sha256):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                data = response.read()
            break
        except OSError:
            if attempt == attempts:
                raise
            time.sleep(2 * attempt)
    if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"Checksum mismatch for {url}")
    path.write_bytes(data)
    return path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
