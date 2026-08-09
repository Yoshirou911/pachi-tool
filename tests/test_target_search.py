import sqlite3

import pytest
from fastapi import HTTPException

from api.routers import hall as hall_router
from api.routers import map as map_router


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_target_search_ranks_only_halls_with_enough_evidence(tmp_path, monkeypatch):
    db_path = tmp_path / "hall.db"
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scrape_hall_config (hall_name TEXT PRIMARY KEY, enabled INTEGER);
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT,
            avg_diff_coins INTEGER, win_rate_pct REAL, unit_count INTEGER,
            source_url TEXT
        );
        INSERT INTO scrape_hall_config VALUES ('十分店', 1), ('不足店', 1);
        """
    )
    rows = [
        ("十分店", "2026-07-27", "L北斗", 500, 60, 10, "https://source/1"),
        ("十分店", "2026-07-28", "L北斗", 300, 55, 10, "https://source/2"),
        ("十分店", "2026-08-03", "L北斗", 700, 65, 10, "https://source/3"),
        ("十分店", "2026-08-04", "L北斗", -100, 45, 10, "https://source/4"),
        ("十分店", "2026-08-04", "L東京喰種", 900, 70, 8, "https://source/4"),
        ("不足店", "2026-08-03", "L北斗", 1000, 80, 5, "https://source/5"),
        ("不足店", "2026-08-04", "L北斗", 1000, 80, 5, "https://source/6"),
    ]
    conn.executemany("INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    monkeypatch.setattr(hall_router, "_get_reports_conn", lambda: _connect(db_path))
    result = hall_router.get_target_search("2026-08-10", days=120, limit=8)

    assert result["weekday"] == "月"
    assert [hall["hall_name"] for hall in result["halls"]] == ["十分店"]
    assert result["halls"][0]["sample_days"] == 4
    assert result["halls"][0]["target_machines"][0]["machine_name"] == "L北斗"
    assert result["insufficient_halls"][0]["hall_name"] == "不足店"

    next_day = hall_router.get_target_search("2026-08-11", days=120, limit=8)
    assert next_day["weekday"] == "火"
    assert next_day["halls"][0]["avg_diff"] != result["halls"][0]["avg_diff"]


def test_target_search_rejects_invalid_date():
    with pytest.raises(HTTPException) as exc:
        hall_router.get_target_search("2026/08/10")
    assert exc.value.status_code == 400


def test_target_heat_map_returns_date_score_and_long_term_trends(tmp_path, monkeypatch):
    db_path = tmp_path / "heat.db"
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scrape_hall_config (hall_name TEXT PRIMARY KEY, enabled INTEGER);
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT,
            avg_diff_coins INTEGER, win_rate_pct REAL, unit_count INTEGER,
            source_url TEXT
        );
        INSERT INTO scrape_hall_config VALUES ('ニコニコ住道店', 1);
        """
    )
    rows = [
        ("ニコニコ住道店", "2026-07-20", "L北斗", 200, 55, 10, "https://source/1"),
        ("ニコニコ住道店", "2026-07-27", "L北斗", 600, 65, 10, "https://source/2"),
        ("ニコニコ住道店", "2026-08-03", "L北斗", 800, 70, 10, "https://source/3"),
        ("ニコニコ住道店", "2026-08-04", "L東京喰種", -100, 45, 8, "https://source/4"),
    ]
    conn.executemany("INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    monkeypatch.setattr(hall_router, "_get_reports_conn", lambda: _connect(db_path))
    monkeypatch.setattr(map_router, "_get_reports_conn", lambda: _connect(db_path))
    result = map_router.get_target_heat_map("2026-08-10", days=120, long_days=365)

    assert result["visit_date"] == "2026-08-10"
    assert result["halls"][0]["hall_name"] == "ニコニコ住道店"
    assert result["halls"][0]["score"] > 0
    assert result["halls"][0]["monthly_trend"]
    assert result["halls"][0]["weekday_profile"]
    assert result["halls"][0]["long_term"]["sample_days"] == 4
