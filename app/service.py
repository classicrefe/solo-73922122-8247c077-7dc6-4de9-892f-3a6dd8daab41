"""Business logic: results, revisions, report preview/publish/withdrawal.

All mutating functions take the write lock through ``write_transaction`` so
their effect is globally serial across the two API processes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from . import database
from .schemas import (
    PreviewItem,
    PreviewResponse,
    PublishResponse,
    PublishSelection,
    ReportItem,
    ReportResponse,
    ResultResponse,
    Revision,
)


def utc_now() -> str:
    """UTC second-precision timestamp with explicit ``Z`` suffix."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error(status: int, code: str, message: str, details: object | None = None) -> HTTPException:
    payload: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        payload["details"] = details
    return HTTPException(status_code=status, detail=payload)


# ---------------------------------------------------------------------------
# Row -> schema helpers
# ---------------------------------------------------------------------------


def _load_revisions(conn: sqlite3.Connection, result_id: str) -> list[Revision]:
    rows = conn.execute(
        "SELECT revision, value, reason, created_at FROM result_revisions "
        "WHERE result_id = ? ORDER BY revision",
        (result_id,),
    ).fetchall()
    return [
        Revision(
            revision=row["revision"],
            value=row["value"],
            reason=row["reason"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def _build_result_response(conn: sqlite3.Connection, row: sqlite3.Row) -> ResultResponse:
    return ResultResponse(
        result_id=row["result_id"],
        sample_code=row["sample_code"],
        item=row["item"],
        current_revision=row["current_revision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        revisions=_load_revisions(conn, row["result_id"]),
    )


def _get_result(conn: sqlite3.Connection, result_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM results WHERE result_id = ?", (result_id,)
    ).fetchone()
    if row is None:
        raise _error(404, "result_not_found", f"结果 ID {result_id} 不存在。")
    return row


# ---------------------------------------------------------------------------
# Result registration & correction
# ---------------------------------------------------------------------------


def create_result(
    result_id: str, sample_code: str, item: str, value: str, reason: str
) -> ResultResponse:
    now = utc_now()
    with database.write_transaction() as conn:
        existing = conn.execute(
            "SELECT 1 FROM results WHERE result_id = ?", (result_id,)
        ).fetchone()
        if existing is not None:
            raise _error(409, "result_already_exists", f"结果 ID {result_id} 已登记。")
        conn.execute(
            "INSERT INTO results (result_id, sample_code, item, current_revision, "
            "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
            (result_id, sample_code, item, now, now),
        )
        conn.execute(
            "INSERT INTO result_revisions (result_id, revision, value, reason, created_at) "
            "VALUES (?, 1, ?, ?, ?)",
            (result_id, value, reason, now),
        )
        row = _get_result(conn, result_id)
        return _build_result_response(conn, row)


def get_result(result_id: str) -> ResultResponse:
    with database.read_connection() as conn:
        row = _get_result(conn, result_id)
        return _build_result_response(conn, row)


def correct_result(result_id: str, new_value: str, reason: str) -> ResultResponse:
    """Append a revision and withdraw every valid report citing the result.

    The whole operation is one ``BEGIN IMMEDIATE`` transaction: any publish
    taking the lock either completes before the correction (and therefore
    gets withdrawn here) or starts after it (and sees the new current
    revision, so its stale selection is rejected).
    """

    now = utc_now()
    with database.write_transaction() as conn:
        row = _get_result(conn, result_id)
        old_revision = row["current_revision"]
        new_revision = old_revision + 1

        conn.execute(
            "INSERT INTO result_revisions (result_id, revision, value, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (result_id, new_revision, new_value, reason, now),
        )
        conn.execute(
            "UPDATE results SET current_revision = ?, updated_at = ? WHERE result_id = ?",
            (new_revision, now, result_id),
        )

        withdraw_reason = (
            f"来源结果 {result_id}（{row['sample_code']}/{row['item']}）由修订 "
            f"{old_revision} 更正为修订 {new_revision}；更正原因：{reason}"
        )
        referenced = conn.execute(
            "SELECT DISTINCT r.report_id FROM report_items ri "
            "JOIN reports r ON r.report_id = ri.report_id "
            "WHERE ri.result_id = ? AND r.status = 'valid'",
            (result_id,),
        ).fetchall()
        for ref in referenced:
            conn.execute(
                "UPDATE reports SET status = 'withdrawn', "
                "etag_version = etag_version + 1, withdrawn_at = ?, withdraw_reason = ? "
                "WHERE report_id = ? AND status = 'valid'",
                (now, withdraw_reason, ref["report_id"]),
            )

        updated = _get_result(conn, result_id)
        return _build_result_response(conn, updated)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def preview_report(sample_code: str, result_ids: list[str]) -> PreviewResponse:
    with database.read_connection() as conn:
        missing: list[str] = []
        mismatched: list[dict[str, str]] = []
        items: list[PreviewItem] = []

        for index, result_id in enumerate(result_ids, start=1):
            row = conn.execute(
                "SELECT * FROM results WHERE result_id = ?", (result_id,)
            ).fetchone()
            if row is None:
                missing.append(result_id)
                continue
            if row["sample_code"] != sample_code:
                mismatched.append(
                    {
                        "result_id": result_id,
                        "expected_sample_code": sample_code,
                        "actual_sample_code": row["sample_code"],
                    }
                )
                continue
            current = conn.execute(
                "SELECT value, created_at FROM result_revisions "
                "WHERE result_id = ? AND revision = ?",
                (result_id, row["current_revision"]),
            ).fetchone()
            items.append(
                PreviewItem(
                    index=index,
                    result_id=result_id,
                    sample_code=row["sample_code"],
                    item=row["item"],
                    value=current["value"],
                    current_revision=row["current_revision"],
                    revision_created_at=current["created_at"],
                    all_revisions=_load_revisions(conn, result_id),
                )
            )

        if missing:
            raise _error(
                404,
                "result_not_found",
                "部分结果 ID 不存在。",
                {"missing_result_ids": missing},
            )
        if mismatched:
            raise _error(
                409,
                "sample_mismatch",
                "部分结果不属于请求的样品码。",
                {"mismatches": mismatched},
            )

        return PreviewResponse(sample_code=sample_code, item_count=len(items), items=items)


# ---------------------------------------------------------------------------
# Publish / query
# ---------------------------------------------------------------------------


def _report_items_from_db(conn: sqlite3.Connection, report_id: str) -> list[ReportItem]:
    rows = conn.execute(
        "SELECT item_index, result_id, sample_code, item, value, revision, "
        "revision_created_at FROM report_items WHERE report_id = ? ORDER BY item_index",
        (report_id,),
    ).fetchall()
    return [
        ReportItem(
            index=row["item_index"],
            result_id=row["result_id"],
            sample_code=row["sample_code"],
            item=row["item"],
            value=row["value"],
            revision=row["revision"],
            revision_created_at=row["revision_created_at"],
        )
        for row in rows
    ]


def compute_etag(
    report_id: str, status: str, etag_version: int, items: list[ReportItem]
) -> str:
    """Strong ETag over identity, lifecycle state and immutable content.

    ``etag_version`` is bumped on every withdrawal, so withdrawing a report
    always changes its ETag even though the published body stays byte
    identical — a conditional GET can therefore never answer 304 after a
    withdrawal.
    """

    canonical = json.dumps(
        {
            "report_id": report_id,
            "status": status,
            "etag_version": etag_version,
            "items": [
                [
                    item.index,
                    item.result_id,
                    item.sample_code,
                    item.item,
                    item.value,
                    item.revision,
                    item.revision_created_at,
                ]
                for item in items
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f'"{digest}"'


def _build_report_response(conn: sqlite3.Connection, row: sqlite3.Row) -> ReportResponse:
    items = _report_items_from_db(conn, row["report_id"])
    etag = compute_etag(
        row["report_id"], row["status"], row["etag_version"], items
    )
    return ReportResponse(
        report_id=row["report_id"],
        sample_code=row["sample_code"],
        status=row["status"],
        etag=etag,
        created_at=row["created_at"],
        published_at=row["published_at"],
        withdrawn_at=row["withdrawn_at"],
        withdraw_reason=row["withdraw_reason"],
        item_count=len(items),
        items=items,
    )


def publish_report(sample_code: str, selections: list[PublishSelection]) -> PublishResponse:
    now = utc_now()
    report_id = uuid.uuid4().hex
    with database.write_transaction() as conn:
        missing: list[str] = []
        mismatched: list[dict[str, str]] = []
        stale: list[dict[str, object]] = []
        snapshots: list[tuple[int, sqlite3.Row, sqlite3.Row]] = []

        for index, selection in enumerate(selections, start=1):
            row = conn.execute(
                "SELECT * FROM results WHERE result_id = ?",
                (selection.result_id,),
            ).fetchone()
            if row is None:
                missing.append(selection.result_id)
                continue
            if row["sample_code"] != sample_code:
                mismatched.append(
                    {
                        "result_id": selection.result_id,
                        "expected_sample_code": sample_code,
                        "actual_sample_code": row["sample_code"],
                    }
                )
                continue
            if row["current_revision"] != selection.revision:
                stale.append(
                    {
                        "result_id": selection.result_id,
                        "submitted_revision": selection.revision,
                        "current_revision": row["current_revision"],
                    }
                )
                continue
            rev_row = conn.execute(
                "SELECT created_at, value FROM result_revisions "
                "WHERE result_id = ? AND revision = ?",
                (selection.result_id, selection.revision),
            ).fetchone()
            snapshots.append((index, row, rev_row))

        if missing:
            raise _error(
                404,
                "result_not_found",
                "部分结果 ID 不存在。",
                {"missing_result_ids": missing},
            )
        if mismatched:
            raise _error(
                409,
                "sample_mismatch",
                "部分结果不属于请求的样品码。",
                {"mismatches": mismatched},
            )
        if stale:
            raise _error(
                409,
                "stale_revision",
                "携带的修订已过期：至少一个来源结果已被更正，整份报告拒绝发布。",
                {"stale_selections": stale},
            )

        conn.execute(
            "INSERT INTO reports (report_id, sample_code, status, etag_version, "
            "created_at, published_at, withdrawn_at, withdraw_reason) "
            "VALUES (?, ?, 'valid', 1, ?, ?, NULL, NULL)",
            (report_id, sample_code, now, now),
        )
        for index, result_row, rev_row in snapshots:
            conn.execute(
                "INSERT INTO report_items (report_id, result_id, item_index, "
                "sample_code, item, value, revision, revision_created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    result_row["result_id"],
                    index,
                    result_row["sample_code"],
                    result_row["item"],
                    rev_row["value"],
                    result_row["current_revision"],
                    rev_row["created_at"],
                ),
            )

        report_row = conn.execute(
            "SELECT * FROM reports WHERE report_id = ?", (report_id,)
        ).fetchone()
        response = _build_report_response(conn, report_row)

    return PublishResponse(
        report_id=response.report_id,
        status="valid",
        etag=response.etag,
        sample_code=response.sample_code,
        item_count=response.item_count,
        items=response.items,
        published_at=response.published_at,
    )


def get_report(report_id: str) -> tuple[ReportResponse, str]:
    """Return the report representation together with its current ETag."""

    with database.read_connection() as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE report_id = ?", (report_id,)
        ).fetchone()
        if row is None:
            raise _error(404, "report_not_found", f"报告 {report_id} 不存在。")
        response = _build_report_response(conn, row)
    return response, response.etag


def list_reports(status: str | None = None) -> list[ReportResponse]:
    with database.read_connection() as conn:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM reports WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reports ORDER BY created_at"
            ).fetchall()
        return [_build_report_response(conn, row) for row in rows]
