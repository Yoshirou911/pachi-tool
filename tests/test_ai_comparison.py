from datetime import date, timedelta
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.ai_comparison import comparison_status, run_comparison
from api.ai_provider import AICompletion
from api.ai_governance import AIGovernanceService
from api.routers import ai as ai_router


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "hall.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
          CREATE TABLE hall_day_machine (hall_name,machine_name,report_date,avg_diff_coins,source_url,scraped_at);
          CREATE TABLE hall_day_seat (hall_name,machine_name,seat_number,report_date,diff_coins,source_url,scraped_at);
          CREATE TABLE hall_layout (id,hall_name,floor_name,valid_from,valid_to,verification_status,source_kind,source_url,updated_at);
          CREATE TABLE hall_layout_seat (layout_id,seat_number,machine_name,island_name,row_name,row_order,x,y,rotation);
        """)
        for offset in range(21):
            day = (date(2026, 8, 20) + timedelta(days=offset)).isoformat()
            conn.execute("INSERT INTO hall_day_machine VALUES(?,?,?,?,?,?)",
                         ("店舗A", "機種A", day, 100 + offset, "https://example.com/a", day))
            conn.execute("INSERT INTO hall_day_machine VALUES(?,?,?,?,?,?)",
                         ("店舗A", "機種B", day, -50, "https://example.com/b", day))
            conn.execute("INSERT INTO hall_day_seat VALUES(?,?,?,?,?,?,?)",
                         ("店舗A", "機種A", 101, day, offset, "https://example.com/seat", day))
    return path


ENV = {
    "PACHI_AI_ALLOW_EXTERNAL": "true", "PACHI_AI_ALLOW_COMPARISON_EXTERNAL": "true",
    "DASHSCOPE_API_KEY": "qwen-secret", "ANTHROPIC_API_KEY": "claude-secret",
    "PACHI_AI_QWEN_INPUT_USD_PER_MTOK": "1", "PACHI_AI_QWEN_OUTPUT_USD_PER_MTOK": "2",
    "PACHI_AI_CLAUDE_INPUT_USD_PER_MTOK": "3", "PACHI_AI_CLAUDE_OUTPUT_USD_PER_MTOK": "4",
    "PACHI_AI_MONTHLY_LIMIT_USD": "1",
}


@pytest.fixture
def governance(tmp_path):
    return AIGovernanceService(tmp_path / "usage.db")


class BenchmarkClient:
    calls = []
    def __init__(self, config):
        self.config = config
    def complete_detailed(self, messages, *, max_tokens):
        BenchmarkClient.calls.append((self.config.definition.name, messages))
        snapshot = json.loads(messages[-1]["content"])["snapshot"]
        ids = [item["id"] for item in snapshot["evidence"][:8]]
        raw = json.dumps({"snapshot_id": snapshot["snapshot_id"],
                          "claims": [{"evidence_id": item} for item in ids]})
        if self.config.definition.name == "claude":
            raw = '{"summary":"勝てる"}'
        return AICompletion(raw, 100, 20)


def test_status_does_not_expose_keys_or_enable_calls_by_default():
    status = comparison_status({"DASHSCOPE_API_KEY": "never-show"})
    assert status["external_comparison_enabled"] is False
    assert status["personal_history_sent"] is False
    assert "never-show" not in json.dumps(status)
    assert len(status["cases"]) == 4


def test_preview_freezes_same_cases_without_calling_any_provider(db):
    class Never:
        def __init__(self, _config):
            pytest.fail("preview must not construct a client")
    result = run_comparison(hall_name="店舗A", target_date="2026-09-14", days=60,
        provider_names=["qwen", "claude"], case_ids=["machine", "weekday"],
        mode="preview", confirm_external=False, environ={}, db_path=db, client_factory=Never)
    assert result["external_calls_planned"] == 4
    assert all(item["status"] == "not_run" for item in result["provider_results"])
    assert all(item["evidence_count"] > 0 for item in result["case_definitions"])
    assert result["winner"] is None and result["changes_live_prediction"] is False


@pytest.mark.parametrize("confirm,env,reason", [
    (False, ENV, "実測の確認"),
    (True, {**ENV, "PACHI_AI_ALLOW_COMPARISON_EXTERNAL": "false"}, "サーバー側"),
])
def test_external_run_needs_request_confirmation_and_server_switch(db, governance, confirm, env, reason):
    with pytest.raises(ValueError, match=reason):
        run_comparison(hall_name="店舗A", target_date="2026-09-14", days=60,
            provider_names=["qwen"], case_ids=["machine"], mode="external",
            confirm_external=confirm, confirm_public_data_only=True, request_id="gates",
            governance_service=governance, environ=env, db_path=db)
    assert not governance.db_path.exists()


@pytest.mark.parametrize("changes,reason", [
    ({"governance_service": None}, "費用・送信管理"),
    ({"request_id": None}, "実行ID"),
    ({"confirm_public_data_only": False}, "公開統計・固定質問"),
])
def test_external_comparison_governance_requirements_cannot_be_bypassed(db, governance, changes, reason):
    def never_create(_config):
        pytest.fail("a rejected comparison must not construct a client")
    request = dict(hall_name="店舗A", target_date="2026-09-14", days=60,
        provider_names=["qwen"], case_ids=["machine"], mode="external", confirm_external=True,
        confirm_public_data_only=True, request_id="requirements", governance_service=governance,
        environ=ENV, db_path=db, client_factory=never_create)
    with pytest.raises(ValueError, match=reason):
        run_comparison(**{**request, **changes})
    assert not governance.db_path.exists()


def test_external_run_uses_identical_snapshots_and_rejects_provider_prose(db, governance):
    BenchmarkClient.calls = []
    result = run_comparison(hall_name="店舗A", target_date="2026-09-14", days=60,
        provider_names=["qwen", "claude"], case_ids=["machine", "weekday"], mode="external",
        confirm_external=True, confirm_public_data_only=True, request_id="identical",
        governance_service=governance, environ=ENV, db_path=db, client_factory=BenchmarkClient)
    qwen, claude = result["provider_results"]
    assert qwen["summary"]["contract_pass_rate_pct"] == 100
    assert qwen["summary"]["evidence_match_pct"] == 100
    assert qwen["summary"]["estimated_cost_usd"] == pytest.approx(0.00028)
    assert claude["summary"]["contract_pass_rate_pct"] == 0
    assert all(item["error_kind"] == "contract_rejected" for item in claude["cases"])
    assert "勝てる" not in json.dumps(result, ensure_ascii=False)
    assert result["raw_provider_answers_stored"] is False
    qwen_snaps = [json.loads(call[1][-1]["content"])["snapshot"]["snapshot_id"] for call in BenchmarkClient.calls if call[0] == "qwen"]
    claude_snaps = [json.loads(call[1][-1]["content"])["snapshot"]["snapshot_id"] for call in BenchmarkClient.calls if call[0] == "claude"]
    assert qwen_snaps == claude_snaps


def test_unconfigured_provider_is_not_called(db, governance):
    BenchmarkClient.calls = []
    result = run_comparison(hall_name="店舗A", target_date="2026-09-14", days=60,
        provider_names=["deepseek"], case_ids=["machine"], mode="external",
        confirm_external=True, confirm_public_data_only=True, request_id="unavailable",
        governance_service=governance, environ=ENV, db_path=db, client_factory=BenchmarkClient)
    assert result["provider_results"][0]["status"] == "unavailable"
    assert BenchmarkClient.calls == []


def test_governance_preflights_all_providers_before_any_paid_call(db, tmp_path):
    BenchmarkClient.calls = []
    service = AIGovernanceService(tmp_path / "usage.db")
    env = {**{key: value for key, value in ENV.items() if "CLAUDE_" not in key},
        "PACHI_AI_QWEN_MONTHLY_LIMIT_USD": "1",
        "PACHI_AI_CLAUDE_MONTHLY_LIMIT_USD": "1",
        # Claude prices deliberately absent: the whole comparison must stop first.
    }
    with pytest.raises(ValueError, match="Claude.*単価"):
        run_comparison(hall_name="店舗A", target_date="2026-09-14", days=60,
            provider_names=["qwen", "claude"], case_ids=["machine"], mode="external",
            confirm_external=True, confirm_public_data_only=True, request_id="one",
            governance_service=service, environ=env, db_path=db, client_factory=BenchmarkClient)
    assert BenchmarkClient.calls == []
    assert not service.db_path.exists()


@pytest.mark.parametrize("abstain", [False, True])
def test_comparison_distinguishes_valid_schema_from_wrong_evidence_and_abstention(db, governance, monkeypatch, abstain):
    from api import ai_comparison
    from api.ai_evidence import evidence, freeze_evidence, metric
    snapshot = freeze_evidence([evidence(hall="店舗B", target="2026-09-14",
        start="2026-09-01", end="2026-09-13", metrics=[metric("平均差枚", 100, "枚")])],
        target_date="2026-09-14", scope="店舗A", constraints={"hall_name": "店舗A"})
    monkeypatch.setattr(ai_comparison, "_case_snapshots", lambda *args, **kwargs: [
        {"case_id": "machine", "label": "機種", "question": "機種は？", "snapshot": snapshot}])

    class Client(BenchmarkClient):
        def complete_detailed(self, messages, *, max_tokens):
            return AICompletion(json.dumps({"snapshot_id": snapshot["snapshot_id"],
                "claims": [] if abstain else [{"evidence_id": "E001"}]}), 100, 20)

    result = run_comparison(hall_name="店舗A", target_date="2026-09-14", days=60,
        provider_names=["qwen"], case_ids=["machine"], mode="external",
        confirm_external=True, confirm_public_data_only=True, request_id="selection",
        governance_service=governance, environ=ENV, db_path=db, client_factory=Client)["provider_results"][0]
    assert result["cases"][0]["status"] == ("abstained" if abstain else "rejected")
    assert result["summary"]["contract_pass_rate_pct"] == 100
    assert result["summary"]["guard_rejected_cases"] == (0 if abstain else 1)
    assert result["summary"]["abstained_cases"] == int(abstain)
    assert result["summary"]["estimated_cost_usd"] == pytest.approx(0.00014)
    assert result["summary"]["evidence_match_pct"] is None


def test_api_defaults_to_preview_and_validates_limits(db, monkeypatch):
    monkeypatch.setattr(ai_router, "HALL_REPORTS_DB", db)
    app = FastAPI()
    app.include_router(ai_router.router)
    body = {"hall_name": "店舗A", "visit_date": "2026-09-14",
            "providers": ["qwen"], "case_ids": ["machine"]}
    with TestClient(app) as client:
        result = client.post("/api/ai/comparison/run", json=body)
        assert result.status_code == 200 and result.json()["mode"] == "preview"
        assert client.post("/api/ai/comparison/run", json={**body, "hall_name": "全店舗"}).status_code == 422
        assert client.post("/api/ai/comparison/run", json={**body, "providers": ["unknown"]}).status_code == 422
        assert client.post("/api/ai/comparison/run", json={**body, "case_ids": ["machine", "weekday", "digit", "seat"]}).status_code == 422
