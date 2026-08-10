from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import app
import opportunity.models as models


client = TestClient(app)


def test_mobile_sync_is_private_and_collects_intraday(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", tmp_path / "opportunities.db")
    models.init_db()
    key = "a" * 32
    state = {
        "version": 3,
        "results": [],
        "patrol_observations": [
            {
                "id": "obs-1", "session_id": "session-1", "observed_at": "2026-08-10T12:10:00",
                "time_bucket": "12:00", "hall_name": "テスト店", "seat_number": 501,
                "machine_name": "スマスロ 北斗の拳", "current_value": 650, "status": "watch",
            }
        ],
        "hall_reset_records": [
            {"id": "reset-1", "recorded_on": "2026-08-10", "hall_name": "テスト店",
             "machine_name": "スマスロ北斗の拳", "seat_number": 501,
             "reset_status": "reset_confirmed", "evidence": "朝一天井短縮"},
            {"id": "reset-2", "recorded_on": "2026-08-03", "hall_name": "テスト店",
             "machine_name": "スマスロ北斗の拳", "seat_number": 502,
             "reset_status": "reset_confirmed", "evidence": "朝一天井短縮"},
            {"id": "reset-3", "recorded_on": "2026-07-27", "hall_name": "テスト店",
             "machine_name": "スマスロ北斗の拳", "seat_number": 503,
             "reset_status": "normal", "evidence": "前日G引継ぎ"},
        ],
    }
    saved = client.put("/api/opportunity/sync", headers={"X-Sync-Key": key}, json={"state": state})
    assert saved.status_code == 200, saved.text
    assert saved.json()["observation_count"] == 1
    assert saved.json()["reset_record_count"] == 3

    loaded = client.get("/api/opportunity/sync", headers={"X-Sync-Key": key})
    assert loaded.status_code == 200
    assert loaded.json()["state"]["patrol_observations"][0]["seat_number"] == 501

    other = client.get("/api/opportunity/sync", headers={"X-Sync-Key": "b" * 32})
    assert other.status_code == 404
    assert models.get_intraday_coverage("テスト店")["records"] == 1
    tendency = client.get("/api/opportunity/reset-tendency", params={
        "hall_name": "テスト店", "machine_name": "スマスロ北斗の拳", "weekday": 0,
    })
    assert tendency.status_code == 200
    assert tendency.json()["selected"]["samples"] == 3
    assert tendency.json()["selected"]["reset_rate"] == 66.7


def test_mobile_sync_rejects_short_key(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", tmp_path / "opportunities.db")
    models.init_db()
    response = client.put("/api/opportunity/sync", headers={"X-Sync-Key": "short"}, json={"state": {}})
    assert response.status_code == 422
