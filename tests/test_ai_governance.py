from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier

import pytest

from api.ai_governance import (
    AIGovernanceError, AIGovernanceService, DuplicateAIRequest, GovernedClient,
    LEDGER_TABLES, monthly_limit, privacy_check,
)
from api.ai_provider import AICompletion, AIProviderError


ENV = {
    "PACHI_AI_QWEN_INPUT_USD_PER_MTOK": "1",
    "PACHI_AI_QWEN_OUTPUT_USD_PER_MTOK": "2",
    "PACHI_AI_QWEN_MONTHLY_LIMIT_USD": "1.5",
}


def messages(text="公開統計だけ"):
    return [{"role": "user", "content": text}]


def authorization(request_id="request", **changes):
    return {"request_id": request_id, "provider": "qwen", "model": "m", "purpose": "test",
            "call_messages": [messages()], "max_tokens": 100, "confirmed": True, **changes}


def test_privacy_guard_blocks_likely_personal_or_secret_text_without_echoing_it():
    result = privacy_check(messages("連絡先 user@example.com api_key=do-not-store"))
    assert result["passed"] is False
    assert result["blocked_categories"] == ["email", "secret"]
    assert "do-not-store" not in json.dumps(result)


def test_unknown_price_or_limit_fails_closed_without_creating_ledger(tmp_path):
    service = AIGovernanceService(tmp_path / "usage.db")
    with pytest.raises(AIGovernanceError, match="単価.*月上限"):
        service.authorize(request_id="a", provider="qwen", model="m", purpose="test",
            call_messages=[messages()], max_tokens=100, confirmed=True, environ={})
    assert not service.db_path.exists()


def test_authorization_reserves_budget_and_prevents_duplicate_charge(tmp_path):
    service = AIGovernanceService(tmp_path / "usage.db")
    plan = service.authorize(request_id="same", provider="qwen", model="m", purpose="test",
        call_messages=[messages()], max_tokens=100, confirmed=True, environ=ENV)
    assert plan["ready"] and plan["request_reserve_usd"] > 0
    with pytest.raises(DuplicateAIRequest):
        service.authorize(request_id="same", provider="qwen", model="m", purpose="test",
            call_messages=[messages()], max_tokens=100, confirmed=True, environ=ENV)
    service.record_call(request_id="same", call_index=1,
        completion=AICompletion("not stored", 10, 5), environ=ENV)
    raw = service.db_path.read_bytes()
    assert b"not stored" not in raw and b"public" not in raw


def test_personal_text_and_monthly_overrun_are_stopped(tmp_path):
    service = AIGovernanceService(tmp_path / "usage.db")
    with pytest.raises(AIGovernanceError, match="個人情報"):
        service.authorize(request_id="private", provider="qwen", model="m", purpose="test",
            call_messages=[messages("090-1234-5678")], max_tokens=10,
            confirmed=True, environ=ENV)
    tiny = {**ENV, "PACHI_AI_QWEN_MONTHLY_LIMIT_USD": "0.000001"}
    with pytest.raises(AIGovernanceError, match="月上限"):
        service.authorize(request_id="over", provider="qwen", model="m", purpose="test",
            call_messages=[messages()], max_tokens=600, confirmed=True, environ=tiny)


def test_status_is_read_only_and_contains_no_keys(tmp_path):
    service = AIGovernanceService(tmp_path / "usage.db")
    status = service.status({**ENV, "DASHSCOPE_API_KEY": "never-show"})
    assert status["duplicate_charge_guard"] is True
    assert status["personal_history_allowed"] is False
    assert "never-show" not in json.dumps(status)
    assert not service.db_path.exists()


def test_confirmed_payload_cannot_be_replaced_after_authorization(tmp_path):
    service = AIGovernanceService(tmp_path / "usage.db")
    expected = messages("公開統計")
    service.authorize(request_id="locked", provider="qwen", model="m", purpose="test",
        call_messages=[expected], max_tokens=100, confirmed=True, environ=ENV)
    class NeverSend:
        display_name = "Qwen"
        def complete_detailed(self, *_args, **_kwargs):
            pytest.fail("changed content must not be sent")
    client = GovernedClient(NeverSend(), service, "locked", ENV, expected_messages=expected)
    with pytest.raises(AIGovernanceError, match="送信内容が変わった"):
        client.complete(messages("別の内容"), max_tokens=100)


@pytest.mark.parametrize("field,value,category", [
    ("subject_label", "user@example.com", "email"),
    ("source_label", "phone 090-1234-5678", "phone"),
    ("url", "https://example.com/?api_key=do-not-store", "secret"),
    ("hall_name", "4111 1111 1111 1111", "card"),
    ("personal_history", [], "private_field"),
    ("kind", "user_input", "private_field"),
    ("snapshot_id", "contact user@example.com", "email"),
])
def test_structured_snapshot_labels_and_nested_fields_are_not_privacy_exempt(field, value, category):
    payload = {"snapshot": {"snapshot_id": "a" * 64, "evidence": [{field: value}]}}
    result = privacy_check(messages(json.dumps(payload)))
    assert not result["passed"] and category in result["blocked_categories"]
    assert "do-not-store" not in json.dumps(result) and "user@example.com" not in json.dumps(result)


