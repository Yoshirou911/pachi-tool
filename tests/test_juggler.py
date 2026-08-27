import sqlite3

import pytest
from fastapi import HTTPException

from api.routers import juggler as juggler_router
from juggler.models import assess_juggler, catalog


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_catalog_uses_official_profiles():
    profiles = catalog()
    assert {item["id"] for item in profiles} >= {
        "my5", "neo_im", "funky2", "gogo3", "mister",
        "happy3", "girls_ss", "ultra_miracle",
    }
    assert all(item["source_url"].startswith("https://www.kitadenshi.co.jp/") for item in profiles)


def test_assessment_separates_strong_and_weak_bonus_counts():
    strong = assess_juggler("my5", games=6000, bb_count=26, rb_count=26)
    weak = assess_juggler("my5", games=6000, bb_count=18, rb_count=12)
    assert strong["action"] == "続行候補・90%級"
    assert strong["prediction_grade"] == "90%級"
    assert strong["high_setting_probability_pct"] >= 90
    assert strong["high_low_likelihood_ratio"] >= 6
    assert strong["setting5_or_higher_probability_pct"] >= strong["setting6_probability_pct"]
    assert strong["expected_setting"] >= 4
    assert strong["sample_adequacy_pct"] == 100
    assert weak["action"].startswith("見送り候補")
    assert weak["high_setting_probability_pct"] <= 25


def test_assessment_abstains_on_small_sample():
    result = assess_juggler("my5", games=1500, bb_count=7, rb_count=7)
    assert result["action"] == "判定保留"
    assert result["confidence"] == "データ不足"


def test_assessment_rejects_impossible_counts():
    with pytest.raises(ValueError):
        assess_juggler("my5", games=10, bb_count=8, rb_count=8)


def test_api_rejects_unknown_profile():
    with pytest.raises(HTTPException) as exc:
        juggler_router.assess_juggler_api(
            juggler_router.JugglerAssessmentRequest(
                profile_id="unknown", games=5000, bb_count=20, rb_count=20
            )
        )
    assert exc.value.status_code == 404


def test_collected_bonus_probability_is_restored_to_count():
    assert juggler_router._bonus_count(1 / 300, 6000) == 20
    assert juggler_router._bonus_count(26, 6000) == 26
    assert juggler_router._bonus_count(None, 6000) is None


def test_morning_targets_rank_only_juggler_history(tmp_path, monkeypatch):
    db_path = tmp_path / "juggler.db"
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scrape_hall_config (
            hall_name TEXT PRIMARY KEY, prefecture TEXT, enabled INTEGER
        );
        CREATE TABLE hall_day_seat (
            hall_name TEXT, report_date TEXT, machine_name TEXT, seat_number INTEGER,
            diff_coins INTEGER, games INTEGER, bb_prob REAL, rb_prob REAL,
            source_url TEXT
        );
        INSERT INTO scrape_hall_config VALUES ('マルハン松本店', '長野県', 1);
        """
    )
    rows = []
    for day in range(1, 23):
        rows.extend([
            (
                "マルハン松本店", f"2026-08-{day:02d}", "マイジャグラーV", 501,
                1200, 6000, 26 / 6000, 26 / 6000, "https://source/juggler",
            ),
            (
                "マルハン松本店", f"2026-08-{day:02d}", "L東京喰種", 601,
                3000, 5000, 20, 20, "https://source/smartslot",
            ),
        ])
    conn.executemany("INSERT INTO hall_day_seat VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    monkeypatch.setattr(juggler_router, "_get_reports_conn", lambda: _connect(db_path))
    result = juggler_router.get_juggler_targets(
        "2026-08-25", days=180, limit=20, region="matsumoto_shiojiri"
    )
    assert result["data_coverage"]["rows"] == 22
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["machine_name"] == "マイジャグラーV"
    assert result["candidates"][0]["action"] == "要確認"
    assert result["candidates"][0]["evidence_level"] == "bonus_counts_growing"
    assert result["candidates"][0]["validation"]["status"] == "insufficient"


def test_shijonawate_machine_daily_history_produces_machine_candidate(tmp_path, monkeypatch):
    db_path = tmp_path / "shijonawate.db"
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scrape_hall_config (
            hall_name TEXT PRIMARY KEY, prefecture TEXT, enabled INTEGER
        );
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT, unit_count INTEGER,
            avg_diff_coins INTEGER, avg_games INTEGER, win_rate_pct REAL,
            source_url TEXT
        );
        INSERT INTO scrape_hall_config VALUES ('キコーナ四條畷店', '大阪府', 1);
        """
    )
    conn.executemany(
        "INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                "キコーナ四條畷店", f"2026-08-{day:02d}", "マイジャグラーV", 20,
                350, 4200, 60.0, "https://anoslot.moe/stores/7964",
            )
            for day in range(1, 23)
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(juggler_router, "_get_reports_conn", lambda: _connect(db_path))
    result = juggler_router.get_juggler_targets(
        "2026-08-25", days=180, limit=20, region="shijonawate"
    )
    assert result["region_label"] == "四條畷駅周辺"
    assert result["data_coverage"]["rows"] == 22
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["scope"] == "machine"
    assert candidate["seat_number"] is None
    assert candidate["action"] == "要確認"
    assert candidate["validation"]["status"] == "insufficient"


