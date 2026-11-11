"""Runtime configuration for the lab report publishing API."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DATA_DIR = Path("/data")


def database_path() -> Path:
    """Return the SQLite database path.

    Defaults to ``/data/lab_reports.db`` so both API replicas can share one
    database file through the Docker volume.  ``LAB_DB_PATH`` overrides it
    (used by the test-suite and local runs).
    """

    override = os.environ.get("LAB_DB_PATH")
    if override:
        path = Path(override)
    else:
        path = DEFAULT_DATA_DIR / "lab_reports.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


BUSY_TIMEOUT_MS = int(os.environ.get("LAB_DB_BUSY_TIMEOUT_MS", "10000"))
MAX_RESULT_IDS_PER_REPORT = 30
MIN_RESULT_IDS_PER_REPORT = 1
