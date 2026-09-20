"""AIチャット・レポートエンドポイント"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.ai_comparison import comparison_status, run_comparison
from api.ai_evaluation import EvaluationConflict, evaluations
from api.ai_knowledge import index_status, load_index, search_knowledge
from api.ai_governance import AIGovernanceError, DuplicateAIRequest, governance
from api.ai_review import build_review

from api.deps import (
    HALL_REPORTS_DB,
    MACHINES_DIR,
    WEB_DIR,
    _cache_get,
    _cache_invalidate_prefix,
    _cache_set,
    _get_event_conn,
    _get_machine_path,
    _get_reports_conn,
    logger,
)

router = APIRouter()

try:
    from api.ai_service import chat_result, report_result, estimate_result, trend_result, explain_event_forecast, answer_event_question, get_ai_status, hall_question_result
    AI_AVAILABLE = True
except ImportError:
    try:
        from ai_service import chat_result, report_result, estimate_result, trend_result, explain_event_forecast, answer_event_question, get_ai_status, hall_question_result
        AI_AVAILABLE = True
    except ImportError:
        AI_AVAILABLE = False


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    hall_name: str = Field("ベガスベガス大東店", max_length=120)
    history: list = Field(default_factory=list, max_length=12)
    target_date: date | None = None


class EventQuestionRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=300)
    visit_date: str
    region: Literal["all", "shijonawate", "matsumoto_shiojiri", "nagano", "osaka"] = "shijonawate"


class EstimateCommentRequest(BaseModel):
    machine_name: str = Field("", max_length=120)
    games: int = Field(0, ge=0)
    posterior: dict[str, Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]] = Field(default_factory=dict)
    ev: float = Field(0, allow_inf_nan=False)


class HallQuestionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    hall_name: str = Field(..., min_length=1, max_length=120)
    visit_date: date
    days: int = Field(90, ge=30, le=730)
    message: str = Field("この店の傾向は？", min_length=1, max_length=2000)
    topic: Literal["auto", "overview", "weekday", "digit", "machine", "event", "seat", "layout", "comparison"] = "auto"
    machine_name: str = Field("", max_length=120)
    request_id: UUID | None = None
    use_external_ai: bool = False
    confirm_public_data_only: bool = False


class AiComparisonRequest(BaseModel):
    model_config = {"extra": "forbid"}
    hall_name: str = Field(..., min_length=1, max_length=120)
    visit_date: date
    days: int = Field(90, ge=30, le=730)
    providers: list[Literal["groq", "deepseek", "qwen", "kimi", "claude"]] = Field(min_length=1, max_length=5)
    case_ids: list[Literal["machine", "weekday", "digit", "seat"]] = Field(min_length=1, max_length=3)
    mode: Literal["preview", "external"] = "preview"
    confirm_external: bool = False
    confirm_public_data_only: bool = False
    request_id: UUID | None = None


class AiEvaluationRequest(BaseModel):
    model_config = {"extra": "forbid"}
    request_id: UUID
    suite_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: Literal["offline", "external"] = "offline"
    provider: Literal["groq", "deepseek", "qwen", "kimi", "claude"] | None = None
    repeats: int = Field(2, ge=1, le=3, strict=True)
    confirm_external: bool = False
    confirm_public_data_only: bool = False


class KnowledgeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    question: str = Field(min_length=1, max_length=1000)
    category: Literal["glossary", "machine", "expectation", "hall"] = "glossary"
    target_date: date
    machine_name: str = Field(default="", max_length=120)
    hall_name: str = Field(default="", max_length=120)
    condition_id: str = Field(default="", max_length=180)
    index_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@router.get("/api/ai/knowledge")
def api_knowledge_status():
    return index_status()


@router.post("/api/ai/knowledge/search")
def api_knowledge_search(req: KnowledgeRequest):
    index = load_index()
    if req.index_hash != index["index_hash"]:
        raise HTTPException(409, "資料の版が更新されました。資料一覧を更新して条件を選び直してください。")
    if not req.question.strip():
        raise HTTPException(422, "調べたい用語・内容を入力してください")
    return search_knowledge(**req.model_dump(exclude={"index_hash", "target_date"}),
                            target_date=req.target_date.isoformat(), index=index)


@router.get("/api/ai/evaluation")
def api_ai_evaluation():
    return {**evaluations.catalog(), "connection": {**comparison_status(), "governance": governance.status()}}


@router.post("/api/ai/evaluation/runs")
def api_ai_evaluation_run(req: AiEvaluationRequest):
    try:
        return evaluations.start(**req.model_dump())
    except (EvaluationConflict, DuplicateAIRequest) as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, AIGovernanceError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/api/ai/evaluation/runs/{run_id}")
def api_ai_evaluation_report(run_id: UUID):
    try:
        return evaluations.report(str(run_id))
    except KeyError as exc:
        raise HTTPException(404, "評価記録がありません") from exc


@router.get("/api/ai/comparison/status")
def api_ai_comparison_status():
    return {**comparison_status(), "governance": governance.status()}


@router.get("/api/ai/governance")
def api_ai_governance():
    return governance.status()


@router.get("/api/ai/review")
def api_ai_review():
    return build_review(governance_status=governance.status())


@router.post("/api/ai/comparison/run")
def api_ai_comparison_run(req: AiComparisonRequest):
    if req.mode == "external" and req.request_id is None:
        raise HTTPException(422, "二重送信を防ぐ実行IDが必要です")
    try:
        return run_comparison(
            hall_name=req.hall_name, target_date=req.visit_date.isoformat(), days=req.days,
            provider_names=req.providers, case_ids=req.case_ids, mode=req.mode,
            confirm_external=req.confirm_external,
            confirm_public_data_only=req.confirm_public_data_only,
            request_id=str(req.request_id) if req.request_id else None, governance_service=governance,
            db_path=HALL_REPORTS_DB,
        )
    except DuplicateAIRequest as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, AIGovernanceError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/ai/hall_ask")
def api_ai_hall_ask(req: HallQuestionRequest):
    if req.use_external_ai and req.request_id is None:
        raise HTTPException(422, "二重送信を防ぐ実行IDが必要です")
    if not AI_AVAILABLE:
        return {"summary": "AIサービスを読み込めません。", "answer_status": "no_data"}
    if not req.hall_name.strip() or req.hall_name.strip() == "全店舗":
        raise HTTPException(422, "分析する店舗を1店選択してください")
    try:
        return hall_question_result(hall_name=req.hall_name, target_date=req.visit_date.isoformat(),
            question=req.message, days=req.days, topic=req.topic, machine_name=req.machine_name,
            external_request_id=str(req.request_id) if req.request_id else None, confirm_external=req.use_external_ai,
            confirm_public_data_only=req.confirm_public_data_only)
    except DuplicateAIRequest as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, AIGovernanceError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/ai/chat")
def api_ai_chat(req: ChatRequest):
    if not AI_AVAILABLE:
        return {"reply": "AIサービスが利用できません。"}
    result = chat_result(req.message, req.hall_name, req.history,
                         req.target_date.isoformat() if req.target_date else None)
    return {**result, "reply": result["summary"]}


@router.get("/api/ai/report")
def api_ai_report(hall_name: str = "ベガスベガス大東店", target_date: date | None = None):
    if not AI_AVAILABLE:
        return {"report": "AIサービスが利用できません。"}
    result = report_result(hall_name, target_date.isoformat() if target_date else None)
    return {**result, "report": result["summary"]}


@router.post("/api/ai/estimate_comment")
def api_ai_estimate_comment(body: EstimateCommentRequest):
    if not AI_AVAILABLE:
        return {"comment": ""}
    result = estimate_result(
        machine_name=body.machine_name, games=body.games,
        element_counts={}, posterior=body.posterior, ev=body.ev, recommendation="",
    )
    return {**result, "comment": result["summary"]}


@router.get("/api/ai/status")
def api_ai_status():
    if not AI_AVAILABLE:
        return {
            "available": False,
            "provider": "disabled",
            "provider_label": "統計エンジン",
            "model": "",
            "reason": "AIサービスを読み込めません",
            "fallback": "統計エンジン",
            "supported_providers": [],
        }
    return {**get_ai_status(), "governance": governance.status()}


@router.get("/api/ai/hall_profile")
def api_ai_hall_profile(
    hall_name: str = Query(..., min_length=1),
    visit_date: date = Query(...),
    days: int = Query(365, ge=30, le=730),
):
    """カルテ説明も共通の店舗根拠を使用。重い予測検証を再実行しない。"""
    if not AI_AVAILABLE:
        return {"available": False, "engine": "統計エンジン", "summary": "AIサービスを読み込めません。"}
    return hall_question_result(hall_name=hall_name, target_date=visit_date.isoformat(),
                                question="この店の傾向は？", days=days, topic="overview")


@router.get("/api/ai/event_forecast")
def api_ai_event_forecast(
    visit_date: str = Query(...),
    region: Literal["all", "shijonawate", "matsumoto_shiojiri", "nagano", "osaka"] = "shijonawate",
):
    """品質ゲート済み予測をAIまたは固定文で説明する。AIに候補変更権限はない。"""
    from api.routers.events import get_event_analysis

    analysis = get_event_analysis(
        visit_date=visit_date,
        region=region,
        hall_name=None,
        history_days=730,
        future_days=31,
    )
    result = explain_event_forecast(analysis) if AI_AVAILABLE else {
        "engine": "統計エンジン",
        "summary": "AIサービスを読み込めないため、イベント分析画面の固定判定を確認してください。",
        "facts": [],
        "decision_source": "説明可能ベースライン v1",
        "ai_changed_decision": False,
    }
    result["visit_date"] = visit_date
    result["region"] = region
    result["summary_counts"] = analysis.get("summary", {})
    return result


@router.post("/api/ai/event_ask")
def api_ai_event_ask(req: EventQuestionRequest):
    """イベント・店舗傾向の根拠限定Q&A。"""
    from api.routers.events import get_event_analysis

    analysis = get_event_analysis(
        visit_date=req.visit_date,
        region=req.region,
        hall_name=None,
        history_days=730,
        future_days=31,
    )
    result = answer_event_question(req.message, analysis) if AI_AVAILABLE else {
        "engine": "統計エンジン", "summary": "AIサービスを読み込めません。",
        "facts": [], "machine_facts": [], "decision_source": "説明可能ベースライン v1",
        "ai_changed_decision": False,
    }
    result["visit_date"] = req.visit_date
    result["region"] = req.region
    return result

