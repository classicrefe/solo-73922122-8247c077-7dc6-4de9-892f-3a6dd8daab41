"""Test-only ASGI wrapper adding a self-shutdown route.

The restricted CI sandbox forbids sending signals to child processes, so
the multi-process tests stop uvicorn through an HTTP route that calls
``os._exit``.  Never used by the production application (not packaged in the
Docker image; see ``.dockerignore``).
"""

from __future__ import annotations

import os

from app.main import app as base_app

_SHUTDOWN_PATH = os.environ["LAB_TEST_SHUTDOWN_PATH"]


@base_app.get(_SHUTDOWN_PATH, include_in_schema=False)
def _lab_test_shutdown() -> dict[str, str]:
    # Flush is unnecessary for our read-only-of-stdout usage; exit immediately.
    os._exit(0)


app = base_app
