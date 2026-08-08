"""収支・履歴セッションエンドポイント"""
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
from records.models import (
    Session,
    delete_session,
    get_session,
    list_halls,
    list_sessions,
    save_session,
    session_to_dict,
    update_session,
)

router = APIRouter()

class SessionCreate(BaseModel):
    date: str = Field(default_factory=lambda: date.today().isoformat())
    machine_name: str
    hall_name: str = ""
    seat_number: Optional[int] = Field(default=None, ge=1)
    is_corner: bool = False
    games_total: int = Field(default=0, ge=0)
    investment: int = Field(default=0, ge=0)
    returns: int = Field(default=0, ge=0)
    diff_coins: int = 0
    is_event_day: bool = False
    started_from: int = Field(default=0, ge=0)
    element_counts: dict[str, int] = Field(default_factory=dict)
    posterior: Optional[dict[str, float]] = None
    notes: str = ""

class SessionUpdate(BaseModel):
    games_total: Optional[int] = Field(default=None, ge=0)
    investment: Optional[int] = Field(default=None, ge=0)
    returns: Optional[int] = Field(default=None, ge=0)
    diff_coins: Optional[int] = None
    element_counts: Optional[dict[str, int]] = None
    posterior: Optional[dict[str, float]] = None
    notes: Optional[str] = None
    seat_number: Optional[int] = Field(default=None, ge=1)
    is_corner: Optional[bool] = None
    is_event_day: Optional[bool] = None

class CsvImportBody(BaseModel):
    csv_text: str  # UTF-8 CSVテキスト（BOM可）


@router.post("/api/sessions", tags=["sessions"])
def create_session(body: SessionCreate) -> dict:
    if body.started_from > body.games_total:
        raise HTTPException(422, "引き継ぎG数は総ゲーム数以下で指定してください")
    s = Session(
        date=body.date,
        machine_name=body.machine_name,
        hall_name=body.hall_name,
        seat_number=body.seat_number,
        is_corner=body.is_corner,
        games_total=body.games_total,
        investment=body.investment,
        returns=body.returns,
        diff_coins=body.diff_coins,
        is_event_day=body.is_event_day,
        started_from=body.started_from,
        posterior=body.posterior,
        element_counts=body.element_counts,
        notes=body.notes,
    )
    sid = save_session(s)
    _cache_invalidate_prefix("halls_list:")
    return {"id": sid, "message": "保存しました"}


