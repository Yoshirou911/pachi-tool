import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from opportunity import models
from api.main import app


@pytest.fixture()
def opportunity_db(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", tmp_path / "opportunities.db")
    models.init_db()
    return tmp_path / "opportunities.db"


def _profile(**overrides):
    data = {
        "machine_name": "テストAT機",
        "condition_label": "通常・等価",
        "exchange_type": "equivalent",
        "funding_mode": "any",
        "reset_status": "normal",
        "metric_name": "現在ゲーム数",
        "unit_label": "G",
        "start_threshold": 500,
        "ceiling_threshold": 1000,
        "expected_value_yen": 2500,
        "estimated_play_minutes": 60,
        "worst_case_investment_yen": 20000,
        "stop_rule": "AT終了後、前兆を確認して終了",
        "source_name": "検証済み資料",
        "source_url": "https://example.com/spec",
        "verified_on": "2026-08-08",
        "confidence": "verified",
        "notes": "",
    }
    data.update(overrides)
    return models.save_profile(data)


def test_verified_candidate_becomes_target(opportunity_db):
    models.save_budget("2026-08", starting_bankroll=100000, loss_limit_yen=30000)
    profile = _profile()
    models.save_candidate({
        "machine_name": "テストAT機",
        "current_value": 600,
        "profile_id": profile["id"],
    })

    dashboard = models.get_dashboard("2026-08")
    candidate = dashboard["candidates"][0]
    assert candidate["judgment"] == "target"
    assert candidate["actionable"] is True
    assert dashboard["summary"]["risk_capacity_yen"] == 30000


def test_candidate_uses_conservative_curve_step(opportunity_db):
    models.save_budget("2026-08", starting_bankroll=100000, loss_limit_yen=30000)
    profile = _profile(curve_points=[
        {"value": 500, "ev_yen": 1200, "worst_case_yen": 24000},
        {"value": 600, "ev_yen": 2300, "worst_case_yen": 19000},
        {"value": 700, "ev_yen": 3600, "worst_case_yen": 14000},
    ])
    models.save_candidate({
        "machine_name": "テストAT機", "current_value": 650, "profile_id": profile["id"],
    })

    candidate = models.get_dashboard("2026-08")["candidates"][0]
    assert candidate["judgment"] == "target"
    assert candidate["expected_value_yen"] == 2300
    assert candidate["worst_case_investment_yen"] == 19000
    assert candidate["matched_curve_value"] == 600
    assert "curve_json" not in candidate


def test_quick_decision_checks_conditions_and_closing_time(opportunity_db):
    profile = _profile(funding_mode="cash", estimated_play_minutes=60)

    target = models.assess_quick_decision(
        profile, current_value=650, risk_capacity_yen=30000,
        exchange_type="equivalent", funding_mode="cash", reset_status="normal",
        minutes_until_close=120,
    )
    assert target["judgment"] == "target"
    assert target["actionable"] is True
    assert target["required_minutes_with_buffer"] == 90

    wrong_funding = models.assess_quick_decision(
        profile, current_value=650, risk_capacity_yen=30000,
        exchange_type="equivalent", funding_mode="medals", reset_status="normal",
        minutes_until_close=120,
    )
    assert wrong_funding["judgment"] == "condition_mismatch"
    assert wrong_funding["actionable"] is False

    closing_risk = models.assess_quick_decision(
        profile, current_value=650, risk_capacity_yen=30000,
        exchange_type="equivalent", funding_mode="cash", reset_status="normal",
        minutes_until_close=80,
    )
    assert closing_risk["judgment"] == "closing_risk"
    assert closing_risk["actionable"] is False


def test_quick_decision_uses_safe_limit_when_duration_unknown(opportunity_db):
    profile = _profile(estimated_play_minutes=None)
    safe = models.assess_quick_decision(
        profile, 650, 30000, "equivalent", "cash", "normal", 180,
    )
    assert safe["judgment"] == "target"
    assert safe["required_minutes_with_buffer"] == 120
    assert safe["warnings"]

    late = models.assess_quick_decision(
        profile, 650, 30000, "equivalent", "cash", "normal", 100,
    )
    assert late["judgment"] == "closing_risk"


def test_seed_catalog_updates_without_duplicates(opportunity_db, tmp_path):
    catalog_path = tmp_path / "catalog.json"
    catalog = {
        "profiles": [{
            "catalog_key": "sample-v1",
            "machine_name": "カタログ機",
            "condition_label": "通常・等価",
            "metric_name": "AT間ゲーム数",
            "unit_label": "G",
            "start_threshold": 500,
            "expected_value_yen": 1000,
            "worst_case_investment_yen": 20000,
            "stop_rule": "前兆確認後に終了",
            "source_name": "検証資料",
            "source_url": "https://example.com/catalog",
            "source_urls": ["https://example.com/catalog"],
            "verified_on": "2026-08-08",
            "confidence": "verified",
            "curve_points": [{"value": 500, "ev_yen": 1000}],
        }],
    }
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    assert models.seed_catalog(catalog_path) == 1
    catalog["profiles"][0]["expected_value_yen"] = 1500
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    assert models.seed_catalog(catalog_path) == 1

    profiles = models.list_profiles("カタログ機")
    assert len(profiles) == 1
    assert profiles[0]["expected_value_yen"] == 1500
    assert profiles[0]["curve_points"][0]["ev_yen"] == 1000


def test_candidate_waits_below_start_line(opportunity_db):
    models.save_budget("2026-08", starting_bankroll=100000, loss_limit_yen=30000)
    profile = _profile()
    models.save_candidate({
        "machine_name": "テストAT機", "current_value": 420, "profile_id": profile["id"],
    })
    candidate = models.get_dashboard("2026-08")["candidates"][0]
    assert candidate["judgment"] == "wait"
    assert "80G" in candidate["reason"]


def test_unverified_or_unaffordable_rule_is_not_actionable(opportunity_db):
    models.save_budget("2026-08", starting_bankroll=100000, loss_limit_yen=10000)
    reference = _profile(machine_name="参考機", confidence="reference")
    expensive = _profile(machine_name="高額機", worst_case_investment_yen=20000)
    models.save_candidate({"machine_name": "参考機", "current_value": 600, "profile_id": reference["id"]})
    models.save_candidate({"machine_name": "高額機", "current_value": 600, "profile_id": expensive["id"]})

    judgments = {item["machine_name"]: item["judgment"] for item in models.get_dashboard("2026-08")["candidates"]}
    assert judgments == {"参考機": "verify", "高額機": "insufficient_funds"}


def test_result_updates_budget_and_closes_candidate(opportunity_db):
    models.save_budget("2026-08", starting_bankroll=80000, loss_limit_yen=30000)
    profile = _profile()
    candidate = models.save_candidate({
        "machine_name": "テストAT機", "current_value": 650, "profile_id": profile["id"],
    })
    models.save_result(candidate["id"], {
        "played_on": "2026-08-08", "investment_yen": 18000,
        "returns_yen": 5000, "played_minutes": 75, "notes": "",
    })

    dashboard = models.get_dashboard("2026-08")
    assert dashboard["candidates"] == []
    assert dashboard["summary"]["net_profit_yen"] == -13000
    assert dashboard["summary"]["remaining_loss_yen"] == 17000
    assert dashboard["summary"]["current_bankroll"] == 67000
    assert dashboard["recent_results"][0]["net_profit_yen"] == -13000

    with pytest.raises(sqlite3.IntegrityError):
        models.save_result(candidate["id"], {
            "played_on": "2026-08-08", "investment_yen": 0,
            "returns_yen": 0, "played_minutes": 0, "notes": "",
        })


def test_opportunity_api_flow(opportunity_db):
    client = TestClient(app)
    budget = client.put("/api/opportunity/budget", json={
        "month": "2026-08", "starting_bankroll": 100000, "loss_limit_yen": 30000,
    })
    assert budget.status_code == 200, budget.text

    profile_response = client.post("/api/opportunity/profiles", json={
        "machine_name": "APIテスト機", "metric_name": "現在ゲーム数", "unit_label": "G",
        "condition_label": "通常・等価", "exchange_type": "equivalent",
        "funding_mode": "any", "reset_status": "normal",
        "start_threshold": 500, "ceiling_threshold": 1000,
        "expected_value_yen": 2000, "worst_case_investment_yen": 20000,
        "estimated_play_minutes": 60,
        "stop_rule": "前兆確認後に終了", "source_name": "検証資料",
        "verified_on": "2026-08-08", "confidence": "verified",
    })
    assert profile_response.status_code == 200, profile_response.text
    profile_id = profile_response.json()["id"]

    candidate_response = client.post("/api/opportunity/candidates", json={
        "machine_name": "APIテスト機", "current_value": 620, "profile_id": profile_id,
    })
    assert candidate_response.status_code == 200, candidate_response.text

    dashboard = client.get("/api/opportunity/dashboard", params={"month": "2026-08"})
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["candidates"][0]["judgment"] == "target"

    quick = client.post("/api/opportunity/quick-assess", json={
        "month": "2026-08", "machine_name": "APIテスト機", "profile_id": profile_id,
        "current_value": 650, "exchange_type": "equivalent", "funding_mode": "cash",
        "reset_status": "normal", "minutes_until_close": 180,
    })
    assert quick.status_code == 200, quick.text
    assert quick.json()["judgment"] == "target"
    assert quick.json()["risk_capacity_yen"] == 30000

    mismatch = client.post("/api/opportunity/candidates", json={
        "machine_name": "別機種", "current_value": 620, "profile_id": profile_id,
    })
    assert mismatch.status_code == 422
