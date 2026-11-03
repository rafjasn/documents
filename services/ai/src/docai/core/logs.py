"""Process-wide logging setup for the CLI and the services."""

import logging


def configure_logging(level: int = logging.INFO) -> None:
    """Log docai messages to stderr. Leaves existing handlers alone."""
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Per-request lines from the HTTP and Gemini clients drown out the pipeline's own.
    for name in ("httpx", "google_genai.models"):
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
