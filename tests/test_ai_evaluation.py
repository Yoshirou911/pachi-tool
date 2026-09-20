from copy import deepcopy
import json
import sqlite3
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.ai_evaluation import EvaluationConflict, EvaluationService, SUITE_PATH, encoded, load_suite, score_response, summarize
from api.ai_evidence import validate_answer
from api.ai_governance import AIGovernanceService
from api.ai_provider import AICompletion, AIProviderError
from api.routers import ai as router


ENV = {"PACHI_AI_ALLOW_EXTERNAL": "true", "PACHI_AI_ALLOW_COMPARISON_EXTERNAL": "true",
       "DASHSCOPE_API_KEY": "secret-never-persist", "PACHI_AI_QWEN_INPUT_USD_PER_MTOK": "1",
       "PACHI_AI_QWEN_OUTPUT_USD_PER_MTOK": "2", "PACHI_AI_QWEN_MONTHLY_LIMIT_USD": "1"}


def response(case, ids=None):
    return encoded({"snapshot_id": case["snapshot"]["snapshot_id"],
                    "claims": [{"evidence_id": i} for i in (case["expected_ids"] if ids is None else ids)]})


def request(**changes):
    return {"request_id": str(uuid4()), "suite_hash": load_suite()["suite_hash"], **changes}


def test_fixture_inputs_are_stable_and_never_include_answers_or_future_data():
    first, second = load_suite(), load_suite()
    assert first == second
    assert len(first["cases"]) == 11
    case = next(c for c in first["cases"] if c["id"] == "future")
    assert case["excluded_inputs"] == ["future_win", "late_revision"]
    assert case["outcome"]["diff_coins"] == 1800
    for case in first["cases"]:
        text = encoded(case["messages"])
        assert all(word not in text for word in ("expected_ids", "forbidden_ids", "outcome", "後日改訂", "終了後の結果"))
        assert all(m["value"] not in (1800, 999) for f in case["snapshot"]["evidence"] for m in f["metrics"])
        assert case["snapshot"]["generated_at"] == first["cutoff"]


def test_gold_is_not_the_first_evidence_and_unnecessary_ids_are_penalized():
    case = load_suite()["cases"][0]
    assert case["expected_ids"] == ["E003"]
    assert score_response(case, response(case))["exact_match"]
    all_ids = [f["id"] for f in case["snapshot"]["evidence"]]
    scored = score_response(case, response(case, all_ids))
    assert scored["schema_valid"] and not scored["exact_match"]
    assert scored["precision_pct"] == 33.3 and scored["recall_pct"] == 100
    assert scored["forbidden_ids"] == ["E001", "E002"]


@pytest.mark.parametrize("id", ["missing_hall", "setting", "layout", "future", "empty"])
def test_abstention_is_scored_but_production_validator_stays_strict(id):
    case = next(c for c in load_suite()["cases"] if c["id"] == id)
    scored = score_response(case, response(case, []))
    assert scored["schema_valid"] and scored["abstained"] and scored["exact_match"]
    with pytest.raises(ValueError):
        validate_answer(response(case, []), case["snapshot"])


@pytest.mark.parametrize("raw", [
    "勝率100%", '{"summary":"架空の台番号777"}', '[]',
    '{"snapshot_id":"wrong","claims":[]}', '{"claims":[],"claims":[]}',
])
def test_invalid_text_never_passes_as_abstention(raw):
    case = next(c for c in load_suite()["cases"] if c["id"] == "empty")
    result = score_response(case, raw)
    assert not result["schema_valid"] and not result["exact_match"] and not result["abstained"]
    assert "勝率" not in encoded(result) and "架空の台番号777" not in encoded(result)


def test_mutated_suite_and_later_labels_change_identity_not_old_scores(tmp_path):
    service = EvaluationService(tmp_path / "eval.db")
    result = service.start(**request(), launch=lambda work: work())
    before = service.report(result["id"])
    raw = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    raw["cases"][0]["expected_keys"] = ["machine_b"]
    raw["cases"][0]["forbidden_keys"] = ["machine_a", "other_store"]
    updated = tmp_path / "suite.json"
    updated.write_text(encoded(raw), encoding="utf-8")
    service.suite_path = updated
    assert load_suite(updated)["suite_hash"] != before["suite_hash"]
    assert service.report(result["id"]) == before
    with pytest.raises(EvaluationConflict):
        service.start(**request(), launch=lambda work: work())


