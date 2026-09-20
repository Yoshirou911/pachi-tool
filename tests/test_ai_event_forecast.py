from api import ai_service
from api.routers import ai as ai_router
from api.routers import events as events_router


def _analysis():
    return {
        "upcoming": [
            {
                "event_date": "2026-09-07",
                "hall_name": "未検証店",
                "event_name": "7のつく日",
                "grade": "A",
                "matched_days": 8,
                "baseline_forecast": {"score": 49, "decision": "参考止まり"},
                "quality_gate": {
                    "passed": False,
                    "quality_score": 65,
                    "blockers": ["店舗全体の結果が3日未満"],
                },
            },
            {
                "event_date": "2026-09-25",
                "hall_name": "検証店",
                "event_name": "毎月25日",
                "grade": "A",
                "matched_days": 9,
                "baseline_forecast": {"score": 78, "decision": "実戦候補"},
                "quality_gate": {
                    "passed": True,
                    "quality_score": 88,
                    "blockers": [],
                },
            },
        ],
        "summary": {"upcoming_count": 2, "actionable_upcoming": 1},
    }


def test_event_forecast_ai_only_explains_fixed_decision(monkeypatch):
    monkeypatch.setattr(ai_service, "_get_client", lambda: None)
    result = ai_service.explain_event_forecast(_analysis())
    assert result["engine"] == "統計エンジン"
    assert result["facts"][0]["hall_name"] == "検証店"
    assert result["facts"][0]["decision"] == "実戦候補"
    assert result["ai_changed_decision"] is False
    assert result["decision_source"] == "説明可能ベースライン v1"


def test_event_question_refuses_to_invent_seat_number(monkeypatch):
    monkeypatch.setattr(ai_service, "_get_client", lambda: None)
    result = ai_service.answer_event_question("狙う台番号は何番？", _analysis())
    assert "台番号を確定できません" in result["summary"]
    assert result["ai_changed_decision"] is False


def test_event_forecast_endpoint_returns_counts_without_giving_ai_control(monkeypatch):
    monkeypatch.setattr(events_router, "get_event_analysis", lambda **_kwargs: _analysis())
    monkeypatch.setattr(ai_router, "AI_AVAILABLE", True)
    monkeypatch.setattr(
        ai_router,
        "explain_event_forecast",
        lambda analysis: {
            "engine": "統計エンジン",
            "summary": "固定説明",
            "facts": analysis["upcoming"],
            "decision_source": "説明可能ベースライン v1",
            "ai_changed_decision": False,
        },
    )
    result = ai_router.api_ai_event_forecast("2026-09-03", "shijonawate")
    assert result["summary_counts"]["actionable_upcoming"] == 1
    assert result["ai_changed_decision"] is False
    assert result["visit_date"] == "2026-09-03"
