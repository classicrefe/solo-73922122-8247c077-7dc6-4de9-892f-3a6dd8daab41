"""Functional tests: results, revisions, preview, publish, withdrawal, ETag."""

from __future__ import annotations

from tests.conftest import make_result


# ---------------------------------------------------------------------------
# Results & revisions
# ---------------------------------------------------------------------------


def test_create_result_starts_at_revision_one(client):
    body = make_result(client, "R-1")
    assert body["current_revision"] == 1
    assert body["revisions"] == [
        {"revision": 1, "value": "7.0", "reason": "initial", "created_at": body["created_at"]}
    ]


def test_duplicate_result_id_rejected(client):
    make_result(client, "R-1")
    response = client.post(
        "/v1/results",
        json={"result_id": "R-1", "sample_code": "W-1", "item": "pH", "value": "8.0"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "result_already_exists"


def test_correction_appends_revision_and_keeps_history(client):
    make_result(client, "R-1", value="7.0")
    response = client.post(
        "/v1/results/R-1/corrections",
        json={"new_value": "7.4", "reason": "仪器复核后更正"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["current_revision"] == 2
    assert [rev["value"] for rev in body["revisions"]] == ["7.0", "7.4"]
    assert [rev["revision"] for rev in body["revisions"]] == [1, 2]
    assert body["revisions"][1]["reason"] == "仪器复核后更正"

    # A second correction -> revision 3, full history still present.
    response = client.post(
        "/v1/results/R-1/corrections",
        json={"new_value": "7.0", "reason": "值改回原值"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["current_revision"] == 3
    assert [rev["value"] for rev in body["revisions"]] == ["7.0", "7.4", "7.0"]


def test_correct_unknown_result_404(client):
    response = client.post(
        "/v1/results/NOPE/corrections", json={"new_value": "1", "reason": "x"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def test_preview_returns_full_content_and_all_revisions(client):
    make_result(client, "R-1", item="pH", value="7.0")
    make_result(client, "R-2", item="浊度", value="1.1")
    client.post(
        "/v1/results/R-1/corrections", json={"new_value": "7.4", "reason": "复核"}
    )

    response = client.post(
        "/v1/report-preview",
        json={"sample_code": "W-1", "result_ids": ["R-1", "R-2"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["item_count"] == 2
    first, second = body["items"]
    assert first["result_id"] == "R-1"
    assert first["value"] == "7.4"
    assert first["current_revision"] == 2
    assert len(first["all_revisions"]) == 2
    assert second["current_revision"] == 1
    assert len(second["all_revisions"]) == 1


def test_preview_rejects_unknown_and_foreign_sample(client):
    make_result(client, "R-1", sample="W-1")
    make_result(client, "R-9", sample="W-OTHER")

    response = client.post(
        "/v1/report-preview",
        json={"sample_code": "W-1", "result_ids": ["R-1", "MISSING"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["details"]["missing_result_ids"] == ["MISSING"]

    response = client.post(
        "/v1/report-preview",
        json={"sample_code": "W-1", "result_ids": ["R-9"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "sample_mismatch"


def test_preview_limits_and_duplicates(client):
    too_many = [f"R-{i}" for i in range(31)]
    response = client.post(
        "/v1/report-preview",
        json={"sample_code": "W-1", "result_ids": too_many},
    )
    assert response.status_code == 422

    response = client.post(
        "/v1/report-preview",
        json={"sample_code": "W-1", "result_ids": ["R-1", "R-1"]},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def _publish(client, result_ids, sample="W-1", revisions=None):
    selections = [
        {"result_id": rid, "revision": (revisions or {}).get(rid, 1)}
        for rid in result_ids
    ]
    return client.post(
        "/v1/reports", json={"sample_code": sample, "selections": selections}
    )


def test_publish_snapshots_content_and_revisions(client):
    make_result(client, "R-1", item="pH", value="7.0")
    make_result(client, "R-2", item="浊度", value="1.1")
    response = _publish(client, ["R-1", "R-2"])
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "valid"
    assert body["item_count"] == 2
    assert body["etag"].startswith('"')
    assert [item["result_id"] for item in body["items"]] == ["R-1", "R-2"]
    assert [item["revision"] for item in body["items"]] == [1, 1]


def test_publish_rejects_stale_revision_whole_report(client):
    make_result(client, "R-1", value="7.0")
    make_result(client, "R-2", value="1.1")
    # Correct R-1 before publishing with revision 1.
    client.post(
        "/v1/results/R-1/corrections", json={"new_value": "7.4", "reason": "复核"}
    )
    response = _publish(client, ["R-1", "R-2"])
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "stale_revision"
    assert detail["details"]["stale_selections"] == [
        {"result_id": "R-1", "submitted_revision": 1, "current_revision": 2}
    ]
    # Nothing was published.
    assert client.get("/v1/reports").json() == []


def test_publish_with_current_revision_succeeds(client):
    make_result(client, "R-1", value="7.0")
    client.post(
        "/v1/results/R-1/corrections", json={"new_value": "7.4", "reason": "复核"}
    )
    response = _publish(client, ["R-1"], revisions={"R-1": 2})
    assert response.status_code == 201
    assert response.json()["items"][0]["value"] == "7.4"


def test_unrelated_correction_does_not_block_publish(client):
    make_result(client, "R-1")
    make_result(client, "R-OTHER", sample="W-2")
    client.post(
        "/v1/results/R-OTHER/corrections",
        json={"new_value": "9.9", "reason": "另一水样更正"},
    )
    response = _publish(client, ["R-1"])
    assert response.status_code == 201


# ---------------------------------------------------------------------------
# Immutability & withdrawal
# ---------------------------------------------------------------------------


def test_published_content_is_immutable_after_source_correction(client):
    make_result(client, "R-1", value="7.0")
    report = _publish(client, ["R-1"]).json()

    client.post(
        "/v1/results/R-1/corrections", json={"new_value": "9.9", "reason": "复核"}
    )
    response = client.get(f"/v1/reports/{report['report_id']}")
    body = response.json()
    assert body["status"] == "withdrawn"
    # Snapshot keeps the original value and revision untouched.
    assert body["items"][0]["value"] == "7.0"
    assert body["items"][0]["revision"] == 1
    assert body["withdrawn_at"] is not None
    assert "R-1" in body["withdraw_reason"]


def test_withdrawal_cascades_to_all_referencing_reports(client):
    make_result(client, "R-1")
    make_result(client, "R-2")
    report_a = _publish(client, ["R-1"]).json()["report_id"]
    report_b = _publish(client, ["R-1", "R-2"]).json()["report_id"]
    report_c = _publish(client, ["R-2"]).json()["report_id"]

    client.post(
        "/v1/results/R-1/corrections", json={"new_value": "8.0", "reason": "复核"}
    )
    statuses = {r["report_id"]: r["status"] for r in client.get("/v1/reports").json()}
    assert statuses[report_a] == "withdrawn"
    assert statuses[report_b] == "withdrawn"
    assert statuses[report_c] == "valid"


def test_reverting_value_does_not_restore_report(client):
    make_result(client, "R-1", value="7.0")
    report = _publish(client, ["R-1"]).json()["report_id"]
    client.post(
        "/v1/results/R-1/corrections", json={"new_value": "7.4", "reason": "复核"}
    )
    client.post(
        "/v1/results/R-1/corrections", json={"new_value": "7.0", "reason": "改回原值"}
    )
    body = client.get(f"/v1/reports/{report}").json()
    assert body["status"] == "withdrawn"
    assert client.get("/v1/results/R-1").json()["current_revision"] == 3


def test_withdrawn_report_cannot_be_withdrawn_twice_and_keeps_first_reason(client):
    make_result(client, "R-1", value="7.0")
    report = _publish(client, ["R-1"]).json()
    first = client.post(
        "/v1/results/R-1/corrections", json={"new_value": "7.4", "reason": "第一次更正"}
    ).json()
    client.post(
        "/v1/results/R-1/corrections", json={"new_value": "7.5", "reason": "第二次更正"}
    )
    body = client.get(f"/v1/reports/{report['report_id']}").json()
    assert body["status"] == "withdrawn"
    assert "第一次更正" in body["withdraw_reason"]
    assert body["withdrawn_at"] == first["updated_at"]


# ---------------------------------------------------------------------------
# ETag / conditional requests
# ---------------------------------------------------------------------------


def test_etag_304_and_200_cycle(client):
    make_result(client, "R-1")
    report = _publish(client, ["R-1"]).json()
    report_id, etag = report["report_id"], report["etag"]

    # ETag header present on GET, matches body field.
    response = client.get(f"/v1/reports/{report_id}")
    assert response.status_code == 200
    assert response.headers["etag"] == etag

    # Conditional GET with the current ETag -> 304, no body.
    response = client.get(f"/v1/reports/{report_id}", headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.headers["etag"] == etag
    assert response.content == b""

    # Wildcard and list forms also match.
    assert client.get(
        f"/v1/reports/{report_id}", headers={"If-None-Match": "*"}
    ).status_code == 304
    assert client.get(
        f"/v1/reports/{report_id}",
        headers={"If-None-Match": f'"stale-x", {etag}'},
    ).status_code == 304

    # A non-matching ETag yields a normal 200.
    assert client.get(
        f"/v1/reports/{report_id}", headers={"If-None-Match": '"other"'}
    ).status_code == 200


def test_withdrawal_changes_etag_and_never_returns_304(client):
    make_result(client, "R-1")
    report = _publish(client, ["R-1"]).json()
    report_id, old_etag = report["report_id"], report["etag"]

    client.post(
        "/v1/results/R-1/corrections", json={"new_value": "7.4", "reason": "复核"}
    )

    # Old ETag must NOT produce 304 after withdrawal.
    response = client.get(f"/v1/reports/{report_id}", headers={"If-None-Match": old_etag})
    assert response.status_code == 200
    new_etag = response.headers["etag"]
    assert new_etag != old_etag
    assert response.json()["status"] == "withdrawn"

    # The new ETag does produce 304, and stays stable across reads.
    response = client.get(f"/v1/reports/{report_id}", headers={"If-None-Match": new_etag})
    assert response.status_code == 304
    again = client.get(f"/v1/reports/{report_id}").headers["etag"]
    assert again == new_etag


def test_etag_differs_between_distinct_reports(client):
    make_result(client, "R-1", value="7.0")
    make_result(client, "R-2", value="1.1")
    etag1 = _publish(client, ["R-1"]).json()["etag"]
    etag2 = _publish(client, ["R-2"]).json()["etag"]
    assert etag1 != etag2


def test_republish_with_new_revision_yields_independent_valid_report(client):
    make_result(client, "R-1", value="7.0")
    first = _publish(client, ["R-1"]).json()
    client.post(
        "/v1/results/R-1/corrections", json={"new_value": "7.4", "reason": "复核"}
    )

    # Old report withdrawn; a fresh report citing revision 2 is valid.
    second = _publish(client, ["R-1"], revisions={"R-1": 2})
    assert second.status_code == 201
    second_body = second.json()
    assert second_body["status"] == "valid"
    assert second_body["items"][0]["revision"] == 2
    assert second_body["items"][0]["value"] == "7.4"
    assert second_body["report_id"] != first["report_id"]

    statuses = {r["report_id"]: r["status"] for r in client.get("/v1/reports").json()}
    assert statuses[first["report_id"]] == "withdrawn"
    assert statuses[second_body["report_id"]] == "valid"


def test_unknown_report_404_and_list_filter(client):
    assert client.get("/v1/reports/nope").status_code == 404
    make_result(client, "R-1")
    _publish(client, ["R-1"])
    client.post(
        "/v1/results/R-1/corrections", json={"new_value": "7.4", "reason": "复核"}
    )
    valid = client.get("/v1/reports", params={"status": "valid"}).json()
    withdrawn = client.get("/v1/reports", params={"status": "withdrawn"}).json()
    assert valid == []
    assert len(withdrawn) == 1 and withdrawn[0]["status"] == "withdrawn"
