"""v3.41: evidence-bound explanations. All public AI paths share one validator."""
from __future__ import annotations

from datetime import date
from api.ai_provider import AIProviderClient, get_provider_config, provider_status
from api.ai_governance import AIGovernanceError, GovernedClient, governance, validate_request_id
from api.ai_evidence import answer, evidence, freeze_evidence, historical_snapshot, metric
from config import HALL_REPORTS_DB
from api.ai_hall_analysis import build_hall_snapshot, resolve_topics
from api.ai_guard import VERSION as GUARD_VERSION, inspect_snapshot


def _get_client(*, request_id=None, purpose="", messages=None, confirmed=False,
                environ=None, governance_service=governance):
    """External AI is opt-in per request; all legacy callers stay local."""
    if not confirmed:
        return None
    validate_request_id(request_id)
    if not messages or governance_service is None:
        raise AIGovernanceError("外部AIの送信内容・費用管理が設定されていません")
    config = get_provider_config(environ)
    if not config or not config.available:
        raise ValueError("外部AIが接続されていません")
    governance_service.authorize(request_id=str(request_id), provider=config.definition.name,
        model=config.model, purpose=purpose, call_messages=[messages], max_tokens=600,
        confirmed=True, environ=environ)
    return GovernedClient(AIProviderClient(config), governance_service, str(request_id), environ,
                          expected_messages=messages)


def get_ai_status() -> dict:
    return {**provider_status(), "evidence_contract_version": "1.0",
            "answer_mode": "根拠照合・固定文", "personal_history_enabled": False,
            "automatic_external_calls": False, "answer_guard_version": GUARD_VERSION}


def current_engine_label(client=None) -> str:
    active = client or _get_client()
    return f"{active.display_name}＋根拠照合" if active else "統計エンジン"


def _seat_question(message: str) -> bool:
    return any(word in message for word in ("台番", "何番", "番台"))


def chat_result(message: str, hall_name: str, history: list | None = None,
                target_date: str | None = None) -> dict:
    target = target_date or date.today().isoformat()
    if hall_name and hall_name != "全店舗":
        return hall_question_result(hall_name=hall_name, target_date=target, question=message)
    snapshot = historical_snapshot(HALL_REPORTS_DB, hall_name=hall_name, target_date=target)
    notice = ("この集計だけでは台番号を確定できません。現在配置・台別履歴・台別検証を確認してください。"
              if _seat_question(message) else "")
    # Conversation history is untrusted, possibly personal and from other scopes.
    # v3.41 grounds each answer in the current scope, never in prior AI prose.
    return answer(snapshot, question=message, client=_get_client(), required_notice=notice)


def chat(message: str, hall_name: str, history: list) -> str:
    return chat_result(message, hall_name, history)["summary"]


def report_result(hall_name: str, target_date: str | None = None) -> dict:
    return chat_result("この店舗の公開実績と不足情報をまとめて", hall_name, target_date=target_date)


def generate_report(hall_name: str) -> str:
    return report_result(hall_name)["summary"]


