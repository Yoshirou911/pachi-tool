"""運用系(キャッシュ・統計)エンドポイント"""
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
    _CACHE,
    _CACHE_LOCK,
    _CACHE_TTL,
    _cache_get,
    _cache_invalidate_prefix,
    _cache_set,
    _get_event_conn,
    _get_machine_path,
    _get_reports_conn,
    logger,
)

router = APIRouter()

@router.get("/api/stats", tags=["meta"])
def get_stats() -> dict:
    """DBデータ量サマリー"""
    result: dict = {}
    conn = _get_reports_conn()
    if conn:
        try:
            result["minrepo_days"] = conn.execute(
                "SELECT COUNT(DISTINCT report_date) FROM hall_day_machine").fetchone()[0]
            result["minrepo_halls"] = conn.execute(
                "SELECT COUNT(DISTINCT hall_name) FROM hall_day_machine").fetchone()[0]
            result["minrepo_records"] = conn.execute(
                "SELECT COUNT(*) FROM hall_day_machine").fetchone()[0]
            result["anaslo_days"] = conn.execute(
                "SELECT COUNT(DISTINCT report_date) FROM hall_day_seat").fetchone()[0]
            result["latest_date"] = conn.execute(
                "SELECT MAX(report_date) FROM hall_day_machine").fetchone()[0]
        except Exception:
            pass
        conn.close()
    econn = _get_event_conn()
    if econn:
        try:
            result["event_count"] = econn.execute(
                "SELECT COUNT(*) FROM hall_event").fetchone()[0]
            result["event_halls"] = econn.execute(
                "SELECT COUNT(DISTINCT hall_name) FROM hall_event").fetchone()[0]
        except Exception:
            pass
        econn.close()
    return result


@router.post("/api/cache/clear", tags=["admin"])
def clear_cache(hall_name: Optional[str] = Query(None)) -> dict:
    """インメモリキャッシュを消去する。hall_name 指定でそのホールのみ。"""
    with _CACHE_LOCK:
        if hall_name:
            keys = [k for k in _CACHE if hall_name in k]
            for k in keys:
                del _CACHE[k]
            cleared = len(keys)
        else:
            cleared = len(_CACHE)
            _CACHE.clear()
    return {"cleared": cleared, "message": f"{cleared}件のキャッシュを削除しました"}


@router.get("/api/cache/stats", tags=["admin"])
def cache_stats() -> dict:
    """キャッシュの統計情報を返す。"""
    import time as _t
    now = _t.time()
    with _CACHE_LOCK:
        entries = [(k, now - v[0]) for k, v in _CACHE.items()]
    return {
        "total_entries": len(entries),
        "ttl_seconds": _CACHE_TTL,
        "entries": [{"key": k, "age_seconds": round(a)} for k, a in sorted(entries, key=lambda x: x[1])]
    }

