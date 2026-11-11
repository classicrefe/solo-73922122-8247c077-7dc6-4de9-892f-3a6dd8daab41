"""Two-process integration and restart-persistence tests.

Spawns real uvicorn worker processes that share one SQLite file, mirroring
the Docker Compose deployment (two API replicas on a shared volume).
"""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(base_url: str, proc: subprocess.Popen) -> None:
    deadline = time.time() + 30
    last_error: Exception | None = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"uvicorn exited early: {proc.returncode}")
        try:
            response = httpx.get(f"{base_url}/health", timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"server at {base_url} never became healthy: {last_error}")


_SPAWNED: list[tuple[subprocess.Popen, str, str]] = []
_SPAWN_LOCK = threading.Lock()


@atexit.register
def _cleanup_all() -> None:
    with _SPAWN_LOCK:
        for proc, base_url, shutdown_path in _SPAWNED:
            try:
                httpx.get(f"{base_url}{shutdown_path}", timeout=2.0)
            except httpx.HTTPError:
                pass


@contextmanager
def api_server(db_path: str, port: int) -> Iterator[str]:
    shutdown_path = f"/__lab_test_shutdown_{os.getpid()}_{port}"
    env = {
        **os.environ,
        "LAB_DB_PATH": db_path,
        "LAB_TEST_SHUTDOWN_PATH": shutdown_path,
        "PYTHONPATH": REPO_ROOT,
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "test_shutdown_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    with _SPAWN_LOCK:
        _SPAWNED.append((proc, base_url, shutdown_path))
    try:
        _wait_for_health(base_url, proc)
        yield base_url
    finally:
        try:
            httpx.get(f"{base_url}{shutdown_path}", timeout=5.0)
        except httpx.HTTPError:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            # Signals are unavailable in restricted sandboxes; atexit cleanup
            # will retry the self-shutdown endpoint.
            pass


@pytest.fixture(scope="module")
def shared_db(tmp_path_factory) -> str:
    return str(tmp_path_factory.mktemp("data") / "lab_reports.db")


def test_two_processes_share_state_and_withdrawal_visits_both(shared_db):
    port_a, port_b = _free_port(), _free_port()
    with api_server(shared_db, port_a) as url_a, api_server(shared_db, port_b) as url_b:
        # Register via process A.
        for rid, value in [("R-1", "7.0"), ("R-2", "1.1")]:
            response = httpx.post(
                f"{url_a}/v1/results",
                json={"result_id": rid, "sample_code": "W-1", "item": "pH", "value": value},
                timeout=10,
            )
            assert response.status_code == 201, response.text

        # Process B immediately sees A's committed data (WAL readers).
        response = httpx.get(f"{url_b}/v1/results/R-1", timeout=10)
        assert response.status_code == 200
        assert response.json()["current_revision"] == 1

        # Preview on B, publish on A.
        preview = httpx.post(
            f"{url_b}/v1/report-preview",
            json={"sample_code": "W-1", "result_ids": ["R-1", "R-2"]},
            timeout=10,
        )
        assert preview.status_code == 200
        selections = [
            {"result_id": item["result_id"], "revision": item["current_revision"]}
            for item in preview.json()["items"]
        ]
        published = httpx.post(
            f"{url_a}/v1/reports",
            json={"sample_code": "W-1", "selections": selections},
            timeout=10,
        )
        assert published.status_code == 201, published.text
        report = published.json()
        etag = report["etag"]

        # Report is readable from both processes.
        for url in (url_a, url_b):
            got = httpx.get(f"{url}/v1/reports/{report['report_id']}", timeout=10)
            assert got.status_code == 200
            assert got.headers["etag"] == etag

        # Correct through B: withdrawal must be visible through A.
        corrected = httpx.post(
            f"{url_b}/v1/results/R-1/corrections",
            json={"new_value": "7.4", "reason": "双进程更正"},
            timeout=10,
        )
        assert corrected.status_code == 200

        conditional = httpx.get(
            f"{url_a}/v1/reports/{report['report_id']}",
            headers={"If-None-Match": etag},
            timeout=10,
        )
        assert conditional.status_code == 200  # not 304 — ETag changed
        body = conditional.json()
        assert body["status"] == "withdrawn"
        assert conditional.headers["etag"] != etag
        assert "双进程更正" in body["withdraw_reason"]

        # Stale publish attempted against either process is rejected.
        stale = httpx.post(
            f"{url_b}/v1/reports",
            json={
                "sample_code": "W-1",
                "selections": [
                    {"result_id": "R-1", "revision": 1},
                    {"result_id": "R-2", "revision": 1},
                ],
            },
            timeout=10,
        )
        assert stale.status_code == 409


def test_data_survives_process_restart(shared_db):
    port = _free_port()
    with api_server(shared_db, port) as url:
        before = httpx.get(f"{url}/v1/reports", timeout=10).json()
        assert before, "fixture should have created at least one report"

    # All processes are gone; start a fresh one against the same database.
    port2 = _free_port()
    with api_server(shared_db, port2) as url:
        after = httpx.get(f"{url}/v1/reports", timeout=10).json()
        assert len(after) == len(before)
        by_id = {report["report_id"]: report for report in after}
        for original in before:
            persisted = by_id[original["report_id"]]
            assert persisted["status"] == original["status"]
            assert persisted["etag"] == original["etag"]
            assert persisted["items"] == original["items"]

        # Revision history persisted too.
        result = httpx.get(f"{url}/v1/results/R-1", timeout=10).json()
        assert result["current_revision"] == 2
        assert [rev["value"] for rev in result["revisions"]] == ["7.0", "7.4"]
