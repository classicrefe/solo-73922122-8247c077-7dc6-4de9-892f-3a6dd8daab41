"""Shared pytest fixtures: isolated database + TestClient."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    db_path = tmp_path / "test_lab_reports.db"
    monkeypatch.setenv("LAB_DB_PATH", str(db_path))
    # Import after the env var is set so config picks up the temp database.
    from app import database
    from app.main import app

    database.initialize()
    with TestClient(app) as test_client:
        yield test_client


def make_result(client: TestClient, result_id: str, sample: str = "W-1", item: str = "pH", value: str = "7.0") -> dict:
    response = client.post(
        "/v1/results",
        json={
            "result_id": result_id,
            "sample_code": sample,
            "item": item,
            "value": value,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
