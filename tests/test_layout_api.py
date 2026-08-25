"""店舗傾向・店内マップAPIの回帰テスト。"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from api import deps
from api.main import app
from api.routers import layout


client = TestClient(app)


@pytest.fixture()
def layout_database(tmp_path, monkeypatch):
    database = tmp_path / "hall_reports.db"
    monkeypatch.setattr(layout, "HALL_REPORTS_DB", database)
    monkeypatch.setattr(deps, "HALL_REPORTS_DB", database)
    conn = layout.init_layout_db()
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
        """
    )
    conn.close()
    return database


def test_trend_profile_uses_only_dates_before_visit(layout_database):
    visit = date.today() + timedelta(days=1)
    conn = sqlite3.connect(layout_database)
    rows = []
    for offset in range(35, 0, -1):
        report_date = visit - timedelta(days=offset)
        value = 900 if report_date.weekday() == 0 else -100
        rows.append(("テスト店", report_date.isoformat(), "スマスロ北斗の拳", value, 55, "https://example.com/source"))
    # 未来の極端な値を入れても予測には混ざらない。
    rows.append(("テスト店", (visit + timedelta(days=1)).isoformat(), "スマスロ北斗の拳", 99999, 100, "https://example.com/future"))
    conn.executemany("INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    response = client.get("/api/hall/trend_profile", params={"hall_name": "テスト店", "visit_date": visit.isoformat(), "days": 90})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "分析済み"
    assert data["sample_days"] == 35
    assert data["latest_date"] < visit.isoformat()
    assert "https://example.com/future" not in data["source_urls"]
    assert data["weekday_profile"]
    assert data["machine_profile"][0]["machine_name"] == "スマスロ北斗の拳"


def test_layout_save_and_seat_heat(layout_database):
    visit = date.today() + timedelta(days=1)
    valid_from = (visit - timedelta(days=30)).isoformat()
    payload = {
        "hall_name": "テスト店",
        "floor_name": "1階",
        "valid_from": valid_from,
        "source_url": "https://example.com/map",
        "source_label": "公式店内案内",
        "source_kind": "official",
        "verification_status": "確認済み",
        "notes": "現地確認済み",
        "seats": [
            {"seat_number": 501, "machine_name": "スマスロ北斗の拳", "x": 50, "y": 60},
            {"seat_number": 502, "machine_name": "スマスロ北斗の拳", "x": 115, "y": 60},
        ],
    }
    saved = client.post("/api/layouts", json=payload)
    assert saved.status_code == 200, saved.text
    assert len(saved.json()["seats"]) == 2

    conn = sqlite3.connect(layout_database)
    history = []
    for offset in range(12, 0, -1):
        report_date = visit - timedelta(days=offset)
        history.extend([
            ("テスト店", report_date.isoformat(), "スマスロ北斗の拳", 501, 800, 7000, "https://example.com/data"),
            ("テスト店", report_date.isoformat(), "スマスロ北斗の拳", 502, -500, 6500, "https://example.com/data"),
        ])
    conn.executemany("INSERT INTO hall_day_seat VALUES (?,?,?,?,?,?,?)", history)
    conn.commit()
    conn.close()

    response = client.get("/api/layouts/seat_heat", params={"hall_name": "テスト店", "visit_date": visit.isoformat(), "days": 30})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "分析済み"
    assert data["layout"]["verification_status"] == "確認済み"
    seats = {seat["seat_number"]: seat for seat in data["seats"]}
    assert seats[501]["score"] > seats[502]["score"]
    assert seats[501]["sample_days"] == 12
    assert data["data_coverage"]["history_rows"] == 24


def test_empty_database_returns_explanatory_status(tmp_path, monkeypatch):
    database = tmp_path / "empty.db"
    monkeypatch.setattr(layout, "HALL_REPORTS_DB", database)
    monkeypatch.setattr(deps, "HALL_REPORTS_DB", database)
    layout.init_layout_db().close()
    visit = (date.today() + timedelta(days=1)).isoformat()

    trend = client.get("/api/hall/trend_profile", params={"hall_name": "未収集店", "visit_date": visit})
    heat = client.get("/api/layouts/seat_heat", params={"hall_name": "未収集店", "visit_date": visit})
    assert trend.status_code == 200
    assert trend.json()["status"] == "データなし"
    assert heat.status_code == 200
    assert heat.json()["status"] == "レイアウト未登録"


def test_seat_heat_exposes_latest_public_floor_map(layout_database):
    conn = sqlite3.connect(layout_database)
    conn.execute(
        """CREATE TABLE hall_floor_map_snapshot (
            hall_name TEXT,snapshot_date TEXT,floor_index INTEGER,image_url TEXT,page_url TEXT
        )"""
    )
    conn.executemany(
        "INSERT INTO hall_floor_map_snapshot VALUES (?,?,?,?,?)",
        [
            ("テスト店", "2026-08-24", 1, "https://cdn.example/map-old.jpg", "https://example.com/shop"),
            ("テスト店", "2026-08-25", 1, "https://cdn.example/map-new.jpg", "https://example.com/shop"),
        ],
    )
    conn.commit()
    conn.close()
    response = client.get(
        "/api/layouts/seat_heat",
        params={"hall_name": "テスト店", "visit_date": "2026-08-26", "days": 30},
    )
    assert response.status_code == 200
    assert response.json()["floor_map_sources"][0]["image_url"].endswith("map-new.jpg")


def test_duplicate_seat_numbers_are_rejected(layout_database):
    payload = {
        "hall_name": "テスト店",
        "valid_from": date.today().isoformat(),
        "seats": [
            {"seat_number": 501, "x": 10, "y": 10},
            {"seat_number": 501, "x": 80, "y": 10},
        ],
    }
    response = client.post("/api/layouts", json=payload)
    assert response.status_code == 422


def test_manual_seat_result_is_resolved_from_layout_and_updates_heatmap(layout_database):
    today = date.today()
    visit = today + timedelta(days=1)
    layout_payload = {
        "hall_name": "テスト店",
        "valid_from": (today - timedelta(days=10)).isoformat(),
        "seats": [
            {"seat_number": 501, "machine_name": "スマスロ北斗の拳", "x": 50, "y": 60},
        ],
    }
    assert client.post("/api/layouts", json=layout_payload).status_code == 200

    payload = {
        "hall_name": "テスト店",
        "report_date": today.isoformat(),
        "source_label": "現地確認",
        "rows": [{"seat_number": 501, "machine_name": "", "diff_coins": 1800, "games": 7200}],
    }
    saved = client.post("/api/layouts/seat_results", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["inserted"] == 1

    listed = client.get(
        "/api/layouts/seat_results",
        params={"hall_name": "テスト店", "report_date": today.isoformat()},
    ).json()
    assert listed["count"] == 1
    assert listed["rows"][0]["machine_name"] == "スマスロ北斗の拳"

    heat = client.get(
        "/api/layouts/seat_heat",
        params={"hall_name": "テスト店", "visit_date": visit.isoformat(), "days": 30},
    ).json()
    assert heat["status"] == "分析済み"
    assert heat["seats"][0]["estimate"] == 1800

    payload["rows"][0]["diff_coins"] = 900
    updated = client.post("/api/layouts/seat_results", json=payload)
    assert updated.status_code == 200
    assert updated.json()["updated"] == 1


def test_manual_import_does_not_overwrite_public_seat_result(layout_database):
    report_date = date.today().isoformat()
    conn = sqlite3.connect(layout_database)
    conn.execute("ALTER TABLE hall_day_seat ADD COLUMN source TEXT DEFAULT 'unknown'")
    conn.execute(
        "INSERT INTO hall_day_seat VALUES (?,?,?,?,?,?,?,?)",
        ("テスト店", report_date, "スマスロ北斗の拳", 501, 2200, 7000, "https://example.com", "anaslo"),
    )
    conn.commit()
    conn.close()

    response = client.post(
        "/api/layouts/seat_results",
        json={
            "hall_name": "テスト店",
            "report_date": report_date,
            "source_label": "現地入力",
            "rows": [{"seat_number": 501, "machine_name": "スマスロ北斗の拳", "diff_coins": -500, "games": 1000}],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["skipped"] == 1
    conn = sqlite3.connect(layout_database)
    value = conn.execute("SELECT diff_coins FROM hall_day_seat WHERE seat_number=501").fetchone()[0]
    conn.close()
    assert value == 2200
