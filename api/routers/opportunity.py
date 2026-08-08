"""期待値(ハイエナ)機能エンドポイント"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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
from opportunity.models import (
    assess_quick_decision,
    deactivate_profile as deactivate_opportunity_profile,
    get_budget_summary as get_opportunity_budget_summary,
    get_dashboard as get_opportunity_dashboard,
    get_profile as get_opportunity_profile,
    save_budget as save_opportunity_budget,
    save_candidate as save_opportunity_candidate,
    save_profile as save_opportunity_profile,
    save_result as save_opportunity_result,
    set_candidate_status as set_opportunity_candidate_status,
)

router = APIRouter()

class OpportunityCurvePoint(BaseModel):
    value: float = Field(ge=0)
    ev_yen: int
    minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    worst_case_yen: Optional[int] = Field(default=None, ge=0)

class OpportunityProfileCreate(BaseModel):
    machine_name: str = Field(min_length=1, max_length=120)
    condition_label: str = Field(default="条件未設定", min_length=1, max_length=120)
    exchange_type: Literal["equivalent", "56", "other", "unknown"] = "unknown"
    funding_mode: Literal["any", "cash", "medals"] = "any"
    reset_status: Literal["any", "normal", "reset_confirmed", "unknown"] = "unknown"
    metric_name: str = Field(default="現在ゲーム数", min_length=1, max_length=40)
    unit_label: str = Field(default="G", min_length=1, max_length=12)
    start_threshold: float = Field(ge=0)
    ceiling_threshold: Optional[float] = Field(default=None, ge=0)
    expected_value_yen: Optional[int] = Field(default=None, ge=0)
    estimated_play_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    worst_case_investment_yen: Optional[int] = Field(default=None, ge=0)
    stop_rule: str = Field(min_length=1, max_length=300)
    source_name: str = Field(default="", max_length=120)
    source_url: str = Field(default="", max_length=500)
    source_urls: list[str] = Field(default_factory=list, max_length=10)
    curve_points: list[OpportunityCurvePoint] = Field(default_factory=list, max_length=50)
    verified_on: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    confidence: Literal["official", "verified", "reference", "unverified"] = "unverified"
    notes: str = Field(default="", max_length=500)
    discrepancy_note: str = Field(default="", max_length=1000)

class OpportunityCandidateCreate(BaseModel):
    machine_name: str = Field(min_length=1, max_length=120)
    hall_name: str = Field(default="", max_length=120)
    seat_number: Optional[int] = Field(default=None, ge=1)
    current_value: float = Field(ge=0)
    profile_id: Optional[int] = Field(default=None, ge=1)
    observed_at: Optional[str] = Field(default=None, max_length=30)
    notes: str = Field(default="", max_length=500)

class OpportunityCandidateStatus(BaseModel):
    status: Literal["open", "skipped"]

class OpportunityResultCreate(BaseModel):
    played_on: str = Field(default_factory=lambda: date.today().isoformat(), pattern=r"^\d{4}-\d{2}-\d{2}$")
    investment_yen: int = Field(default=0, ge=0)
    returns_yen: int = Field(default=0, ge=0)
    played_minutes: int = Field(default=0, ge=0, le=1440)
    notes: str = Field(default="", max_length=500)

class OpportunityBudgetUpdate(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    starting_bankroll: int = Field(ge=0)
    loss_limit_yen: int = Field(ge=0)


class OpportunityQuickAssess(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    machine_name: str = Field(min_length=1, max_length=120)
    profile_id: int = Field(ge=1)
    current_value: float = Field(ge=0)
    exchange_type: Literal["equivalent", "56", "other"]
    funding_mode: Literal["cash", "medals"]
    reset_status: Literal["normal", "reset_confirmed", "unknown"]
    minutes_until_close: int = Field(ge=0, le=1440)


@router.get("/api/opportunity/dashboard", tags=["opportunity"])
def opportunity_dashboard(month: str = Query(default_factory=lambda: date.today().strftime("%Y-%m"), pattern=r"^\d{4}-\d{2}$")) -> dict:
    """当月資金、候補台、根拠ルールをまとめて返す。"""
    return get_opportunity_dashboard(month)


@router.post("/api/opportunity/quick-assess", tags=["opportunity"])
def opportunity_quick_assess(body: OpportunityQuickAssess) -> dict:
    profile = get_opportunity_profile(body.profile_id)
    if not profile:
        raise HTTPException(404, "狙い目ルールが見つかりません")
    if profile["machine_name"] != body.machine_name:
        raise HTTPException(422, "機種と狙い目ルールが一致しません")
    summary = get_opportunity_budget_summary(body.month)
    assessment = assess_quick_decision(
        profile=profile,
        current_value=body.current_value,
        risk_capacity_yen=summary["risk_capacity_yen"],
        exchange_type=body.exchange_type,
        funding_mode=body.funding_mode,
        reset_status=body.reset_status,
        minutes_until_close=body.minutes_until_close,
    )
    return {
        **assessment,
        "profile_id": profile["id"],
        "machine_name": profile["machine_name"],
        "current_value": body.current_value,
        "exchange_type": body.exchange_type,
        "funding_mode": body.funding_mode,
        "reset_status": body.reset_status,
        "condition_label": profile["condition_label"],
        "metric_name": profile["metric_name"],
        "unit_label": profile["unit_label"],
        "start_threshold": profile["start_threshold"],
        "ceiling_threshold": profile.get("ceiling_threshold"),
        "stop_rule": profile["stop_rule"],
        "source_name": profile["source_name"],
        "verified_on": profile.get("verified_on"),
        "discrepancy_note": profile.get("discrepancy_note", ""),
        "risk_capacity_yen": summary["risk_capacity_yen"],
    }


@router.post("/api/opportunity/profiles", tags=["opportunity"])
def create_opportunity_profile(body: OpportunityProfileCreate) -> dict:
    if body.ceiling_threshold is not None and body.ceiling_threshold < body.start_threshold:
        raise HTTPException(422, "天井値は開始ライン以上にしてください")
    return save_opportunity_profile(body.model_dump())


@router.delete("/api/opportunity/profiles/{profile_id}", tags=["opportunity"])
def delete_opportunity_profile(profile_id: int) -> dict:
    if not get_opportunity_profile(profile_id):
        raise HTTPException(404, "狙い目ルールが見つかりません")
    deactivate_opportunity_profile(profile_id)
    return {"ok": True}


@router.post("/api/opportunity/candidates", tags=["opportunity"])
def create_opportunity_candidate(body: OpportunityCandidateCreate) -> dict:
    if body.profile_id:
        profile = get_opportunity_profile(body.profile_id)
        if not profile:
            raise HTTPException(404, "狙い目ルールが見つかりません")
        if profile["machine_name"] != body.machine_name:
            raise HTTPException(422, "機種と狙い目ルールが一致しません")
    return save_opportunity_candidate(body.model_dump())


@router.patch("/api/opportunity/candidates/{candidate_id}", tags=["opportunity"])
def update_opportunity_candidate(candidate_id: int, body: OpportunityCandidateStatus) -> dict:
    if not set_opportunity_candidate_status(candidate_id, body.status):
        raise HTTPException(404, "候補台が見つかりません")
    return {"ok": True}


@router.post("/api/opportunity/candidates/{candidate_id}/result", tags=["opportunity"])
def create_opportunity_result(candidate_id: int, body: OpportunityResultCreate) -> dict:
    try:
        return save_opportunity_result(candidate_id, body.model_dump())
    except KeyError:
        raise HTTPException(404, "候補台が見つかりません")
    except sqlite3.IntegrityError:
        raise HTTPException(409, "この候補台には実戦結果が登録済みです")


@router.put("/api/opportunity/budget", tags=["opportunity"])
def update_opportunity_budget(body: OpportunityBudgetUpdate) -> dict:
    if body.loss_limit_yen > body.starting_bankroll:
        raise HTTPException(422, "月間損失上限は運用資金以下にしてください")
    return save_opportunity_budget(body.month, body.starting_bankroll, body.loss_limit_yen)

