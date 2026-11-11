"""Concurrency tests proving correct/publish/withdraw linearise.

Each worker opens its own SQLite connection (via the service layer), so these
tests exercise the real ``BEGIN IMMEDIATE`` lock contention — the same
mechanism that serialises the two API processes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest

from app import config, database, service


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_DB_PATH", str(tmp_path / "concurrency.db"))
    database.initialize()
    yield
    # config reads the env var lazily on every connect(); nothing to reset.
    assert config.database_path().name == "concurrency.db"


def _run_concurrently(targets: list[Callable[[], object]]) -> list[object]:
    results: list[object] = [None] * len(targets)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            results[index] = targets[index]()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(targets))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)
    return results


def test_concurrent_correction_vs_publish_never_leaves_stale_valid_report():
    service.create_result("R-1", "W-1", "pH", "7.0", "initial")
    outcomes: set[str] = set()

    for _ in range(30):
        # Fresh result each round so publish selections are comparable.
        rid = f"R-ITER-{_}"
        service.create_result(rid, "W-1", "pH", "7.0", "initial")

        def publish(rid: str = rid):
            try:
                report = service.publish_report(
                    "W-1", [service.PublishSelection.model_validate(
                        {"result_id": rid, "revision": 1}
                    )]
                )
                return ("published", report.report_id)
            except Exception as exc:  # noqa: BLE001
                status = getattr(exc, "status_code", None)
                return ("rejected", status)

        def correct(rid: str = rid):
            service.correct_result(rid, "7.4", "并发更正")
            return "corrected"

        publish_outcome, _ = _run_concurrently([publish, correct])
        outcomes.add(publish_outcome[0])

        # Invariant: no report citing revision 1 may remain valid afterwards.
        reports = service.list_reports()
        for report in reports:
            item = next((i for i in report.items if i.result_id == rid), None)
            if item is not None:
                assert report.status == "withdrawn"
                assert item.revision == 1  # immutable snapshot

    # Over 30 rounds both serial orderings should have occurred.
    assert outcomes == {"published", "rejected"}


def test_publish_rejected_when_correction_commits_first():
    service.create_result("R-1", "W-1", "pH", "7.0", "initial")

    barrier = threading.Barrier(2)

    def correct():
        barrier.wait()
        service.correct_result("R-1", "7.4", "先更正")

    def publish():
        barrier.wait()
        try:
            service.publish_report(
                "W-1",
                [service.PublishSelection.model_validate(
                    {"result_id": "R-1", "revision": 1}
                )],
            )
            return "published"
        except Exception as exc:  # noqa: BLE001
            return getattr(exc, "status_code", None)

    results = _run_concurrently([correct, publish])
    # Correction is a strict prefix here (same barrier, smaller write), but
    # regardless of ordering a stale-revision publish can never succeed; a
    # success means publish locked first and the report must now be withdrawn.
    if results[1] == "published":
        (only,) = [r for r in service.list_reports() if any(i.result_id == "R-1" for i in r.items)]
        assert only.status == "withdrawn"
    else:
        assert results[1] == 409


def test_concurrent_corrections_serialise_into_consecutive_revisions():
    service.create_result("R-1", "W-1", "pH", "7.0", "initial")
    n = 20

    def make_correction(i: int):
        def target():
            service.correct_result("R-1", str(float(i)), f"更正 {i}")
        return target

    _run_concurrently([make_correction(i) for i in range(n)])
    result = service.get_result("R-1")
    assert result.current_revision == n + 1
    assert [rev.revision for rev in result.revisions] == list(range(1, n + 2))
    values = {rev.value for rev in result.revisions[1:]}
    assert values == {str(float(i)) for i in range(n)}


def test_concurrent_publishes_are_independent_and_all_survive():
    for i in range(10):
        service.create_result(f"R-{i}", "W-1", "pH", "7.0", "initial")

    def make_publish(i: int):
        def target():
            service.publish_report(
                "W-1",
                [service.PublishSelection.model_validate(
                    {"result_id": f"R-{i}", "revision": 1}
                )],
            )
        return target

    _run_concurrently([make_publish(i) for i in range(10)])
    reports = service.list_reports()
    assert len(reports) == 10
    assert all(report.status == "valid" for report in reports)
    assert len({report.report_id for report in reports}) == 10