def test_juggler_candidate_requires_walk_forward_validation(tmp_path, monkeypatch):
    from datetime import date, timedelta

    db_path = tmp_path / "validated-juggler.db"
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scrape_hall_config (hall_name TEXT PRIMARY KEY, prefecture TEXT, enabled INTEGER);
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT, unit_count INTEGER,
            avg_diff_coins INTEGER, avg_games INTEGER, win_rate_pct REAL, source_url TEXT
        );
        INSERT INTO scrape_hall_config VALUES ('キコーナ四條畷店', '大阪府', 1);
        """
    )
    start = date(2026, 5, 1)
    conn.executemany(
        "INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?,?,?)",
        [
            ("キコーナ四條畷店", (start + timedelta(days=index)).isoformat(),
             "マイジャグラーV", 20, 400, 5000, 65.0, "https://source")
            for index in range(75)
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(juggler_router, "_get_reports_conn", lambda: _connect(db_path))
    result = juggler_router.get_juggler_targets(
        "2026-08-01", days=180, limit=20, region="shijonawate"
    )
    candidate = result["candidates"][0]
    assert candidate["validation"]["status"] == "validated"
    assert candidate["validation"]["recommendation_success_pct"] == 100
    assert candidate["action"] == "要確認"
    assert candidate["evidence_level"] == "diff_proxy"


def test_juggler_90_grade_requires_bonus_count_history(tmp_path, monkeypatch):
    from datetime import date, timedelta

    db_path = tmp_path / "juggler-90.db"
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scrape_hall_config (hall_name TEXT PRIMARY KEY, prefecture TEXT, enabled INTEGER);
        CREATE TABLE hall_day_seat (
            hall_name TEXT, report_date TEXT, machine_name TEXT, seat_number INTEGER,
            diff_coins INTEGER, games INTEGER, bb_prob REAL, rb_prob REAL, source_url TEXT
        );
        INSERT INTO scrape_hall_config VALUES ('キコーナ四條畷店', '大阪府', 1);
        """
    )
    start = date(2026, 5, 1)
    conn.executemany(
        "INSERT INTO hall_day_seat VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                "キコーナ四條畷店", (start + timedelta(days=index)).isoformat(),
                "マイジャグラーV", 450, 1200, 6000,
                26 / 6000, 26 / 6000, "https://source",
            )
            for index in range(75)
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(juggler_router, "_get_reports_conn", lambda: _connect(db_path))
    result = juggler_router.get_juggler_targets(
        "2026-08-01", days=180, limit=20, region="shijonawate"
    )
    candidate = result["candidates"][0]
    assert candidate["evidence_level"] == "bonus_counts"
    assert candidate["validation"]["trust_level"] == "90%級"
    assert candidate["action"] == "朝一候補・90%級"
