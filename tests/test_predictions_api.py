import sqlite3
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import predictions
from hall.prediction_log import JST, initialize


def test_verification_routes_are_separate_and_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    def connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, 12, tzinfo=JST)
    monkeypatch.setattr(predictions, "_get_reports_conn", connect)
    monkeypatch.setattr(predictions, "datetime", FixedClock)
    # save_batch uses its own clock, so replace both clocks (never the user's system clock).
    monkeypatch.setattr("hall.prediction_log.datetime", FixedClock)
    monkeypatch.setattr("api.scheduler.get_scheduler", lambda: None)
    calls = []
    def build(*args, **kwargs):
        calls.append((args, kwargs))
        return {"visit_date": "2026-09-09", "region": "shijonawate", "input_cutoff_date": "2026-09-07",
                "frozen_inputs": {"machine_rows": [{"report_date": "2026-09-07"}]},
                "forecast_subjects": [{"scope": "machine", "hall_name": "店", "machine_name": "L北斗", "seat_number": 0,
                                       "projected": 1, "probability_pct": 50, "action": "見送り", "quality": {"analysis_eligible": False}}]}
    monkeypatch.setattr("api.routers.hall._build_target_search", build)
    app = FastAPI()
    app.include_router(predictions.router)
    client = TestClient(app)
    assert client.get("/api/predictions/verification").json()["batches"] == 0
    assert calls == []  # GET never computes or creates a forecast
    response = client.post("/api/predictions/run")
    assert response.status_code == 200 and response.json()["recorded"] == 1
    assert client.post("/api/predictions/run").json()["status"] == "already_saved"
    assert len(calls) == 1
    assert calls[0][0] == ("2026-09-09", 120, 20, "shijonawate", 70)
    assert calls[0][1] == {"include_inputs": True}
    result = client.get("/api/predictions/verification").json()
    assert result["batches"] == 1
    assert result["by_scope"][0]["success_pct"] is None
    assert result["recent"][0]["result"] == "結果待ち"
    predictions._run_lock.acquire()
    try:
        assert client.post("/api/predictions/run").status_code == 409
    finally:
        predictions._run_lock.release()


def test_empty_db_does_not_claim_saved(monkeypatch):
    monkeypatch.setattr(predictions, "_get_reports_conn", lambda: None)
    app = FastAPI()
    app.include_router(predictions.router)
    response = TestClient(app).post("/api/predictions/run")
    assert response.status_code == 409
    assert not predictions._run_lock.locked()
