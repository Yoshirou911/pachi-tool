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


@router.get("/api/map/target_heat", tags=["map"])
def get_target_heat_map(
    visit_date: str = Query(..., description="YYYY-MM-DD"),
    days: int = Query(120, ge=14, le=365),
    long_days: int = Query(365, ge=30, le=730),
) -> dict:
    """指定日の店舗熱量と、店舗ごとの月次・曜日別長期傾向を返す。"""
    try:
        target_date = date.fromisoformat(visit_date)
    except ValueError as exc:
        raise HTTPException(400, "visit_date は YYYY-MM-DD で指定してください") from exc

    # 循環importを避けるためリクエスト時に読み込む。
    from api.routers.hall import get_target_search

    search = get_target_search(visit_date=visit_date, days=days, limit=20)
    coords = _load_coords()
    reference_date = min(target_date, date.today())
    start_date = reference_date - timedelta(days=long_days)
    conn = _get_reports_conn()
    monthly_by_hall: dict[str, list[dict]] = {}
    weekday_by_hall: dict[str, list[dict]] = {}
    long_summary: dict[str, dict] = {}
    if conn is not None:
        try:
            monthly_rows = conn.execute(
                """SELECT hall_name, substr(report_date,1,7) AS month,
                          ROUND(AVG(avg_diff_coins)) AS avg_diff,
                          COUNT(DISTINCT report_date) AS sample_days
                   FROM hall_day_machine
                   WHERE report_date BETWEEN ? AND ?
                     AND machine_name != '_NODATA_' AND avg_diff_coins IS NOT NULL
                   GROUP BY hall_name, month ORDER BY hall_name, month""",
                (start_date.isoformat(), reference_date.isoformat()),
            ).fetchall()
            for row in monthly_rows:
                monthly_by_hall.setdefault(row[0], []).append({
                    "month": row[1], "avg_diff": int(row[2] or 0), "sample_days": row[3],
                })

            weekday_rows = conn.execute(
                """SELECT hall_name, strftime('%w', report_date) AS dow,
                          ROUND(AVG(avg_diff_coins)) AS avg_diff,
                          COUNT(DISTINCT report_date) AS sample_days
                   FROM hall_day_machine
                   WHERE report_date BETWEEN ? AND ?
                     AND machine_name != '_NODATA_' AND avg_diff_coins IS NOT NULL
                   GROUP BY hall_name, dow ORDER BY hall_name, dow""",
                (start_date.isoformat(), reference_date.isoformat()),
            ).fetchall()
            dow_names = {"0": "日", "1": "月", "2": "火", "3": "水", "4": "木", "5": "金", "6": "土"}
            for row in weekday_rows:
                weekday_by_hall.setdefault(row[0], []).append({
                    "weekday": dow_names.get(row[1], row[1]),
                    "avg_diff": int(row[2] or 0),
                    "sample_days": row[3],
                })

            summary_rows = conn.execute(
                """SELECT hall_name, ROUND(AVG(avg_diff_coins)) AS avg_diff,
                          COUNT(DISTINCT report_date) AS sample_days,
                          MIN(report_date), MAX(report_date)
                   FROM hall_day_machine
                   WHERE report_date BETWEEN ? AND ?
                     AND machine_name != '_NODATA_' AND avg_diff_coins IS NOT NULL
                   GROUP BY hall_name""",
                (start_date.isoformat(), reference_date.isoformat()),
            ).fetchall()
            long_summary = {
                row[0]: {
                    "avg_diff": int(row[1] or 0), "sample_days": row[2],
                    "first_date": row[3], "latest_date": row[4],
                }
                for row in summary_rows
            }
        finally:
            conn.close()

    ranked = {item["hall_name"]: item for item in search["halls"]}
    insufficient = {item["hall_name"]: item for item in search["insufficient_halls"]}
    hall_names = list(dict.fromkeys([*ranked.keys(), *insufficient.keys()]))
    halls = []
    for hall_name in hall_names:
        point = coords.get(hall_name)
        if not point:
            continue
        item = ranked.get(hall_name)
        score = int(item["score"]) if item else 0
        if not item:
            heat_level, color = "データ不足", "#64748b"
        elif score >= 70:
            heat_level, color = "かなり熱い", "#f43f5e"
        elif score >= 60:
            heat_level, color = "熱い", "#f97316"
        elif score >= 50:
            heat_level, color = "注目", "#eab308"
        else:
            heat_level, color = "慎重", "#38bdf8"
        halls.append({
            "hall_name": hall_name,
            "lat": point[0],
            "lng": point[1],
            "score": score,
            "rank": item.get("rank") if item else None,
            "heat_level": heat_level,
            "color": color,
            "confidence": item.get("confidence", "不足") if item else "不足",
            "projected_diff": item.get("avg_diff") if item else None,
            "positive_rate": item.get("positive_rate") if item else None,
            "sample_days": item.get("sample_days", 0) if item else insufficient[hall_name]["sample_days"],
            "latest_date": item.get("latest_date", "") if item else "",
            "reasons": item.get("reasons", []) if item else [insufficient[hall_name]["reason"]],
            "target_machines": item.get("target_machines", []) if item else [],
            "long_term": long_summary.get(hall_name, {"avg_diff": 0, "sample_days": 0}),
            "monthly_trend": monthly_by_hall.get(hall_name, []),
            "weekday_profile": weekday_by_hall.get(hall_name, []),
        })
    halls.sort(key=lambda item: (item["score"], item["sample_days"]), reverse=True)
    return {
        "visit_date": visit_date,
        "weekday": search["weekday"],
        "analysis_days": days,
        "long_days": long_days,
        "generated_at": search["generated_at"],
        "center": {"lat": 34.724, "lng": 135.631},
        "halls": halls,
        "notice": search["notice"],
    }

