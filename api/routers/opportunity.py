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

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
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
    get_profile_calibration,
    get_profile as get_opportunity_profile,
    get_reset_tendency,
    load_mobile_sync,
    save_budget as save_opportunity_budget,
    save_candidate as save_opportunity_candidate,
    save_profile as save_opportunity_profile,
    save_result as save_opportunity_result,
    save_mobile_sync,
    set_candidate_status as set_opportunity_candidate_status,
)
from scraper.opportunity_crawler import (
    approve_candidate as approve_crawl_candidate,
    crawler_status,
    list_candidates as list_crawl_candidates,
    reject_candidate as reject_crawl_candidate,
    run_crawl,
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
    safe_play_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    duration_model: dict = Field(default_factory=dict)
    duration_basis: str = Field(default="", max_length=1000)
    duration_confidence: Literal["modeled", "verified", "legacy", "unknown"] = "unknown"
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
    input_fields: list[dict] = Field(default_factory=list, max_length=20)
    requirements: list[dict] = Field(default_factory=list, max_length=20)

class OpportunityCandidateCreate(BaseModel):
    machine_name: str = Field(min_length=1, max_length=120)
    hall_name: str = Field(default="", max_length=120)
    seat_number: Optional[int] = Field(default=None, ge=1)
    current_value: float = Field(ge=0)
    profile_id: Optional[int] = Field(default=None, ge=1)
    observed_at: Optional[str] = Field(default=None, max_length=30)
    notes: str = Field(default="", max_length=500)
    section_difference_coins: Optional[int] = Field(default=None, ge=-20000, le=20000)
    replay_limit_medals: int = Field(default=0, ge=0, le=10000)
    replay_used_medals: int = Field(default=0, ge=0, le=50000)
    exchange_rate: Optional[float] = Field(default=None, gt=5, le=20)
    extra_inputs: dict = Field(default_factory=dict)
    exchange_type: Literal["equivalent", "56", "other"] = "equivalent"
    funding_mode: Literal["cash", "medals"] = "cash"

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


class MobileSyncUpdate(BaseModel):
    state: dict


class CrawlReview(BaseModel):
    note: str = Field(default="", max_length=500)


def _checked_sync_key(value: str) -> str:
    key = value.strip()
    if len(key) < 32 or len(key) > 128 or not all(char.isalnum() or char in "-_" for char in key):
        raise HTTPException(422, "同期コードが不正です")
    return key


@router.put("/api/opportunity/sync", tags=["opportunity"])
def update_mobile_sync(body: MobileSyncUpdate, x_sync_key: str = Header(...)) -> dict:
    try:
        return save_mobile_sync(_checked_sync_key(x_sync_key), body.state)
    except ValueError as exc:
        raise HTTPException(413, str(exc))


@router.get("/api/opportunity/sync", tags=["opportunity"])
def read_mobile_sync(x_sync_key: str = Header(...)) -> dict:
    saved = load_mobile_sync(_checked_sync_key(x_sync_key))
    if not saved:
        raise HTTPException(404, "同期データがありません")
    return saved


@router.get("/api/opportunity/reset-tendency", tags=["opportunity"])
def opportunity_reset_tendency(
    hall_name: str = Query(..., min_length=1, max_length=120),
    machine_name: str = Query(default="", max_length=120),
    weekday: Optional[int] = Query(default=None, ge=0, le=6),
) -> dict:
    return get_reset_tendency(hall_name, machine_name, weekday)


@router.get("/api/opportunity/crawler/status", tags=["opportunity"])
def opportunity_crawler_status() -> dict:
    return crawler_status()


@router.get("/api/opportunity/crawler/candidates", tags=["opportunity"])
def opportunity_crawler_candidates(
    status: Optional[Literal["pending", "conflict", "approved", "rejected"]] = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    return list_crawl_candidates(status, limit)


@router.post("/api/opportunity/crawler/run", tags=["opportunity"])
def opportunity_crawler_run(
    background_tasks: BackgroundTasks,
    max_profiles: int = Query(default=100, ge=1, le=500),
) -> dict:
    background_tasks.add_task(run_crawl, max_profiles)
    return {"accepted": True, "message": "期待値ソース確認を開始しました"}


@router.post("/api/opportunity/crawler/candidates/{candidate_id}/approve", tags=["opportunity"])
def opportunity_crawler_approve(candidate_id: int, body: CrawlReview) -> dict:
    try:
        return approve_crawl_candidate(candidate_id, body.note)
    except KeyError:
        raise HTTPException(404, "更新候補が見つかりません")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/api/opportunity/crawler/candidates/{candidate_id}/reject", tags=["opportunity"])
def opportunity_crawler_reject(candidate_id: int, body: CrawlReview) -> dict:
    try:
        return reject_crawl_candidate(candidate_id, body.note)
    except KeyError:
        raise HTTPException(404, "未レビューの更新候補が見つかりません")


class OpportunityQuickAssess(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    machine_name: str = Field(min_length=1, max_length=120)
    profile_id: int = Field(ge=1)
    current_value: float = Field(ge=0)
    exchange_type: Literal["equivalent", "56", "other"]
    funding_mode: Literal["cash", "medals"]
    reset_status: Literal["normal", "reset_confirmed", "unknown"]
    minutes_until_close: int = Field(ge=0, le=1440)
    section_difference_coins: Optional[int] = Field(default=None, ge=-20000, le=20000)
    replay_limit_medals: int = Field(default=0, ge=0, le=10000)
    replay_used_medals: int = Field(default=0, ge=0, le=50000)
    exchange_rate: Optional[float] = Field(default=None, gt=5, le=20)
    extra_inputs: dict = Field(default_factory=dict)


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
        section_difference_coins=body.section_difference_coins,
        replay_limit_medals=body.replay_limit_medals,
        replay_used_medals=body.replay_used_medals,
        exchange_rate=body.exchange_rate,
        extra_inputs=body.extra_inputs,
    )
    calibration = get_profile_calibration(profile["id"])
    if calibration["active"] and assessment.get("expected_value_yen") is not None:
        before = int(assessment["expected_value_yen"])
        adjusted = round(before * calibration["factor"])
        assessment["expected_value_before_personal_calibration_yen"] = before
        assessment["expected_value_yen"] = adjusted
        if assessment.get("estimated_play_minutes"):
            assessment["ev_per_hour_yen"] = round(
                adjusted * 60 / int(assessment["estimated_play_minutes"])
            )
        assessment.setdefault("warnings", []).append(
            f"同じ条件{calibration['count']}件の実績から期待値を安全側{calibration['factor_pct']}%に補正"
        )
    assessment["personal_calibration"] = calibration
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
    data = body.model_dump()
    extra_inputs = data.pop("extra_inputs", {})
    data["context"] = {key: data.pop(key) for key in (
        "section_difference_coins", "replay_limit_medals", "replay_used_medals",
        "exchange_rate", "exchange_type", "funding_mode",
    )}
    data["context"].update(extra_inputs)
    return save_opportunity_candidate(data)


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