def test_read_only_catalog_has_no_side_effect_and_offline_never_uses_provider(tmp_path):
    def unexpected(_config):
        pytest.fail("offline must not create a provider")
    service = EvaluationService(tmp_path / "eval.db", client_factory=unexpected)
    assert service.catalog()["runs"] == []
    assert not service.db_path.exists()
    result = service.start(**request(), environ=ENV, launch=lambda work: work())
    assert result["status"] == "completed" and result["progress_pct"] == 100
    s = result["summary"]
    assert s["total"] == s["completed"] == 22
    assert s["external_calls"] == 0 and s["estimated_cost_usd"] is None
    assert s["exact_count"] == 4 and s["exact_match_pct"] == 18.2
    assert s["consistency_pct"] == 100 and s["consistency_pairs"] == s["planned_pairs"] == 11
    assert "AI未使用" in result["label"]
    with sqlite3.connect(service.db_path) as conn:
        for statement in ("DELETE FROM eval_runs", "UPDATE eval_samples SET attempt=0", "DELETE FROM eval_finishes"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)


def test_idempotency_concurrency_and_restart_do_not_repeat_calls(tmp_path):
    service = EvaluationService(tmp_path / "eval.db")
    queued = []
    req = request()
    first = service.start(**req, launch=queued.append)
    assert first["status"] == "running"
    again = service.start(**req, launch=queued.append)
    assert again["id"] == first["id"] and len(queued) == 1
    with pytest.raises(EvaluationConflict):
        service.start(**request(), launch=queued.append)
    with pytest.raises(EvaluationConflict):
        service.start(**{**req, "repeats": 3}, launch=queued.append)
    restarted = EvaluationService(service.db_path)
    resumed = restarted.start(**req, launch=queued.append)
    assert resumed["status"] == "interrupted" and len(queued) == 1
    queued[0]()
    complete = service.start(**req, launch=queued.append)
    assert complete["status"] == "completed" and len(queued) == 1


@pytest.mark.parametrize("changes,env,reason", [
    ({"confirm_external": False}, ENV, "実行確認"),
    ({"confirm_public_data_only": False}, ENV, "固定評価問題"),
    ({}, {**ENV, "PACHI_AI_ALLOW_EXTERNAL": "false"}, "比較許可"),
    ({}, {**ENV, "PACHI_AI_ALLOW_COMPARISON_EXTERNAL": "false"}, "比較許可"),
    ({}, {**ENV, "DASHSCOPE_API_KEY": ""}, "設定"),
    ({}, {**ENV, "PACHI_AI_QWEN_INPUT_USD_PER_MTOK": ""}, "単価"),
    ({}, {**ENV, "PACHI_AI_QWEN_MONTHLY_LIMIT_USD": ""}, "月上限"),
])
def test_external_gates_before_recording_or_transport(tmp_path, changes, env, reason):
    def unexpected(_config):
        pytest.fail("rejected requests must not create a provider")
    governance = AIGovernanceService(tmp_path / "usage.db")
    service = EvaluationService(tmp_path / "eval.db", client_factory=unexpected,
                                governance_service=governance)
    req = request(mode="external", provider="qwen", confirm_external=True, confirm_public_data_only=True)
    with pytest.raises(ValueError, match=reason):
        service.start(**{**req, **changes}, environ=env)
    assert not service.db_path.exists()
    assert not governance.db_path.exists()


def test_external_mock_usage_preserves_rejected_answer_cost_and_unknowns(tmp_path):
    calls = []
    suite = load_suite()
    lookup = {c["question"]: c for c in suite["cases"]}
    class Client:
        def __init__(self, config):
            assert config.allow_personal_history is False
        def complete_detailed(self, messages, *, max_tokens):
            calls.append(deepcopy(messages))
            context = json.loads(messages[1]["content"])
            case = lookup[context["question"]]
            if case["id"] == "machine":
                return AICompletion('secret-never-persist 勝率100%', 100, 20)
            if case["id"] == "empty":
                raise AIProviderError("timeout", "secret-never-persist")
            return AICompletion(response(case), 0, 0)
    governance = AIGovernanceService(tmp_path / "usage.db")
    service = EvaluationService(tmp_path / "eval.db", client_factory=Client, governance_service=governance)
    result = service.start(**request(mode="external", provider="qwen", confirm_external=True,
                                   confirm_public_data_only=True),
                           environ=ENV, launch=lambda work: work())
    assert len(calls) == 22 and all(calls[i] == calls[i+1] for i in range(0, 22, 2))
    bad = [s for s in result["samples"] if s["case_id"] == "machine"]
    assert bad[0]["estimated_cost_usd"] == pytest.approx(0.00014)
    assert bad[0]["error_kind"] == "invalid_contract"
    assert result["summary"]["estimated_cost_usd"] is None  # timeout's usage is not known
    assert result["summary"]["exact_count"] == 18
    assert result["summary"]["consistency_pairs"] == 9  # two invalid cases cannot inflate stability
    assert "secret-never-persist" not in service.db_path.read_bytes().decode(errors="ignore")
    assert "secret-never-persist" not in encoded(result)
    assert "secret-never-persist" not in governance.db_path.read_bytes().decode(errors="ignore")


