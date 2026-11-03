"""Container healthcheck: `python -m docai.ingest.health`.

Exits 0 when the worker loop, or its heartbeat during a long document, touched the
health file recently. It imports only the standard library, so it answers within the
healthcheck timeout even while the worker is busy; `docai` would import LangChain first.
"""

import os
import sys
import time
from pathlib import Path

HEALTH_FILE = Path("/tmp/ingestion-worker-alive")
MAX_AGE_SECONDS = 120


def healthy(path: Path, max_age_seconds: float = MAX_AGE_SECONDS) -> bool:
    try:
        return time.time() - path.stat().st_mtime < max_age_seconds
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(0 if healthy(Path(os.environ.get("HEALTH_FILE", HEALTH_FILE))) else 1)
