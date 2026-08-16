"""hall/occupancy.py + api/routers/occupancy.py のテスト。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import hall.occupancy as occupancy_models
import scraper.anaslo as anaslo
from api.main import app

client = TestClient(app)


def _isolate_db(tmp_path, monkeypatch):
    """occupancy と hall config を同じ隔離DBに向ける(本番では同じ hall_reports.db を共有するため)。"""
    db_path = tmp_path / "hall_reports.db"
    monkeypatch.setattr(occupancy_models, "DB_PATH", db_path)
    monkeypatch.setattr(anaslo, "DB_PATH", db_path)
    occupancy_models.init_db().close()


def test_record_occupancy_rejects_invalid_level(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        occupancy_models.record_occupancy("テスト店", "invalid")


def test_record_occupancy_requires_hall_name(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        occupancy_models.record_occupancy("", "high")


def test_record_occupancy_derives_weekday_and_time_bucket(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    row = occupancy_models.record_occupancy("テスト店", "high", recorded_at="2026-08-10T14:30:00")
    assert row["weekday"] == 0  # 2026-08-10は月曜日
    assert row["time_bucket"] == "12-15"
    assert row["level"] == "high"
    assert row["avg_rotation_games_per_hour"] is None


def test_patrol_list_prioritizes_unrecorded_then_stale_then_fresh_high(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    anaslo.upsert_hall_config("店A", prefecture="大阪府")
    anaslo.upsert_hall_config("店B", prefecture="大阪府")
    anaslo.upsert_hall_config("店C", prefecture="大阪府")

    # 店A: たった今 high で記録 -> しばらく再訪不要 = 優先度最低
    occupancy_models.record_occupancy("店A", "high")
    # 店B: 一度も記録が無い -> 優先度最高
    # 店C: 48時間前に low で記録 -> 店Aより優先度は高いが店Bほどではない
    old_time = (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds")
    occupancy_models.record_occupancy("店C", "low", recorded_at=old_time)

    result = occupancy_models.get_patrol_list()
    names = [r["hall_name"] for r in result]
    assert names.index("店B") < names.index("店C") < names.index("店A")

    by_name = {r["hall_name"]: r for r in result}
    assert by_name["店B"]["last_level"] is None
    assert by_name["店C"]["last_level"] == "low"
    assert by_name["店A"]["last_level"] == "high"


def test_patrol_list_filters_by_hall_names(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    anaslo.upsert_hall_config("店A")
    anaslo.upsert_hall_config("店B")

    result = occupancy_models.get_patrol_list(hall_names=["店A"])
    assert [r["hall_name"] for r in result] == ["店A"]


def test_patrol_list_filters_by_prefecture(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    anaslo.upsert_hall_config("大阪店", prefecture="大阪府")
    anaslo.upsert_hall_config("松本店", prefecture="長野県")

    result = occupancy_models.get_patrol_list(prefecture="長野県")
    assert [r["hall_name"] for r in result] == ["松本店"]


def test_api_create_and_patrol_list_round_trip(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    anaslo.upsert_hall_config("APIテスト店")

    res = client.post("/api/occupancy", json={
        "hall_name": "APIテスト店", "level": "mid", "avg_rotation_games_per_hour": 45.5,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["hall_name"] == "APIテスト店"
    assert body["level"] == "mid"
    assert body["avg_rotation_games_per_hour"] == 45.5

    res2 = client.get("/api/occupancy/patrol-list")
    assert res2.status_code == 200
    row = next(r for r in res2.json() if r["hall_name"] == "APIテスト店")
    assert row["last_level"] == "mid"


def test_api_rejects_invalid_level(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    res = client.post("/api/occupancy", json={"hall_name": "x", "level": "invalid"})
    assert res.status_code == 422


def test_statistics_uses_same_weekday_and_time_bucket(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    for recorded_at, level in [
        ("2026-07-27T14:10:00", "mid"),
        ("2026-08-03T13:40:00", "high"),
        ("2026-08-10T14:20:00", "mid"),
        ("2026-08-11T19:00:00", "low"),
    ]:
        occupancy_models.record_occupancy("統計店", level, 52, recorded_at)
    result = occupancy_models.get_occupancy_statistics(
        "統計店", "2026-08-17T14:30:00", lookback_days=90
    )
    assert result["time_bucket"] == "12-15"
    assert result["matching_sample_count"] == 3
    assert result["sample_count"] == 3
    assert result["confidence"] == "medium"
    assert result["predicted_level"] in {"mid", "high"}
    assert result["avg_rotation_games_per_hour"] == 52


def test_hyena_ranking_combines_supported_machines_activity_and_occupancy(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    anaslo.upsert_hall_config("対応多い店")
    anaslo.upsert_hall_config("データなし店")
    with occupancy_models._conn() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS hall_day_machine (
                id INTEGER PRIMARY KEY, hall_name TEXT, report_date TEXT,
                machine_name TEXT, unit_count INTEGER, avg_diff_coins INTEGER,
                avg_games INTEGER)"""
        )
        con.execute(
            """CREATE TABLE hall_machine_snapshot (
                id INTEGER PRIMARY KEY, hall_name TEXT, snapshot_date TEXT,
                machine_name TEXT, machine_id TEXT, source_url TEXT)"""
        )
        machines = [
            "スマスロ からくりサーカス", "スマスロ 革命機ヴァルヴレイヴ",
            "スマスロ ToLOVEるダークネス", "スマスロ 聖闘士星矢 海皇覚醒 CUSTOM EDITION",
            "スマスロ モンキーターン5", "スマスロ ゴッドイーター リザレクション",
        ]
        con.executemany(
            "INSERT INTO hall_machine_snapshot VALUES (NULL,?,?,?,?,?)",
            [("対応多い店", "2026-08-11", name, str(i), "https://example.com") for i, name in enumerate(machines)],
        )
        con.execute(
            """INSERT INTO hall_day_machine
               (hall_name,report_date,machine_name,unit_count,avg_diff_coins,avg_games)
               VALUES ('対応多い店','2026-08-11','スマスロ からくりサーカス',10,0,5200)"""
        )
    for day in (3, 10):
        occupancy_models.record_occupancy("対応多い店", "mid", 55, f"2026-08-{day:02d}T14:00:00")
    result = occupancy_models.rank_hyena_halls("2026-08-17T14:30:00")
    assert result["halls"][0]["hall_name"] == "対応多い店"
    best = result["halls"][0]
    assert best["machines"]["supported_machine_count"] >= 5
    assert best["score"] > next(row["score"] for row in result["halls"] if row["hall_name"] == "データなし店")
    assert best["reasons"]
    assert "保証" in result["notice"]


