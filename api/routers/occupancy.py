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
from hall.occupancy import get_patrol_list, record_occupancy

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
) -> list[dict]:
    """巡回優先度順のホール一覧を返す(未記録・記録が古い・直近highのホールほど上位)。"""
    return get_patrol_list(hall_names=hall_name)