def test_only_exact_generated_snapshot_ids_are_exempt_from_numeric_privacy_checks():
    numeric_digest = "a" * 48 + "4111111111111111"
    assert privacy_check(messages(json.dumps({"snapshot_id": numeric_digest})))["passed"]
    assert not privacy_check(messages(json.dumps({"subject_label": numeric_digest})))["passed"]
    assert not privacy_check(messages(json.dumps({"snapshot_id": numeric_digest + "a"})))["passed"]


@pytest.mark.parametrize("phone", ["09012345678", "090-1234-5678", "03-1234-5678", "0120-123-456"])
def test_phone_guard_blocks_domestic_numbers_without_misclassifying_generated_timestamps(phone):
    assert "phone" in privacy_check(messages(phone))["blocked_categories"]
    payload = {"generated_at": "2026-09-20T01:23:45.012345+00:00", "report_date": "2026-09-14"}
    assert privacy_check(messages(json.dumps(payload)))["passed"]


@pytest.mark.parametrize("invalid", ["", "0", "-1", "nan", "inf", "not-a-limit"])
def test_explicit_invalid_provider_limit_never_falls_back_to_global(tmp_path, invalid):
    env = {**ENV, "PACHI_AI_MONTHLY_LIMIT_USD": "100", "PACHI_AI_QWEN_MONTHLY_LIMIT_USD": invalid}
    assert monthly_limit("qwen", env) is None
    service = AIGovernanceService(tmp_path / "usage.db")
    with pytest.raises(AIGovernanceError, match="月上限が未設定"):
        service.authorize(**authorization(), environ=env)
    assert not service.db_path.exists()
    del env["PACHI_AI_QWEN_MONTHLY_LIMIT_USD"]
    assert monthly_limit("qwen", env) == 100


def test_authorize_many_rolls_back_every_reservation_when_combined_budget_is_too_large(tmp_path):
    service = AIGovernanceService(tmp_path / "usage.db")
    reserve = service.plan(provider="qwen", call_messages=[messages()], max_tokens=100,
                           environ=ENV)["request_reserve_usd"]
    env = {**ENV, "PACHI_AI_QWEN_MONTHLY_LIMIT_USD": str(reserve * 1.5)}
    with pytest.raises(AIGovernanceError, match="月上限を超過"):
        service.authorize_many(requests=[authorization("first"), authorization("second")], environ=env)
    assert service.month_used("qwen") == 0
    with sqlite3.connect(service.db_path) as conn:
        assert all(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in LEDGER_TABLES)
    assert service.authorize(**authorization("first"), environ=env)["ready"]


def test_authorize_many_rolls_back_new_reservations_if_a_later_id_already_exists(tmp_path):
    service = AIGovernanceService(tmp_path / "usage.db")
    first = service.authorize(**authorization("already"), environ=ENV)
    with pytest.raises(DuplicateAIRequest):
        service.authorize_many(requests=[authorization("new"), authorization("already")], environ=ENV)
    assert service.month_used("qwen") == first["request_reserve_usd"]
    with sqlite3.connect(service.db_path) as conn:
        assert conn.execute("SELECT request_id FROM ai_usage_requests").fetchall() == [("already",)]
    assert service.authorize(**authorization("new"), environ=ENV)["ready"]


def test_concurrent_service_instances_cannot_reserve_the_same_remaining_budget(tmp_path):
    path = tmp_path / "usage.db"
    ready = Barrier(4)
    class ConcurrentService(AIGovernanceService):
        def plan(self, **kwargs):
            result = super().plan(**kwargs)
            ready.wait(timeout=10)
            return result
    reserve = AIGovernanceService(path).plan(provider="qwen", call_messages=[messages()],
        max_tokens=100, environ=ENV)["request_reserve_usd"]
    env = {**ENV, "PACHI_AI_QWEN_MONTHLY_LIMIT_USD": str(reserve * 1.5)}
    def reserve_one(index):
        try:
            return ConcurrentService(path).authorize(**authorization(f"parallel-{index}"), environ=env)
        except AIGovernanceError as exc:
            assert "月上限" in str(exc)
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve_one, range(4)))
    assert sum(result is not None for result in results) == 1
    assert AIGovernanceService(path).month_used("qwen") == reserve


