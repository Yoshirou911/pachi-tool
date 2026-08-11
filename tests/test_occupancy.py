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