@router.get("/api/sessions", tags=["sessions"])
def get_sessions(
    hall_name: Optional[str] = Query(None),
    machine_name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
) -> list[dict]:
    sessions = list_sessions(
        hall_name=hall_name,
        machine_name=machine_name,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return [session_to_dict(s) for s in sessions]


@router.get("/api/sessions/export", tags=["sessions"])
def export_sessions_csv_route(
    hall_name: Optional[str] = Query(None),
    machine_name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
) -> StreamingResponse:
    """セッション履歴をCSVでエクスポートする。"""
    sessions = list_sessions(
        hall_name=hall_name,
        machine_name=machine_name,
        date_from=date_from,
        date_to=date_to,
        limit=5000,
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "date", "hall_name", "machine_name", "seat_number", "is_corner",
        "games_total", "investment", "returns", "diff_yen", "diff_coins",
        "is_event_day", "started_from", "expected_setting", "high_setting_prob",
        "notes",
    ])
    for s in sessions:
        exp_setting = ""
        high_prob = ""
        if s.posterior:
            exp_setting = f"{sum(int(k)*v for k,v in s.posterior.items()):.2f}"
            high_prob = f"{sum(v for k,v in s.posterior.items() if int(k)>=4)*100:.1f}%"
        writer.writerow([
            s.id, s.date, s.hall_name, s.machine_name,
            s.seat_number or "", int(s.is_corner),
            s.games_total, s.investment, s.returns, s.diff_yen, s.diff_coins,
            int(s.is_event_day), s.started_from,
            exp_setting, high_prob, s.notes,
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sessions.csv"},
    )


@router.post("/api/sessions/import_csv", tags=["sessions"])
def import_sessions_csv(body: CsvImportBody) -> dict:
    """CSVテキストからセッションを一括インポートする。"""
    text = body.csv_text.lstrip("﻿").strip()  # BOM除去
    reader = csv.DictReader(io.StringIO(text))
    imported, skipped = 0, 0
    for row in reader:
        try:
            machine = row.get("machine_name", "").strip()
            if not machine:
                skipped += 1
                continue
            inv = int(row.get("investment") or 0)
            ret = int(row.get("returns") or 0)
            s = Session(
                date=row.get("date", date.today().isoformat()),
                machine_name=machine,
                hall_name=row.get("hall_name", ""),
                seat_number=int(row["seat_number"]) if row.get("seat_number") else None,
                is_corner=row.get("is_corner", "0") in ("1", "True", "true"),
                games_total=int(row.get("games_total") or 0),
                investment=inv,
                returns=ret,
                diff_coins=int(row.get("diff_coins") or 0),
                is_event_day=row.get("is_event_day", "0") in ("1", "True", "true"),
                started_from=int(row.get("started_from") or 0),
                notes=row.get("notes", ""),
            )
            save_session(s)
            imported += 1
        except Exception:
            skipped += 1
    if imported:
        _cache_invalidate_prefix("halls_list:")
    return {"imported": imported, "skipped": skipped}


@router.get("/api/sessions/estimation_accuracy", tags=["sessions"])
def get_estimation_accuracy(
    hall_name: Optional[str] = Query(None),
    limit: int = Query(100),
) -> dict:
    """
    推定設定 vs 実差枚の相関分析。
    推測エンジンの精度を評価し、「高設定推定時に実際に収益がプラスだった率」を返す。
    """
    from records.models import list_sessions
    sessions = list_sessions(hall_name=hall_name)
    if not sessions:
        return {"message": "セッションなし"}

    valid = []
    for s in sessions[-limit:]:
        if s.posterior is None or s.diff_coins is None:
            continue
        try:
            post = json.loads(s.posterior) if isinstance(s.posterior, str) else s.posterior
            if not post:
                continue
            exp_s = sum(float(k) * v for k, v in post.items())
            high_p = sum(v for k, v in post.items() if float(k) >= 4)
            valid.append({
                "expected_setting": exp_s,
                "high_prob": high_p,
                "diff_coins": s.diff_coins,
                "games": s.games_total,
                "is_positive": s.diff_coins > 0,
            })
        except Exception:
            continue

    if not valid:
        return {"message": "推測データ付きセッションなし"}

    # 高設定推定（≥4）時の勝率
    high_est = [v for v in valid if v["expected_setting"] >= 4.0]
    low_est  = [v for v in valid if v["expected_setting"] < 3.0]
    high_est_winrate = sum(1 for v in high_est if v["is_positive"]) / len(high_est) if high_est else None
    low_est_winrate  = sum(1 for v in low_est if v["is_positive"]) / len(low_est) if low_est else None

    # 高設定確率別の勝率区分
    brackets = []
    for lo, hi in [(0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.0)]:
        grp = [v for v in valid if lo <= v["high_prob"] < hi]
        if grp:
            wr = sum(1 for v in grp if v["is_positive"]) / len(grp)
            avg_diff = sum(v["diff_coins"] for v in grp) / len(grp)
            brackets.append({
                "bracket": f"高設定確率{int(lo*100)}~{int(hi*100)}%",
                "count": len(grp),
                "win_rate": round(wr * 100, 1),
                "avg_diff": round(avg_diff),
            })

    # 期待設定との相関（単純な方向性）
    correct_direction = sum(
        1 for v in valid
        if (v["expected_setting"] >= 4 and v["diff_coins"] > 0) or
           (v["expected_setting"] < 3 and v["diff_coins"] <= 0)
    )
    direction_accuracy = correct_direction / len(valid) if valid else 0

    return {
        "total_sessions_analyzed": len(valid),
        "overall_win_rate": round(sum(1 for v in valid if v["is_positive"]) / len(valid) * 100, 1),
        "high_setting_est_sessions": len(high_est),
        "high_setting_est_win_rate": round(high_est_winrate * 100, 1) if high_est_winrate is not None else None,
        "low_setting_est_sessions": len(low_est),
        "low_setting_est_win_rate": round(low_est_winrate * 100, 1) if low_est_winrate is not None else None,
        "direction_accuracy": round(direction_accuracy * 100, 1),
        "high_prob_brackets": brackets,
    }


@router.get("/api/sessions/{session_id}", tags=["sessions"])
def get_session_endpoint(session_id: int) -> dict:
    s = get_session(session_id)
    if not s:
        raise HTTPException(404, "セッションが見つかりません")
    return session_to_dict(s)


@router.put("/api/sessions/{session_id}", tags=["sessions"])
def update_session_endpoint(session_id: int, body: SessionUpdate) -> dict:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "更新内容がありません")
    update_session(session_id, **updates)
    s = get_session(session_id)
    if not s:
        raise HTTPException(404, "セッションが見つかりません")
    return session_to_dict(s)


@router.delete("/api/sessions/{session_id}", tags=["sessions"])
def delete_session_endpoint(session_id: int) -> dict:
    if not delete_session(session_id):
        raise HTTPException(404, "セッションが見つかりません")
    return {"deleted": True}


@router.get("/api/halls", tags=["sessions"])
def get_halls() -> list[str]:
    ckey = f"halls_list:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    result = list_halls()
    _cache_set(ckey, result)
    return result

