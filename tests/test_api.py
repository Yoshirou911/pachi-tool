"""API スモークテスト。

FastAPI の TestClient を直接使うため、事前にサーバーを起動しておく必要はない
（旧バージョンは requests で localhost:8000 を叩く方式で、サーバーを手動起動しない限り
pytest 実行時に必ず ConnectionError で失敗していた）。

sessions テーブルは tmp_path 上の DB に差し替えて、実データ(data/sessions.db)を
汚さないようにしている。machines/ 配下の理論値データと hall/prior.py の
定数（DAITO_MACHINE_SCORES 等）は読み取り専用なのでそのまま利用する。
"""
import pytest
from fastapi.testclient import TestClient

from records import models as records_models
from api.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_sessions_db(tmp_path, monkeypatch):
    monkeypatch.setattr(records_models, "DB_PATH", tmp_path / "sessions.db")
    records_models.init_db()


def _get_first_machine() -> str:
    r = client.get("/api/machines")
    r.raise_for_status()
    machines = r.json()
    assert len(machines) > 0
    return machines[0]


def test_machines():
    r = client.get("/api/machines")
    assert r.status_code == 200
    machines = r.json()
    assert len(machines) > 0
    assert not any("ジャグラー" in machine for machine in machines)


def test_mobile_slot_app_is_served():
    r = client.get("/mobile/")
    assert r.status_code == 200
    assert "ジャグラー設定狙い" in r.text


def test_stats_available_on_fresh_database():
    r = client.get("/api/stats")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_estimate():
    machine_name = _get_first_machine()
    r = client.post("/api/estimate", json={
        "machine_name": machine_name,
        "games_total": 3000,
        "element_counts": {},
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert "posterior" in data
    assert data["ev_pct"] > 0
    assert "confidence" in data
    assert "confidence_label" in data


def test_estimate_with_hall():
    machine_name = _get_first_machine()
    r = client.post("/api/estimate", json={
        "machine_name": machine_name,
        "games_total": 3000,
        "hall_name": "ベガスベガス大東店",
        "weekday": 6,
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert "ev_pct" in data


def test_estimate_started_from():
    """宵越し補正: started_from=1000 で観測G数が2000になるか確認"""
    machine_name = _get_first_machine()
    r1 = client.post("/api/estimate", json={
        "machine_name": machine_name,
        "games_total": 2000,
        "element_counts": {},
    })
    r2 = client.post("/api/estimate", json={
        "machine_name": machine_name,
        "games_total": 3000,
        "started_from": 1000,
        "element_counts": {},
    })
    assert r1.status_code == 200
    assert r2.status_code == 200
    # 両者は同じ観測G数(2000)なので後験は同一になるはず
    p1 = r1.json()["posterior"]
    p2 = r2.json()["posterior"]
    for s in p1:
        assert abs(p1[s] - p2[s]) < 1e-9, f"setting {s}: {p1[s]} != {p2[s]}"


def test_daito():
    r = client.get("/api/hall/daito")
    assert r.status_code == 200
    data = r.json()
    top = data["machine_scores"][0]
    assert "machine" in top
    assert "score" in top


def test_sessions():
    # create
    r = client.post("/api/sessions", json={
        "machine_name": "テスト機",
        "hall_name": "テストホール",
        "games_total": 1000,
        "investment": 5000,
        "returns": 4000,
    })
    assert r.status_code == 200
    sid = r.json()["id"]
    # get
    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    s = r.json()
    assert s["machine_name"] == "テスト機"
    # delete
    r = client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 200


def test_sessions_export():
    r = client.get("/api/sessions/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
