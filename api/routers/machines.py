"""機種マスタ関連エンドポイント"""
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
from core.bayes_engine import MachineProfile

router = APIRouter()

@router.get("/api/machines", tags=["machines"])
def list_machines() -> list[str]:
    """保存済み機種の一覧を返す。"""
    ckey = f"machines_list:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    result = sorted(
        p.stem for p in MACHINES_DIR.glob("*.json")
        if p.stem and p.stem != ""
    )
    _cache_set(ckey, result)
    return result


@router.get("/api/machines/{machine_name}", tags=["machines"])
def get_machine(machine_name: str) -> dict:
    """機種データ（確率テーブル・機械割）を返す。"""
    ckey = f"machine_data:{machine_name}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    path = _get_machine_path(machine_name)
    if not path.exists():
        raise HTTPException(404, f"機種が見つかりません: {machine_name}")
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    # API表示側も、コアローダーが補正した確率を返す。
    profile = MachineProfile.from_dict(result)
    normalized = {el.name: el.probabilities for el in profile.elements}
    for element in result.get("elements", []):
        if element.get("name") in normalized:
            element["p"] = dict(normalized[element["name"]])
            element.pop("one_over", None)
    _cache_set(ckey, result)
    return result

