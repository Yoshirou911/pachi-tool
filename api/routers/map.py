"""ホールマップエンドポイント"""
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

router = APIRouter()

import urllib.parse
import urllib.request

_COORDS_FILE = Path(__file__).parent.parent.parent / "data" / "hall_coords.json"


def _load_coords() -> dict:
    if _COORDS_FILE.exists():
        return json.loads(_COORDS_FILE.read_text(encoding="utf-8"))
    return {}


def _save_coords(cache: dict):
    _COORDS_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _geocode(hall_name: str) -> Optional[list]:
    cache = _load_coords()
    if hall_name in cache:
        return cache[hall_name]
    query = f"{hall_name} 大阪府"
    url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(query)}&format=json&limit=1&countrycodes=jp"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pachi-tool/1.0 (local research)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        if data:
            coords = [float(data[0]["lat"]), float(data[0]["lon"])]
            cache[hall_name] = coords
            _save_coords(cache)
            _time.sleep(1.1)
            return coords
    except Exception:
        pass
    return None


@router.get("/api/map/halls", tags=["map"])
def get_map_halls(days: int = Query(30)) -> list[dict]:
    """マップ用ホール強度データ（差枚スコアで色分け）"""
    conn = _get_reports_conn()
    if not conn:
        return []

    # アナスロデータ優先、なければみんレポで補完
    rows = []
    try:
        rows = conn.execute("""
            SELECT hall_name,
                   AVG(diff_coins) AS avg_diff,
                   COUNT(DISTINCT report_date) AS days_cnt,
                   SUM(CASE WHEN diff_coins > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS win_rate
            FROM hall_day_seat
            WHERE bb_prob IS NOT NULL
              AND report_date >= date('now', '-' || ? || ' days')
              AND machine_name NOT LIKE '末尾%' AND machine_name != '全データ一覧'
            GROUP BY hall_name
            HAVING days_cnt >= 1
            ORDER BY avg_diff DESC
        """, (days,)).fetchall()
    except Exception:
        pass

    seat_halls = {r[0] for r in rows}
    try:
        mr_rows = conn.execute("""
            SELECT hall_name,
                   AVG(avg_diff_coins) AS avg_diff,
                   COUNT(DISTINCT report_date) AS days_cnt,
                   SUM(CASE WHEN avg_diff_coins > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS win_rate
            FROM hall_day_machine
            WHERE report_date >= date('now', '-' || ? || ' days')
              AND avg_diff_coins IS NOT NULL
            GROUP BY hall_name
            HAVING days_cnt >= 1
            ORDER BY avg_diff DESC
        """, (days,)).fetchall()
        for r in mr_rows:
            if r[0] not in seat_halls:
                rows.append(r)
    except Exception:
        pass

    conn.close()

    if not rows:
        return []

    diffs = [r[1] or 0 for r in rows]
    mn, mx = min(diffs), max(diffs)
    rng = max(mx - mn, 1)

    result = []
    for r in rows:
        coords = _geocode(r[0])
        if not coords:
            continue
        score = (r[1] - mn) / rng  # 0.0（弱）〜 1.0（強）
        # 赤(強) → 黄 → 緑(弱)
        if score >= 0.7:
            color = "#e53e3e"
        elif score >= 0.4:
            color = "#dd8800"
        elif score >= 0.2:
            color = "#c8b800"
        else:
            color = "#38a169"
        result.append({
            "hall_name": r[0],
            "lat": coords[0],
            "lng": coords[1],
            "avg_diff": round(r[1] or 0),
            "win_rate": round(r[3] or 0, 1),
            "days_cnt": r[2],
            "score": round(score, 3),
            "color": color,
        })
    return result

