"""店舗ごとのデータ充足度API。"""
from __future__ import annotations

import sqlite3
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from api import deps
from api.main import app
from api.routers import hall as hall_router


client = TestClient(app)


@pytest.fixture()
def coverage_database(tmp_path, monkeypatch):
    database = tmp_path / "hall_reports.db"
    monkeypatch.setattr(deps, "HALL_REPORTS_DB", database)
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT,
            avg_diff_coins REAL, win_rate_pct REAL, source_url TEXT
        );
        CREATE TABLE hall_day_seat (
            hall_name TEXT, report_date TEXT, machine_name TEXT,
            seat_number INTEGER, diff_coins REAL, games INTEGER, source_url TEXT
        );
        CREATE TABLE hall_machine_snapshot (
            hall_name TEXT, snapshot_date TEXT, machine_name TEXT,
            machine_id TEXT, source_url TEXT
        );
        CREATE TABLE hall_event (
            hall_name TEXT, event_date TEXT, event_title TEXT
        );
        """
    )
    conn.close()
    return database


def test_coverage_reports_each_data_layer(coverage_database):
    conn = sqlite3.connect(coverage_database)
    for offset in range(34, -1, -1):
        report_date = (date.today() - timedelta(days=offset)).isoformat()
        conn.execute(
            "INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?)",
            ("テスト店", report_date, "スマスロ北斗の拳", 300, 51, "https://example.com"),
        )
        conn.execute(
            "INSERT INTO hall_day_seat VALUES (?,?,?,?,?,?,?)",
            ("テスト店", report_date, "スマスロ北斗の拳", 501, 300, 7000, "https://example.com"),
        )
    conn.execute(
        "INSERT INTO hall_machine_snapshot VALUES (?,?,?,?,?)",
        ("テスト店", date.today().isoformat(), "スマスロ北斗の拳", "1", "https://example.com"),
    )
    conn.execute(
        "INSERT INTO hall_event VALUES (?,?,?)",
        ("テスト店", date.today().isoformat(), "新台入替"),
    )
    conn.commit()
    conn.close()

    response = client.get("/api/hall/data_coverage", params={"hall_name": "テスト店"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["performance"]["performance_days"] == 35
    assert data["performance"]["machine_records"] == 35
    assert data["performance"]["seat_records"] == 35
    assert data["installation"]["records"] == 1
    assert data["events"]["records"] == 1
    assert data["readiness"]["trend_label"] == "参考"
    assert data["readiness"]["trend_ready"] is True
    assert data["readiness"]["seat_ready"] is True
    assert data["intraday"]["records"] == 0
    assert data["intraday"]["days"] == 0
    assert data["intraday"]["ready"] is False
    assert "30件" in data["intraday"]["note"]


def test_coverage_explains_empty_store(coverage_database):
    response = client.get("/api/hall/data_coverage", params={"hall_name": "未収集店"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["performance"]["total_records"] == 0
    assert data["readiness"]["trend_label"] == "不足"
    assert data["readiness"]["trend_ready"] is False
    assert any("未収集" in reason for reason in data["readiness"]["reasons"])


def test_coverage_merges_confirmed_hall_name_alias(coverage_database):
    conn = sqlite3.connect(coverage_database)
    conn.execute(
        "INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?)",
        ("キコーナ四条畷店", date.today().isoformat(), "L北斗", 100, 51, "https://example.com"),
    )
    conn.commit()
    conn.close()
    response = client.get(
        "/api/hall/data_coverage", params={"hall_name": "キコーナ四條畷店"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["performance"]["machine_records"] == 1


def test_region_coverage_distinguishes_public_not_disclosed_from_network_error(
    coverage_database,
):
    conn = sqlite3.connect(coverage_database)
    conn.execute(
        """CREATE TABLE collection_run_log (
               id INTEGER PRIMARY KEY, source TEXT, started_at TEXT, finished_at TEXT,
               status TEXT, records INTEGER, details_json TEXT, error TEXT
           )"""
    )
    details = [{
        "hall_name": "キコーナ四条畷店",
        "status": "no_public_data",
        "rows": 0,
    }]
    conn.execute(
        "INSERT INTO collection_run_log VALUES (1,?,?,?,?,?,?,?)",
        (
            "public_machine_daily", "2026-09-02T08:00:00", "2026-09-02T08:00:01",
            "partial", 0, json.dumps(details, ensure_ascii=False), "",
        ),
    )
    conn.commit()
    conn.close()

    response = client.get("/api/hall/region_data_coverage", params={"region": "shijonawate"})
    assert response.status_code == 200, response.text
    data = response.json()
    item = next(row for row in data["halls"] if row["hall_name"] == "キコーナ四條畷店")
    assert item["collection"]["state"] == "not_disclosed"
    assert item["collection"]["label"] == "公開元が差枚未掲載"
    assert "CSV" in item["next_action"]
    assert data["summary"]["public_not_disclosed_halls"] == 1


def test_target_search_region_board_keeps_all_local_halls_visible(coverage_database):
    board = hall_router._build_region_decision_board("shijonawate", [], [])
    assert board["summary"]["hall_count"] == 8
    assert len(board["halls"]) == 8
    assert all(item["next_action"] for item in board["halls"])

