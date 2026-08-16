import json
import sqlite3
from datetime import datetime, timedelta

from scraper import anoslot_public


def _flight_html(payload: dict) -> str:
    chunk = '1:' + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    push = json.dumps([1, chunk], ensure_ascii=False)
    return f"<html><script>self.__next_f.push({push})</script></html>"


def test_parse_public_daily_machine_rows():
    payload = {
        "machines": [
            {
                "machineName": "Lスマスロ北斗の拳",
                "machineId": 123,
                "dailyData": [
                    {
                        "date": "2026-08-09", "units": 10, "activeUnits": 8,
                        "diff": 4000, "avgG": 6123.4, "positiveUnits": 5,
                    }
                ],
            },
            {
                "machineName": "マイジャグラーV",
                "machineId": 456,
                "dailyData": [{"date": "2026-08-09", "activeUnits": 5, "diff": 1000}],
            },
        ],
        "storeName": "テスト店",
    }
    rows = anoslot_public.parse_store_page(
        _flight_html(payload), "テスト店", "https://anoslot.moe/stores/1"
    )
    assert len(rows) == 1
    assert rows[0]["machine_name"] == "Lスマスロ北斗の拳"
    assert rows[0]["unit_count"] == 8
    assert rows[0]["avg_diff_coins"] == 500
    assert rows[0]["avg_games"] == 6123
    assert rows[0]["win_rate_pct"] == 62.5


def test_save_rows_only_fills_missing_analysis_value(tmp_path):
    db = tmp_path / "hall.db"
    conn = anoslot_public.init_db(db)
    conn.execute(
        """INSERT INTO hall_day_machine
           (hall_name,report_date,machine_name,avg_diff_coins)
           VALUES ('既存店','2026-08-09','L北斗',100)"""
    )
    conn.commit()
    base = {
        "source": "anoslot", "report_date": "2026-08-09", "machine_name": "L北斗",
        "machine_id": "1", "unit_count": 5, "total_diff_coins": 2500,
        "avg_diff_coins": 500, "avg_games": 6000, "win_rate_pct": 60.0,
        "source_url": "https://anoslot.moe/stores/1",
    }
    anoslot_public.save_rows(conn, [{**base, "hall_name": "既存店"}, {**base, "hall_name": "空店"}])
    assert conn.execute(
        "SELECT avg_diff_coins FROM hall_day_machine WHERE hall_name='既存店'"
    ).fetchone()[0] == 100
    assert conn.execute(
        "SELECT avg_diff_coins FROM hall_day_machine WHERE hall_name='空店'"
    ).fetchone()[0] == 500
    assert conn.execute("SELECT COUNT(*) FROM hall_source_machine_daily").fetchone()[0] == 2
    conn.close()


def test_parse_accepts_name_key_used_by_live_page():
    payload = {
        "storeName": "test hall",
        "machines": [{
            "name": "L smartslot Hokuto",
            "machineId": 7,
            "dailyData": [{
                "date": "2026-08-09", "units": 4, "activeUnits": 4,
                "diff": 2400, "avgG": 5100, "positiveUnits": 3,
            }],
        }],
    }
    rows = anoslot_public.parse_store_page(
        _flight_html(payload), "test hall", "https://anoslot.moe/stores/7"
    )
    assert len(rows) == 1
    assert rows[0]["avg_diff_coins"] == 600


def test_parse_rejects_hall_wide_masked_zero_results():
    payload = {
        "storeName": "masked hall",
        "machines": [{
            "name": "L smartslot Hokuto",
            "machineId": 7,
            "dailyData": [
                {
                    "date": f"2026-07-{day:02d}", "units": 4, "activeUnits": 4,
                    "diff": 0, "avgG": 3000, "positiveUnits": 0,
                }
                for day in range(1, 21)
            ],
        }],
    }
    rows = anoslot_public.parse_store_page(
        _flight_html(payload), "masked hall", "https://anoslot.moe/stores/7"
    )
    assert rows == []


def test_save_rows_does_not_duplicate_cross_source_machine_alias(tmp_path):
    conn = anoslot_public.init_db(tmp_path / "aliases.db")
    conn.execute(
        """INSERT INTO hall_day_machine
           (hall_name,report_date,machine_name,avg_diff_coins,source_url)
           VALUES ('hall','2026-08-09','L pachislot Hokuto',100,'https://primary.test')"""
    )
    conn.commit()
    row = {
        "source": "anoslot", "hall_name": "hall", "report_date": "2026-08-09",
        "machine_name": "pachislot Hokuto", "machine_id": "1", "unit_count": 5,
        "total_diff_coins": 2500, "avg_diff_coins": 500, "avg_games": 6000,
        "win_rate_pct": 60.0, "source_url": "https://anoslot.moe/stores/1",
    }
    anoslot_public.save_rows(conn, [row])
    assert conn.execute("SELECT COUNT(*) FROM hall_source_machine_daily").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM hall_day_machine").fetchone()[0] == 1
    conn.close()


def test_refresh_due_uses_latest_successful_source_timestamp(tmp_path):
    db = tmp_path / "refresh.db"
    assert anoslot_public.is_refresh_due(db, max_age_hours=6) is True
    conn = anoslot_public.init_db(db)
    conn.execute(
        """INSERT INTO hall_source_machine_daily
           (source,hall_name,report_date,machine_name,source_url,scraped_at)
           VALUES ('anoslot','hall','2026-08-09','machine','https://example.test',?)""",
        (datetime.now().isoformat(timespec="seconds"),),
    )
    conn.commit()
    conn.close()
    assert anoslot_public.is_refresh_due(db, max_age_hours=6) is False

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE hall_source_machine_daily SET scraped_at=?",
        ((datetime.now() - timedelta(hours=7)).isoformat(timespec="seconds"),),
    )
    conn.commit()
    conn.close()
    assert anoslot_public.is_refresh_due(db, max_age_hours=6) is True
