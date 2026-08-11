"""店舗稼働率トラッキングエンドポイント (/api/occupancy/*)"""
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
from hall.occupancy import (
    get_occupancy_statistics,
    get_patrol_list,
    rank_hyena_halls,
    record_occupancy,
)

router = APIRouter()


class OccupancyRecordCreate(BaseModel):
    hall_name: str
    level: Literal["high", "mid", "low"]
    avg_rotation_games_per_hour: Optional[float] = Field(default=None, ge=0)
    recorded_at: Optional[str] = None


@router.post("/api/occupancy", tags=["occupancy"])
def create_occupancy_record(body: OccupancyRecordCreate) -> dict:
    """ワンタップ記録: 店舗の稼働状況(高/中/低)を1件保存する。"""
    try:
        return record_occupancy(
            hall_name=body.hall_name,
            level=body.level,
            avg_rotation_games_per_hour=body.avg_rotation_games_per_hour,
            recorded_at=body.recorded_at,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/api/occupancy/patrol-list", tags=["occupancy"])
def occupancy_patrol_list(
    hall_name: Optional[list[str]] = Query(default=None, description="絞り込むホール名(複数可)。省略時は有効ホール全件"),
    prefecture: Optional[str] = Query(default=None, description="都道府県で絞り込み"),
) -> list[dict]:
    """巡回優先度順のホール一覧を返す(未記録・記録が古い・直近highのホールほど上位)。"""
    return get_patrol_list(hall_names=hall_name, prefecture=prefecture)


@router.get("/api/occupancy/statistics", tags=["occupancy"])
def occupancy_statistics(
    hall_name: str = Query(..., min_length=1, max_length=120),
    at: Optional[str] = Query(default=None, description="判定日時 ISO8601。省略時は現在"),
    days: int = Query(default=90, ge=7, le=365),
) -> dict:
    """店舗×曜日×時間帯の手動稼働記録を集計する。"""
    try:
        return get_occupancy_statistics(hall_name, at, days)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/api/occupancy/hyena-stores", tags=["occupancy"])
def hyena_store_ranking(
    at: Optional[str] = Query(default=None, description="巡回予定日時 ISO8601。省略時は現在"),
    prefecture: Optional[str] = Query(default=None, description="都道府県で絞り込み"),
    hall_name: Optional[list[str]] = Query(default=None),
    limit: int = Query(default=10, ge=1, le=30),
) -> dict:
    """今からハイエナ巡回する候補店舗を、根拠・信頼度付きで順位付けする。"""
    try:
        return rank_hyena_halls(
            target_at=at,
            hall_names=hall_name,
            limit=limit,
            prefecture=prefecture,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