def hall_question_result(*, hall_name: str, target_date: str, question: str,
                         days: int = 90, topic: str = "auto", machine_name: str = "",
                         external_request_id=None, confirm_external=False,
                         confirm_public_data_only=False, environ=None,
                         governance_service=governance) -> dict:
    from hall.names import canonical_hall_name
    hall = canonical_hall_name(hall_name)
    topics = resolve_topics(question, topic)
    event_facts = []
    event_error = False
    if "event" in topics:
        try:
            from api.routers.events import get_event_analysis
            analysis = get_event_analysis(visit_date=target_date, region="all", hall_name=hall,
                                          history_days=days, future_days=31)
            event_snapshot, _, _ = _event_evidence(analysis)
            event_facts = event_snapshot["evidence"]
        except Exception:
            event_error = True
    snapshot = build_hall_snapshot(HALL_REPORTS_DB, hall_name=hall, target_date=target_date,
        days=days, question=question, topic=topic, machine_name=machine_name, event_facts=event_facts)
    if event_error:
        # Add diagnostics before freezing again, keeping them covered by the ID.
        snapshot = {**snapshot, **freeze_evidence(snapshot["evidence"], target_date=target_date, scope=hall,
                    missing=snapshot["missing_information"] + ["登録イベントの読み込みに失敗しました。曜日や日付から代用しません"],
                    constraints=snapshot.get("constraints"))}
    client = _get_client()
    if confirm_external:
        validate_request_id(external_request_id)
        if not confirm_public_data_only:
            raise ValueError("外部送信する内容の確認が必要です")
        # answer() will stay local for insufficient or rejected evidence. Do not
        # reserve money or construct a provider for a request it cannot send.
        guard = inspect_snapshot(snapshot, question)
        if snapshot["evidence"] and not guard["blocked_codes"] and not guard["rejected"]:
            from api.ai_evidence import evidence_messages
            messages = evidence_messages(snapshot, question)
            client = _get_client(request_id=external_request_id, purpose="hall_question",
                messages=messages, confirmed=True, environ=environ,
                governance_service=governance_service)
    result = answer(snapshot, question=question, client=client)
    return {**result, "available": result["answer_status"] == "validated"}


def trend_result(profile: dict) -> dict:
    target = profile.get("visit_date") or date.today().isoformat()
    sources = [{"url": url, "retrieved_at": None, "label": profile.get("source_label")}
               for url in profile.get("source_urls", [])]
    common = dict(hall=profile.get("hall_name"), target=target,
                  start=profile.get("first_date"), end=profile.get("latest_date"),
                  sources=sources, source_label="店舗カルテ集計",
                  missing=["結果公開時刻・改訂履歴は未確認"])
    overall = profile.get("overall") or {}
    facts = []
    if profile.get("sample_days"):
        facts.append(evidence(**common, metrics=[
            metric("記録日数", profile.get("sample_days"), "日"),
            metric("日別平均差枚の平均", overall.get("avg_diff"), "枚"),
            metric("プラス日率", overall.get("positive_day_rate"), "%")]))
        for item in (profile.get("machine_profile") or [])[:8]:
            facts.append(evidence(**common, machine=item.get("machine_name"), metrics=[
                metric("記録日数", item.get("sample_days"), "日"),
                metric("既存カルテの補正平均差枚", item.get("avg_diff"), "枚"),
                metric("店舗平均との差", item.get("strength_margin"), "枚")]))
    snapshot = freeze_evidence(facts, target_date=target, scope=profile.get("hall_name") or "店舗カルテ",
                               constraints={"hall_name": profile.get("hall_name")},
                               missing=["将来の勝率・確定設定を示すものではありません"])
    return answer(snapshot, question="店舗カルテの要点と不足情報を説明", client=_get_client())


def explain_trend_profile(profile: dict) -> str:
    return trend_result(profile)["summary"]


