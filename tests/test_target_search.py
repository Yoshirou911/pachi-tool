import sqlite3
from types import SimpleNamespace

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
    assert result["target_accuracy"] == 70
    assert result["accuracy_summary"]["target_pct"] == 70

    next_day = hall_router.get_target_search("2026-08-11", days=120, limit=8)
    assert next_day["weekday"] == "火"
    assert next_day["halls"][0]["avg_diff"] != result["halls"][0]["avg_diff"]

    strict = hall_router.get_target_search(
        "2026-08-10", days=120, limit=8, target_accuracy=80
    )
    assert strict["target_accuracy"] == 80
    assert strict["accuracy_summary"]["target_pct"] == 80


def test_target_search_rejects_invalid_date():
    with pytest.raises(HTTPException) as exc:
        hall_router.get_target_search("2026/08/10")
    assert exc.value.status_code == 400


def test_target_search_weights_daily_average_by_installed_units(tmp_path, monkeypatch):
    db_path = tmp_path / "weighted.db"
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scrape_hall_config (hall_name TEXT PRIMARY KEY, enabled INTEGER);
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT,
            avg_diff_coins INTEGER, win_rate_pct REAL, unit_count INTEGER,
            source_url TEXT
        );
        INSERT INTO scrape_hall_config VALUES ('加重店', 1);
        """
    )
    rows = []
    for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
        rows.extend([
            ("加重店", day, "L主力", 100, 55, 20, "https://source/main"),
            ("加重店", day, "L少数", -1000, 0, 1, "https://source/minor"),
        ])
    conn.executemany("INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    monkeypatch.setattr(hall_router, "_get_reports_conn", lambda: _connect(db_path))
    result = hall_router.get_target_search("2026-08-10", days=120, limit=8)

    assert result["halls"][0]["baseline_avg"] == 48


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


def test_target_search_filters_matsumoto_shiojiri_region(tmp_path, monkeypatch):
    db_path = tmp_path / "region.db"
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scrape_hall_config (
            hall_name TEXT PRIMARY KEY, prefecture TEXT, enabled INTEGER
        );
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT,
            avg_diff_coins INTEGER, win_rate_pct REAL, unit_count INTEGER,
            source_url TEXT
        );
        INSERT INTO scrape_hall_config VALUES
            ('マルハン松本店', '長野県', 1),
            ('ニコニコ住道店', '大阪府', 1);
        """
    )
    rows = []
    for report_date in ("2026-08-01", "2026-08-02", "2026-08-03"):
        rows.extend([
            ("マルハン松本店", report_date, "L東京喰種", 500, 60, 10, "https://source/nagano"),
            ("ニコニコ住道店", report_date, "L北斗", 800, 70, 10, "https://source/osaka"),
        ])
    conn.executemany("INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    monkeypatch.setattr(hall_router, "_get_reports_conn", lambda: _connect(db_path))
    result = hall_router.get_target_search(
        "2026-08-10", days=120, limit=8, region="matsumoto_shiojiri"
    )

    assert result["region_label"] == "松本・塩尻"
    assert [hall["hall_name"] for hall in result["halls"]] == ["マルハン松本店"]


def test_target_search_excludes_machine_missing_from_fresh_installation_snapshot(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "installation.db"
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scrape_hall_config (
            hall_name TEXT PRIMARY KEY, prefecture TEXT, enabled INTEGER
        );
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT,
            avg_diff_coins INTEGER, win_rate_pct REAL, unit_count INTEGER,
            source_url TEXT, avg_games REAL
        );
        CREATE TABLE hall_machine_snapshot (
            hall_name TEXT, snapshot_date TEXT, machine_name TEXT
        );
        INSERT INTO scrape_hall_config VALUES ('マルハン松本店', '長野県', 1);
        INSERT INTO hall_machine_snapshot VALUES ('マルハン松本店', '2026-08-09', 'Ｌ 東京喰種');
        """
    )
    rows = []
    for report_date in ("2026-08-01", "2026-08-02", "2026-08-03"):
        rows.extend([
            ("マルハン松本店", report_date, "L東京喰種", 400, 60, 10, "https://source/current", 3500),
            ("マルハン松本店", report_date, "L撤去済み機種", 1200, 75, 5, "https://source/removed", 3000),
        ])
    conn.executemany("INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    monkeypatch.setattr(hall_router, "_get_reports_conn", lambda: _connect(db_path))
    result = hall_router.get_target_search("2026-08-10", days=120, limit=8)

    hall = result["halls"][0]
    assert [item["machine_name"] for item in hall["target_machines"]] == ["L東京喰種"]
    assert hall["target_machines"][0]["installation_status"] == "現行設置を確認"
    assert hall["data_quality"]["excluded_not_installed"] == 1


def test_target_search_excludes_suspicious_zero_diff_hall(tmp_path, monkeypatch):
    db_path = tmp_path / "quality.db"
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scrape_hall_config (
            hall_name TEXT PRIMARY KEY, prefecture TEXT, enabled INTEGER
        );
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT,
            avg_diff_coins INTEGER, win_rate_pct REAL, unit_count INTEGER,
            source_url TEXT, avg_games REAL
        );
        INSERT INTO scrape_hall_config VALUES ('APULO811', '長野県', 1);
        """
    )
    rows = [
        ("APULO811", f"2026-07-{day:02d}", "L北斗", 0, 50, 10, "https://source/bad", 2500)
        for day in range(1, 31)
    ]
    conn.executemany("INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    monkeypatch.setattr(hall_router, "_get_reports_conn", lambda: _connect(db_path))
    result = hall_router.get_target_search(
        "2026-08-10", days=120, limit=8, region="matsumoto_shiojiri"
    )

    assert result["halls"] == []
    assert result["insufficient_halls"][0]["hall_name"] == "APULO811"
    assert "差枚欠損" in result["insufficient_halls"][0]["reason"]


def test_seat_patterns_require_history_and_personal_profit_never_raises_prediction():
    rows = []
    for day in range(1, 21):
        rows.extend([
            {"report_date": f"2026-07-{day:02d}", "machine_name": "L北斗", "seat_number": 101, "diff_coins": 500, "games": 3000},
            {"report_date": f"2026-07-{day:02d}", "machine_name": "L北斗", "seat_number": 102, "diff_coins": -300, "games": 3000},
        ])
    layouts = [
        {"seat_number": 101, "machine_name": "L北斗", "island_name": "A"},
        {"seat_number": 102, "machine_name": "L北斗", "island_name": "A"},
    ]
    sessions = [
        SimpleNamespace(
            hall_name="検証店", date=f"2026-07-{day:02d}", started_from=0,
            diff_yen=-1000, id=day,
        )
        for day in range(1, 11)
    ]

    seat_summary = hall_router._seat_pattern_summary(rows, layouts)
    profit = hall_router._personal_profit_summary(sessions, "検証店", hall_router.date(2026, 8, 1))

    assert seat_summary["top_seats"][0]["seat_number"] == 101
    assert seat_summary["top_seats"][0]["status"] == "検証対象"
    assert seat_summary["corner"]["verified_layout"] is True
    assert profit["all"]["count"] == 10
    assert profit["all"]["avg_yen"] == -1000
    assert profit["can_raise_prediction"] is False


def test_seat_patterns_exclude_machine_that_moved_to_another_seat():
    rows = [
        {
            "report_date": f"2026-07-{day:02d}", "machine_name": "L炎炎ノ消防隊",
            "seat_number": 325, "diff_coins": 500, "games": 3000,
        }
        for day in range(1, 21)
    ]
    rows.append({
        "report_date": "2026-07-21", "machine_name": "Lガンダムユニコーン",
        "seat_number": 325, "diff_coins": 100, "games": 3000,
    })

    summary = hall_router._seat_pattern_summary(rows, [])

    assert all(item["machine_name"] != "L炎炎ノ消防隊" for item in summary["top_seats"])


def test_target_search_merges_machine_name_variants(tmp_path, monkeypatch):
    db_path = tmp_path / "aliases.db"
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scrape_hall_config (hall_name TEXT PRIMARY KEY, enabled INTEGER);
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT,
            avg_diff_coins INTEGER, win_rate_pct REAL, unit_count INTEGER,
            source_url TEXT
        );
        INSERT INTO scrape_hall_config VALUES ('表記統合店', 1);
        """
    )
    rows = [
        ("表記統合店", "2026-08-01", "モンキーターンV", 300, 60, 5, "https://source/1"),
        ("表記統合店", "2026-08-02", "スマスロモンキーターンV", 500, 65, 5, "https://source/2"),
        ("表記統合店", "2026-08-03", "LモンキーターンV", 700, 70, 5, "https://source/3"),
    ]
    conn.executemany("INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    monkeypatch.setattr(hall_router, "_get_reports_conn", lambda: _connect(db_path))
    result = hall_router.get_target_search("2026-08-10", days=120, limit=8)

    machines = result["halls"][0]["target_machines"]
    assert len(machines) == 1
    assert machines[0]["sample_days"] == 3


def test_collection_days_expand_to_cover_missed_dates(tmp_path, monkeypatch):
    db_path = tmp_path / "reports.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE hall_day_machine(hall_name TEXT, report_date TEXT);
        CREATE TABLE hall_day_seat(hall_name TEXT, report_date TEXT);
    """)
    latest = (hall_router.date.today() - hall_router.timedelta(days=8)).isoformat()
    conn.execute("INSERT INTO hall_day_machine VALUES (?,?)", ("テスト店", latest))
    conn.commit()
    conn.close()

    monkeypatch.setattr(hall_router, "_get_reports_conn", lambda: sqlite3.connect(db_path))

    assert hall_router._recommended_collection_days("テスト店", 3) == 10
    assert hall_router._recommended_collection_days("未収集店", 3) == 30