def test_external_evaluation_freezes_prices_and_never_repeats_a_completed_run(tmp_path):
    governance = AIGovernanceService(tmp_path / "usage.db")
    calls = []
    class Client:
        def __init__(self, _config):
            pass
        def complete_detailed(self, messages, *, max_tokens):
            calls.append(deepcopy(messages))
            snap = json.loads(messages[-1]["content"])["snapshot"]
            return AICompletion(encoded({"snapshot_id": snap["snapshot_id"], "claims": []}), 100, 20)
    service = EvaluationService(tmp_path / "eval.db", client_factory=Client, governance_service=governance)
    env, queued = dict(ENV), []
    req = request(mode="external", provider="qwen", repeats=1,
                  confirm_external=True, confirm_public_data_only=True)
    running = service.start(**req, environ=env, launch=queued.append)
    env.update(PACHI_AI_QWEN_INPUT_USD_PER_MTOK="999", PACHI_AI_QWEN_OUTPUT_USD_PER_MTOK="999")
    queued[0]()
    completed = service.report(running["id"])
    assert completed["status"] == "completed" and len(calls) == 11
    assert all(sample["estimated_cost_usd"] == pytest.approx(0.00014) for sample in completed["samples"])
    with sqlite3.connect(governance.db_path) as conn:
        assert conn.execute("SELECT cost_usd FROM ai_usage_calls").fetchall() == [(0.00014,)] * 11
    def never_create(_config):
        pytest.fail("a completed run must not create another client")
    restarted = EvaluationService(service.db_path, client_factory=never_create,
                                  governance_service=AIGovernanceService(governance.db_path))
    assert restarted.start(**req, environ=env, launch=queued.append) == completed
    assert len(queued) == 1 and len(calls) == 11


def test_repeated_wrong_answers_are_consistent_but_not_correct():
    case = load_suite()["cases"][0]
    samples = []
    for i in range(2):
        samples.append({**score_response(case, response(case, ["E001"])), "case_id": case["id"],
            "external_call": True, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0})
    s = summarize([case], samples, 2)
    assert s["exact_match_pct"] == 0 and s["consistency_pct"] == 100
    assert s["input_tokens"] == 0 and s["estimated_cost_usd"] == 0
    samples[1].update(score_response(case, response(case)))
    assert summarize([case], samples, 2)["consistency_pct"] == 0


def test_api_gets_are_read_only_and_default_run_does_not_send(monkeypatch, tmp_path):
    service = EvaluationService(tmp_path / "eval.db")
    real_start = service.start
    monkeypatch.setattr(service, "start", lambda **kw: real_start(**kw, launch=lambda worker: worker()))
    monkeypatch.setattr(router, "evaluations", service)
    app = FastAPI()
    app.include_router(router.router)
    with TestClient(app) as client:
        catalog = client.get("/api/ai/evaluation").json()
        assert not service.db_path.exists()
        body = request()
        for change in ({"repeats": 9}, {"history": ["秘密"]}, {"request_id": "bad"}, {"repeats": True}):
            assert client.post("/api/ai/evaluation/runs", json={**body, **change}).status_code == 422
        response = client.post("/api/ai/evaluation/runs", json=body)
        assert response.status_code == 200
        run_id = response.json()["id"]
        assert client.post("/api/ai/evaluation/runs", json=body).json()["id"] == run_id
        assert client.get("/api/ai/evaluation/runs/" + str(uuid4())).status_code == 404
        assert catalog["suite_hash"] == body["suite_hash"]
