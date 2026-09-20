import sqlite3
from datetime import date

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import prediction_replay as replay
from api.routers import predictions


@pytest.fixture(autouse=True)
def reset_state():
    original = replay.status()
    replay._set(running=False, status="idle", completed=0, percent=0)
    yield
    with replay._guard:
        replay._state.clear()
        replay._state.update(original)


def test_comparison_get_does_not_start_replay_or_create_schema(monkeypatch):
    monkeypatch.setattr(predictions, "_get_reports_conn", lambda: None)
    monkeypatch.setattr(replay, "start", lambda: pytest.fail("GET started a replay"))
    app = FastAPI()
    app.include_router(predictions.router)
    response = TestClient(app).get("/api/predictions/comparison")
    assert response.status_code == 200
    assert response.json()["prospective"]["cohorts"] == []
    assert response.json()["retrospective"] is None


def test_start_is_async_fixed_and_rejects_duplicate(monkeypatch):
    threads = []
    class FakeThread:
        def __init__(self, **kwargs):
            threads.append(kwargs)
        def start(self):
            pass
    monkeypatch.setattr(replay, "Thread", FakeThread)
    app = FastAPI()
    app.include_router(predictions.router)
    client = TestClient(app)
    r = client.post("/api/predictions/benchmark", json={"target": "future", "probability": 100})
    assert r.status_code == 202
    assert r.json()["total"] == 7 and r.json()["running"]
    assert threads[0]["target"] is replay._worker
    assert client.post("/api/predictions/benchmark").status_code == 409


def test_failed_worker_not_complete_or_stuck(monkeypatch):
    monkeypatch.setattr(replay, "_get_reports_conn", lambda: None)
    replay._set(running=True)
    replay._worker()
    assert replay.status()["status"] == "failed"
    assert not replay.status()["running"]
    assert replay.status()["percent"] != 100


def test_completed_report_persists_and_final_progress_after_save(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite"
    def connect():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(replay, "_get_reports_conn", connect)
    monkeypatch.setattr(replay, "_load_actuals", lambda *a: (["2026-08-31"], {}))
    def calculate(dates, build, actual, version, progress):
        progress(1, 1, dates[0])
        assert replay.status()["percent"] == 99
        return {"version": version, "cohorts": [], "dates": dates}
    monkeypatch.setattr(replay, "run_replay", calculate)
    replay._set(running=True)
    replay._worker()
    assert replay.status()["status"] == "completed" and replay.status()["percent"] == 100
    assert replay.status()["report_id"] == 1
    c = connect()
    assert c.execute("SELECT COUNT(*) FROM prediction_benchmark_report").fetchone()[0] == 1
    c.close()


def test_selects_latest_seven_calendar_days_not_profitable_days():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE hall_day_machine(report_date TEXT,hall_name TEXT,machine_name TEXT,avg_diff_coins INTEGER)")
    c.executemany("INSERT INTO hall_day_machine VALUES (?,?,?,?)", [
        ("2026-08-31", "キコーナ四條畷店", "L北斗", -100),
        ("2026-08-30", "キコーナ四條畷店", "L北斗", 9999),
        ("2026-09-01", "キコーナ四條畷店", "L北斗", None),
        ("2026-09-07", "マルハン松本店", "L北斗", 500),
        ("2026-09-07", "キコーナ四條畷店", "マイジャグラーV", 500),
        ("2026-09-08", "キコーナ四條畷店", "L北斗", 500),
    ])
    dates, actuals = replay._load_actuals(c, date(2026, 9, 8))
    assert dates == [f"2026-08-{d}" for d in range(25, 32)]
    assert len(actuals["machine"]) == 2
    c.close()