def _event_evidence(analysis: dict) -> tuple[dict, list, list]:
    target = analysis.get("visit_date") or date.today().isoformat()
    ranked = sorted(list(analysis.get("upcoming") or []),
                    key=lambda item: (item.get("baseline_forecast") or {}).get("score") or 0,
                    reverse=True)[:5]
    facts, records, machine_facts = [], [], []
    patterns = {(row.get("hall_name"), row.get("event_name")): row
                for row in analysis.get("event_analysis", [])}
    for item in ranked:
        forecast = item.get("baseline_forecast") or {}
        gate = item.get("quality_gate") or {}
        fact = {
            "event_date": item.get("event_date"), "hall_name": item.get("hall_name"),
            "event_name": item.get("event_name"), "grade": item.get("grade"),
            "score": forecast.get("score"), "decision": forecast.get("decision", "参考止まり"),
            "matched_days": item.get("matched_days"), "quality_score": gate.get("quality_score"),
            "quality_passed": bool(gate.get("passed")), "blockers": list(gate.get("blockers") or []),
        }
        facts.append(fact)
        pattern = patterns.get((fact["hall_name"], fact["event_name"]), {})
        sources = [{"url": url, "retrieved_at": None, "label": "イベント分析の記録出典"}
                   for url in pattern.get("source_urls", [])]
        records.append(evidence(hall=fact["hall_name"], event=fact["event_name"],
            target=fact["event_date"], start=analysis.get("history_start"), end=analysis.get("reference_date"),
            sources=sources, kind="fixed_event_decision", source_label="説明可能ベースライン v1",
            decision=fact["decision"], missing=fact["blockers"] + ["当日の公式告知・設置状況は未確認"],
            metrics=[metric("基準点", fact["score"], "点"), metric("品質点", fact["quality_score"], "点"),
                     metric("対応実績日数", fact["matched_days"], "日")]))
    for row in analysis.get("event_analysis", []):
        if not (row.get("quality_gate") or {}).get("passed"):
            continue
        for item in (row.get("strong_machines") or [])[:3]:
            if len(machine_facts) >= 10:
                break
            fact = {key: item.get(key) for key in ("machine_name", "lift_vs_normal", "matched_days", "status")}
            fact.update(hall_name=row.get("hall_name"), event_name=row.get("event_name"))
            machine_facts.append(fact)
            records.append(evidence(hall=fact["hall_name"], event=fact["event_name"],
                machine=fact["machine_name"], target=target,
                start=analysis.get("history_start"), end=analysis.get("reference_date"),
                sources=[{"url": url, "retrieved_at": None} for url in row.get("source_urls", [])],
                source_label="イベント分析の機種別集計", metrics=[
                    metric("通常日比", fact["lift_vs_normal"], "枚"),
                    metric("対応実績日数", fact["matched_days"], "日")],
                missing=["機種別参考集計であり台番号候補ではありません"]))
    snapshot = freeze_evidence(records, target_date=target, scope=analysis.get("region") or "イベント分析",
        constraints={"allow_upcoming_events": True},
        missing=["対象日から先のイベント予定を含みます。予定日と集計期間を区別してください",
                 "取得・改訂履歴を使った厳密な事前予測検証は未実施"])
    return snapshot, facts, machine_facts


def _event_result(analysis: dict, question: str) -> dict:
    snapshot, facts, machine_facts = _event_evidence(analysis)
    notice = ("イベント分析だけでは台番号を確定できません。狙い台捜索の台番号候補で、現在配置・台別履歴・過去検証を確認してください。"
              if _seat_question(question) else "")
    result = answer(snapshot, question=question, client=_get_client(), required_notice=notice)
    return {**result, "facts": facts, "machine_facts": machine_facts,
            "decision_source": "説明可能ベースライン v1"}


def explain_event_forecast(analysis: dict) -> dict:
    return _event_result(analysis, "イベントの固定判定と不足情報を説明")


def answer_event_question(question: str, analysis: dict) -> dict:
    # One request, one provider call. Never call forecast AI first.
    return {**_event_result(analysis, question), "question": question}


def estimate_result(machine_name: str, games: int, element_counts: dict,
                    posterior: dict, ev: float, recommendation: str,
                    element_analysis=None, credible_interval=None,
                    element_powers=None, correlated_elements=None) -> dict:
    target = date.today().isoformat()
    metrics = [metric("入力ゲーム数", games, "G"), metric("入力された期待値", ev, "枚/1000G")]
    for setting in range(1, 7):
        value = posterior.get(str(setting), posterior.get(setting))
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
            metrics.append(metric(f"入力された設定{setting}の推定確率", round(value * 100, 2), "%"))
    fact = evidence(machine=machine_name, target=target, metrics=metrics,
                    kind="user_input", source_label="画面から渡された入力値（独立検証なし）",
                    missing=["入力値の妥当性・確定設定を独立に確認していません"])
    snapshot = freeze_evidence([fact], target_date=target, scope=machine_name or "設定推測",
        missing=["続行・撤退判定は元の設定推測画面で確認してください"])
    # These are personal live-session values; keep this path entirely local.
    return answer(snapshot, question="入力された推測結果を確認", client=None)


def comment_estimate(*args, **kwargs) -> str:
    return estimate_result(*args, **kwargs)["summary"]
