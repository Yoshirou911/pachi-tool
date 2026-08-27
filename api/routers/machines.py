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
from hall.machine_scope import is_smartslot_machine

router = APIRouter()

@router.get("/api/machines", tags=["machines"])
def list_machines(scope: Literal["all", "smartslot", "live_setting"] = "all") -> list[str]:
    """保存済み機種の一覧を返す。"""
    ckey = f"machines_list:{scope}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    result = []
    for path in MACHINES_DIR.glob("*.json"):
        if not path.stem:
            continue
        if scope in {"smartslot", "live_setting"} and not is_smartslot_machine(path.stem):
            continue
        if scope == "live_setting":
            try:
                if not json.loads(path.read_text(encoding="utf-8-sig")).get("verified_for_live_setting"):
                    continue
            except (OSError, json.JSONDecodeError):
                continue
        result.append(path.stem)
    result.sort()
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

