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
        "version": 2,
        "results": [],
        "patrol_observations": [
            {
                "id": "obs-1", "session_id": "session-1", "observed_at": "2026-08-10T12:10:00",
                "time_bucket": "12:00", "hall_name": "テスト店", "seat_number": 501,
                "machine_name": "スマスロ 北斗の拳", "current_value": 650, "status": "watch",
            }
        ],
    }
    saved = client.put("/api/opportunity/sync", headers={"X-Sync-Key": key}, json={"state": state})
    assert saved.status_code == 200, saved.text
    assert saved.json()["observation_count"] == 1

    loaded = client.get("/api/opportunity/sync", headers={"X-Sync-Key": key})
    assert loaded.status_code == 200
    assert loaded.json()["state"]["patrol_observations"][0]["seat_number"] == 501

    other = client.get("/api/opportunity/sync", headers={"X-Sync-Key": "b" * 32})
    assert other.status_code == 404
    assert models.get_intraday_coverage("テスト店")["records"] == 1


def test_mobile_sync_rejects_short_key(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", tmp_path / "opportunities.db")
    models.init_db()
    response = client.put("/api/opportunity/sync", headers={"X-Sync-Key": "short"}, json={"state": {}})
    assert response.status_code == 422
