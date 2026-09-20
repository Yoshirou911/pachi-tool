"""API スモークテスト。

FastAPI の TestClient を直接使うため、事前にサーバーを起動しておく必要はない
（旧バージョンは requests で localhost:8000 を叩く方式で、サーバーを手動起動しない限り
pytest 実行時に必ず ConnectionError で失敗していた）。

sessions テーブルは tmp_path 上の DB に差し替えて、実データ(data/sessions.db)を
汚さないようにしている。machines/ 配下の理論値データと hall/prior.py の
定数（DAITO_MACHINE_SCORES 等）は読み取り専用なのでそのまま利用する。
"""
import asyncio
import sys

import pytest
from fastapi.testclient import TestClient

from records import models as records_models
from api import main as api_main

app = api_main.app

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


def test_packaged_smoke_test_skips_background_collection(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["PACHI TOOL.exe", "--smoke-test"])

    def unexpected_scheduler_start():
        raise AssertionError("smoke test must not start scheduled collection")

    monkeypatch.setattr(api_main.scheduler, "_start_scrape_scheduler", unexpected_scheduler_start)

    async def enter_lifespan():
        async with api_main._lifespan(app):
            pass

    asyncio.run(enter_lifespan())


def test_machines():
    r = client.get("/api/machines")
    assert r.status_code == 200
    machines = r.json()
    assert len(machines) > 0
    assert not any("ジャグラー" in machine for machine in machines)


def test_smartslot_machine_scope():
    r = client.get("/api/machines?scope=smartslot")
    assert r.status_code == 200
    machines = r.json()
    assert "スマスロ北斗の拳" in machines
    assert "パチスロ甲鉄城のカバネリ" not in machines
    verified = client.get("/api/machines?scope=live_setting").json()
    assert {"スマスロ北斗の拳", "スマスロモンキーターン5", "スマスロ東京喰種", "スマスロかぐや様は告らせたい"} <= set(verified)


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
    assert data["confidence_scope"].endswith("的中率ではありません）")
    assert data["prediction_grade"] in {"統計モデル90%級", "統計モデル80%級", "判定材料不足"}
    assert 0 <= data["sample_adequacy_pct"] <= 100
    assert data["action"] in {"続行候補", "様子見", "撤退候補", "情報不足"}
    assert set(data["high_setting_probabilities"]) == {"setting4_or_higher", "setting5_or_higher", "setting6"}
    assert "profile_verified" in data


def test_estimate_accepts_per_element_trials():
    r = client.post("/api/estimate", json={
        "machine_name": "スマスロ モンキーターン5",
        "games_total": 3000,
        "element_counts": {"水神突入率": 8},
        "element_trials": {"水神突入率": 10},
    })
    assert r.status_code == 200, r.text
    analysis = {row["name"]: row for row in r.json()["element_analysis"]}
    assert analysis["水神突入率"]["trials"] == 10
    assert analysis["水神突入率"]["count"] == 8


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
    assert s["outcome_labels"]["player_profit"]["status"] == "negative"
    assert s["outcome_labels"]["machine_diff"]["status"] == "unknown"
    assert s["outcome_labels"]["high_setting"]["confirmed"] is False
    # delete
    r = client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 200


def test_sessions_export():
    r = client.get("/api/sessions/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")


def test_session_outcome_labels_are_kept_separate():
    r = client.post("/api/sessions", json={
        "machine_name": "ラベル検証機",
        "investment": 20000,
        "returns": 10000,
        "diff_coins": 1200,
        "expected_value_yen": 3500,
        "expected_value_source": "着席前予測",
        "minimum_confirmed_setting": 4,
        "setting_evidence_note": "設定4以上確定画面",
        "prediction_snapshot": {"high_setting_probability_pct": 62},
    })
    assert r.status_code == 200, r.text
    session = client.get(f"/api/sessions/{r.json()['id']}").json()
    labels = session["outcome_labels"]
    assert labels["player_profit"]["status"] == "negative"
    assert labels["machine_diff"]["status"] == "positive"
    assert labels["expected_value"]["status"] == "positive"
    assert labels["high_setting"]["status"] == "confirmed_minimum"
    assert labels["high_setting"]["confirmed"] is True
    assert session["prediction_snapshot"]["high_setting_probability_pct"] == 62

    summary = client.get("/api/sessions/label_summary").json()
    assert summary["labels"]["player_profit"]["count"] == 1
    assert summary["labels"]["machine_diff"]["count"] == 1
    assert summary["labels"]["expected_value"]["count"] == 1
    assert summary["labels"]["setting_evidence"]["confirmed_count"] == 1


def test_zero_results_can_be_recorded_without_becoming_unknown():
    r = client.post("/api/sessions", json={
        "machine_name": "引き分け検証機",
        "investment": 0,
        "returns": 0,
        "diff_coins": 0,
    })
    session = client.get(f"/api/sessions/{r.json()['id']}").json()
    assert session["outcome_labels"]["player_profit"]["status"] == "break_even"
    assert session["outcome_labels"]["machine_diff"]["status"] == "break_even"


def test_inconsistent_confirmed_settings_are_rejected():
    r = client.post("/api/sessions", json={
        "machine_name": "矛盾検証機",
        "confirmed_setting": 3,
        "minimum_confirmed_setting": 4,
    })
    assert r.status_code == 422


def test_setting_truth_can_be_corrected_without_leaving_stale_confirmation():
    created = client.post("/api/sessions", json={
        "machine_name": "訂正検証機",
        "minimum_confirmed_setting": 4,
        "setting_evidence_level": "confirmed_minimum",
    })
    sid = created.json()["id"]
    updated = client.put(f"/api/sessions/{sid}", json={
        "setting_evidence_level": "strong_hint",
        "setting_evidence_note": "確定ではなく強い示唆へ訂正",
    })
    assert updated.status_code == 200, updated.text
    label = updated.json()["outcome_labels"]["high_setting"]
    assert label["status"] == "strong_hint"
    assert label["confirmed"] is False
    assert label["minimum_confirmed_setting"] is None