@pytest.mark.parametrize("outcome", ["completed", "timeout", "record_failure"])
def test_durable_claim_blocks_repeat_transport_after_unknown_outcome_and_restart(tmp_path, monkeypatch, outcome):
    service = AIGovernanceService(tmp_path / "usage.db")
    plan = service.authorize(**authorization(), environ=ENV)
    calls = []
    class Client:
        def complete_detailed(self, sent, *, max_tokens):
            calls.append(sent)
            if outcome == "timeout":
                raise AIProviderError("timeout", "unknown provider outcome")
            return AICompletion("never-persist-response", 100, 20)
    if outcome == "record_failure":
        def failed_record(**_kwargs):
            raise sqlite3.OperationalError("simulated ledger write failure")
        monkeypatch.setattr(service, "record_call", failed_record)
    kwargs = {"request_id": "request", "call_index": 1, "messages": messages(), "max_tokens": 100}
    if outcome == "completed":
        service.complete_call(Client(), **kwargs)
    else:
        with pytest.raises(AIProviderError if outcome == "timeout" else sqlite3.OperationalError):
            service.complete_call(Client(), **kwargs)
    restarted = AIGovernanceService(service.db_path)
    for instance in (service, restarted):
        with pytest.raises(DuplicateAIRequest):
            instance.complete_call(Client(), **kwargs)
    assert len(calls) == 1
    assert restarted.month_used("qwen") == plan["request_reserve_usd"]
    with sqlite3.connect(service.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_usage_claims").fetchone()[0] == 1
        rows = conn.execute("SELECT status,error_kind,cost_usd FROM ai_usage_calls").fetchall()
    if outcome == "timeout":
        assert rows == [("failed", "timeout", None)]
    elif outcome == "record_failure":
        assert rows == []
    assert b"never-persist-response" not in service.db_path.read_bytes()


def test_claim_is_durable_even_if_process_stops_before_transport(tmp_path):
    service = AIGovernanceService(tmp_path / "usage.db")
    service.authorize(**authorization(), environ=ENV)
    kwargs = {"request_id": "request", "call_index": 1, "messages": messages(), "max_tokens": 100}
    service.claim_call(**kwargs)
    class NeverSend:
        def complete_detailed(self, *_args, **_kwargs):
            pytest.fail("a claimed call must never be retried")
    with pytest.raises(DuplicateAIRequest):
        AIGovernanceService(service.db_path).complete_call(NeverSend(), **kwargs)


def test_two_service_instances_racing_to_send_one_slot_transport_only_once(tmp_path):
    path = tmp_path / "usage.db"
    AIGovernanceService(path).authorize(**authorization(), environ=ENV)
    ready, calls = Barrier(2), []
    class Client:
        def complete_detailed(self, sent, *, max_tokens):
            calls.append(sent)
            return AICompletion("not stored", 0, 0)
    def send(_index):
        ready.wait(timeout=10)
        try:
            return AIGovernanceService(path).complete_call(Client(), request_id="request", call_index=1,
                                                          messages=messages(), max_tokens=100)
        except DuplicateAIRequest:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert sum(result is not None for result in results) == len(calls) == 1


def test_usage_uses_frozen_authorization_prices_not_later_environment(tmp_path):
    service = AIGovernanceService(tmp_path / "usage.db")
    env = dict(ENV)
    service.authorize(**authorization(), environ=env)
    env.update(PACHI_AI_QWEN_INPUT_USD_PER_MTOK="999", PACHI_AI_QWEN_OUTPUT_USD_PER_MTOK="999",
               PACHI_AI_QWEN_MONTHLY_LIMIT_USD="999")
    restarted = AIGovernanceService(service.db_path)
    restarted.record_call(request_id="request", call_index=1,
                          completion=AICompletion("not stored", 100, 20), environ=env)
    with sqlite3.connect(service.db_path) as conn:
        assert conn.execute("SELECT cost_usd FROM ai_usage_calls").fetchone()[0] == pytest.approx(0.00014)
        assert conn.execute("SELECT input_usd_per_million,output_usd_per_million,monthly_limit_usd "
                            "FROM ai_usage_terms").fetchone() == (1, 2, 1.5)


@pytest.mark.parametrize("change", [{"call_index": 2}, {"max_tokens": 101}, {"messages": messages("別の内容")}])
def test_unreserved_or_modified_call_never_reaches_transport_or_consumes_slot(tmp_path, change):
    service = AIGovernanceService(tmp_path / "usage.db")
    service.authorize(**authorization(), environ=ENV)
    class NeverSend:
        def complete_detailed(self, *_args, **_kwargs):
            pytest.fail("unapproved payload must not be sent")
    kwargs = {"request_id": "request", "call_index": 1, "messages": messages(), "max_tokens": 100, **change}
    with pytest.raises(AIGovernanceError):
        service.complete_call(NeverSend(), **kwargs)
    with sqlite3.connect(service.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_usage_claims").fetchone()[0] == 0