def test_hyena_ranking_api_and_statistics_api(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    anaslo.upsert_hall_config("APIランキング店")
    occupancy_models.record_occupancy("APIランキング店", "mid", 48, "2026-08-10T15:00:00")
    stats = client.get("/api/occupancy/statistics", params={
        "hall_name": "APIランキング店", "at": "2026-08-17T15:10:00",
    })
    assert stats.status_code == 200, stats.text
    assert stats.json()["hall_name"] == "APIランキング店"
    ranking = client.get("/api/occupancy/hyena-stores", params={
        "at": "2026-08-17T15:10:00", "limit": 5,
    })
    assert ranking.status_code == 200, ranking.text
    assert ranking.json()["halls"][0]["hall_name"] == "APIランキング店"


def test_hyena_ranking_api_filters_prefecture(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    anaslo.upsert_hall_config("大阪店", prefecture="大阪府")
    anaslo.upsert_hall_config("松本店", prefecture="長野県")
    response = client.get("/api/occupancy/hyena-stores", params={"prefecture": "長野県"})
    assert response.status_code == 200
    assert response.json()["prefecture"] == "長野県"
    assert [row["hall_name"] for row in response.json()["halls"]] == ["松本店"]


def test_sparse_occupancy_never_gets_strong_verdict(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    anaslo.upsert_hall_config("未記録店")
    result = occupancy_models.rank_hyena_halls("2026-08-17T14:00:00")
    row = result["halls"][0]
    assert row["score"] <= 69
    assert row["verdict"] != "strong"
