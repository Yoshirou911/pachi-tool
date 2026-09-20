"""v3.43: opt-in, evidence-contract benchmark for multiple AI providers.

No provider is contacted in preview mode. External mode requires two server-side
switches plus request confirmation. Results never alter predictions or rankings.
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import os
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Callable, Mapping

from api.ai_evidence import evidence_messages, validate_answer
from api.ai_hall_analysis import build_hall_snapshot
from api.ai_guard import check_selection, display_report
from api.ai_provider import (
    AICompletion,
    AIProviderClient,
    AIProviderError,
    PROVIDERS,
    comparison_unit_prices,
    get_comparison_provider_config,
)
from api.ai_governance import AIGovernanceError, AIGovernanceService, validate_request_id
from config import HALL_REPORTS_DB

COMPARISON_PROTOCOL = "ai-provider-comparison-v2-guard"
MAX_CASES = 3
FIXED_CASES = {
    "machine": {"label": "強い機種", "question": "公開実績では、どの機種の数値が相対的に高い？", "topic": "machine"},
    "weekday": {"label": "曜日傾向", "question": "公開実績では、曜日ごとにどのような差がある？", "topic": "weekday"},
    "digit": {"label": "日付末尾", "question": "公開実績では、日付末尾ごとにどのような差がある？", "topic": "digit"},
    "seat": {"label": "台番号履歴", "question": "現在の推薦ではなく、保存済み台番号履歴から何を確認できる？", "topic": "seat"},
}


def _false(value: str | None) -> bool:
    return value is None or value.strip().lower() in {"", "0", "false", "no", "off", "disabled"}


def comparison_status(environ: Mapping[str, str] | None = None) -> dict:
    env = os.environ if environ is None else environ
    providers = []
    for name, definition in PROVIDERS.items():
        config = get_comparison_provider_config(name, env)
        input_price, output_price = comparison_unit_prices(name, env)
        configured = bool(config and config.api_key)
        enabled = bool(config and config.allow_external)
        if not configured:
            reason = f"{definition.key_env} が未設定"
        elif not enabled:
            reason = "比較用の外部送信が無効"
        else:
            reason = "実測可能"
        providers.append({
            "provider": name, "label": definition.label,
            "model": config.model if config else definition.default_model,
            "configured": configured, "ready": configured and enabled,
            "reason": reason,
            "price_configured": input_price is not None and output_price is not None,
        })
    return {
        "protocol": COMPARISON_PROTOCOL,
        "external_comparison_enabled": not _false(env.get("PACHI_AI_ALLOW_EXTERNAL"))
            and not _false(env.get("PACHI_AI_ALLOW_COMPARISON_EXTERNAL")),
        "providers": providers,
        "cases": [{"id": key, **value} for key, value in FIXED_CASES.items()],
        "max_cases_per_run": MAX_CASES,
        "notice": "準備確認では外部通信しません。実測は選択したAI×問題数だけAPI呼び出しが発生します。",
        "personal_history_sent": False,
        "changes_live_prediction": False,
    }


def _case_snapshots(db_path: Path, *, hall_name: str, target_date: str,
                    days: int, case_ids: list[str]) -> list[dict]:
    cases = []
    for case_id in case_ids:
        definition = FIXED_CASES[case_id]
        snapshot = build_hall_snapshot(
            db_path, hall_name=hall_name, target_date=target_date, days=days,
            question=definition["question"], topic=definition["topic"],
        )
        cases.append({
            "case_id": case_id, "label": definition["label"],
            "question": definition["question"], "snapshot": snapshot,
        })
    return cases


def _cost(input_tokens, output_tokens, prices):
    if input_tokens is None or output_tokens is None or None in prices:
        return None
    return round((input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000, 8)


def _summary(results: list[dict]) -> dict:
    completed = [item for item in results if item["status"] == "validated"]
    measured_latency = [item["latency_ms"] for item in results if item["latency_ms"] is not None]
    measured_cost = [item["estimated_cost_usd"] for item in results if item["estimated_cost_usd"] is not None]
    return {
        "attempted_cases": len(results),
        "validated_cases": len(completed),
        "contract_pass_rate_pct": round(100 * sum(bool(r["contract_valid"]) for r in results) / len(results), 1) if results else None,
        "guard_rejected_cases": sum(r.get("error_kind") == "evidence_guard_rejected" for r in results),
        "abstained_cases": sum(r["status"] == "abstained" for r in results),
        "evidence_match_pct": round(mean(item["evidence_match_pct"] for item in completed), 1) if completed else None,
        "average_latency_ms": round(mean(measured_latency)) if measured_latency else None,
        "reported_input_tokens": sum(item["input_tokens"] for item in results if item["input_tokens"] is not None) or None,
        "reported_output_tokens": sum(item["output_tokens"] for item in results if item["output_tokens"] is not None) or None,
        "estimated_cost_usd": round(sum(measured_cost), 8) if len(measured_cost) == len(results) and results else None,
    }


def run_comparison(*, hall_name: str, target_date: str, days: int,
                   provider_names: list[str], case_ids: list[str], mode: str,
                   confirm_external: bool, environ: Mapping[str, str] | None = None,
                   db_path: Path | None = None,
                   client_factory: Callable = AIProviderClient,
                   request_id: str | None = None,
                   confirm_public_data_only: bool = False,
                   governance_service: AIGovernanceService | None = None) -> dict:
    env = os.environ if environ is None else environ
    date.fromisoformat(target_date)
    hall = hall_name.strip()
    if not hall or hall == "全店舗":
        raise ValueError("比較する店舗を1店選択してください")
    providers = list(dict.fromkeys(provider_names))
    cases_requested = list(dict.fromkeys(case_ids))
    if not providers or any(name not in PROVIDERS for name in providers):
        raise ValueError("対応するAIを1社以上選択してください")
    if not cases_requested or len(cases_requested) > MAX_CASES or any(case not in FIXED_CASES for case in cases_requested):
        raise ValueError(f"固定問題は1〜{MAX_CASES}件選択してください")
    if mode not in {"preview", "external"}:
        raise ValueError("比較モードが不正です")
    if mode == "external" and not confirm_external:
        raise ValueError("外部AI実測の確認が必要です")
    if mode == "external":
        if governance_service is None:
            raise AIGovernanceError("外部AIの費用・送信管理が設定されていません")
        validate_request_id(request_id)
        if not confirm_public_data_only:
            raise ValueError("外部送信する公開統計・固定質問の確認が必要です")
    if mode == "external" and (_false(env.get("PACHI_AI_ALLOW_EXTERNAL"))
                               or _false(env.get("PACHI_AI_ALLOW_COMPARISON_EXTERNAL"))):
        raise ValueError("サーバー側で複数AI比較の外部送信が許可されていません")

    cases = _case_snapshots(db_path or HALL_REPORTS_DB, hall_name=hall,
                            target_date=target_date, days=days, case_ids=cases_requested)
    public_cases = [{"case_id": item["case_id"], "label": item["label"],
                     "question": item["question"],
                     "snapshot_id": item["snapshot"]["snapshot_id"],
                     "evidence_count": len(item["snapshot"]["evidence"]),
                     "has_evidence": bool(item["snapshot"]["evidence"])} for item in cases]
    provider_results = []
    governance_plans = []
    governed_ids = {}
    if mode == "external" and governance_service:
        governed_messages = [evidence_messages(item["snapshot"], item["question"])
                             for item in cases if item["snapshot"]["evidence"]]
        preflight = []
        for name in providers:
            config = get_comparison_provider_config(name, env)
            if config and config.available and governed_messages:
                governed_ids[name] = f"{request_id}:{name}"
                preflight.append(dict(request_id=governed_ids[name], provider=name, model=config.model,
                    purpose="provider_comparison", call_messages=governed_messages,
                    max_tokens=600, confirmed=confirm_external and confirm_public_data_only))
        governance_plans = governance_service.authorize_many(requests=preflight, environ=env)
    for name in providers:
        definition = PROVIDERS[name]
        config = get_comparison_provider_config(name, env)
        prices = comparison_unit_prices(name, env)
        results = []
        if mode == "preview":
            provider_results.append({
                "provider": name, "label": definition.label,
                "model": config.model if config else definition.default_model,
                "ready": bool(config and config.available), "status": "not_run",
                "cases": [], "summary": _summary([]),
            })
            continue
        if not config or not config.available:
            provider_results.append({
                "provider": name, "label": definition.label,
                "model": config.model if config else definition.default_model,
                "ready": False, "status": "unavailable", "cases": [],
                "summary": _summary([]),
            })
            continue
        governed_request_id = governed_ids.get(name)
        client = client_factory(config)
        external_call_index = 0
        for item in cases:
            snapshot = item["snapshot"]
            if not snapshot["evidence"]:
                results.append({"case_id": item["case_id"], "status": "no_evidence",
                    "contract_valid": False, "evidence_match_pct": None, "latency_ms": None,
                    "input_tokens": None, "output_tokens": None, "estimated_cost_usd": None,
                    "error_kind": "no_evidence"})
                continue
            external_call_index += 1
            started = perf_counter()
            completion = None
            try:
                messages = evidence_messages(snapshot, item["question"])
                completion = governance_service.complete_call(client, request_id=governed_request_id,
                    call_index=external_call_index, messages=messages, max_tokens=600, environ=env)
                selected = validate_answer(completion.content, snapshot, allow_abstention=True)
                guard = check_selection(selected, snapshot, item["question"])
                if not guard["passed"]:
                    results.append({"case_id": item["case_id"], "status": "rejected",
                        "contract_valid": True, "evidence_match_pct": None,
                        "latency_ms": round((perf_counter() - started) * 1000),
                        "input_tokens": completion.input_tokens, "output_tokens": completion.output_tokens,
                        "estimated_cost_usd": _cost(completion.input_tokens, completion.output_tokens, prices),
                        "error_kind": "evidence_guard_rejected", "answer_guard": display_report(guard)})
                    continue
                expected = [fact["id"] for fact in snapshot["evidence"][:8]]
                union = set(selected) | set(expected)
                match = 100 * len(set(selected) & set(expected)) / len(union) if union else 100.0
                results.append({"case_id": item["case_id"], "status": "validated" if selected else "abstained",
                    "contract_valid": True, "evidence_match_pct": round(match, 1),
                    "latency_ms": round((perf_counter() - started) * 1000),
                    "input_tokens": completion.input_tokens, "output_tokens": completion.output_tokens,
                    "estimated_cost_usd": _cost(completion.input_tokens, completion.output_tokens, prices),
                    "error_kind": None})
            except AIProviderError as exc:
                results.append({"case_id": item["case_id"], "status": "failed",
                    "contract_valid": False, "evidence_match_pct": None,
                    "latency_ms": round((perf_counter() - started) * 1000),
                    "input_tokens": None, "output_tokens": None, "estimated_cost_usd": None,
                    "error_kind": exc.kind})
            except AIGovernanceError:
                raise
            except (ValueError, TypeError, json.JSONDecodeError):
                results.append({"case_id": item["case_id"], "status": "rejected",
                    "contract_valid": False, "evidence_match_pct": None,
                    "latency_ms": round((perf_counter() - started) * 1000),
                    "input_tokens": None, "output_tokens": None, "estimated_cost_usd": None,
                    "error_kind": "contract_rejected"})
            except Exception:
                # Never return raw provider/library exceptions or interrupt other providers.
                results.append({"case_id": item["case_id"], "status": "failed",
                    "contract_valid": False, "evidence_match_pct": None,
                    "latency_ms": round((perf_counter() - started) * 1000),
                    "input_tokens": None, "output_tokens": None, "estimated_cost_usd": None,
                    "error_kind": "internal_error"})
        provider_results.append({"provider": name, "label": definition.label,
            "model": config.model, "ready": True, "status": "measured",
            "cases": results, "summary": _summary(results)})

    identity = {"protocol": COMPARISON_PROTOCOL, "hall": hall, "target_date": target_date,
                "days": days, "mode": mode, "providers": providers,
                "cases": [(item["case_id"], item["snapshot_id"]) for item in public_cases]}
    benchmark_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {
        **identity, "benchmark_id": benchmark_id,
        "case_definitions": public_cases, "provider_results": provider_results,
        "external_calls_planned": len(providers) * sum(item["has_evidence"] for item in public_cases),
        "personal_history_sent": False, "raw_provider_answers_stored": False,
        "governance": {"applied": bool(governance_service and mode == "external"),
            "plans": governance_plans, "prompts_or_answers_stored": False},
        "changes_live_prediction": False, "winner": None,
        "notice": "比較値は根拠選択契約への適合度です。予測精度・勝率・AIの採用順位ではありません。",
    }
