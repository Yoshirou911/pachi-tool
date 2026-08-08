"""イベント情報エンドポイント (/api/events/*)"""
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
from api import scheduler

router = APIRouter()

_EVENT_PROGRESS: dict = {
    "running": False,
    "started_at": None,
    "halls": [],   # [{name, status, found, by_source}]
}


@router.get("/api/events/calendar", tags=["events"])
def get_event_calendar(
    month: str = Query(..., description="YYYY-MM"),
    hall_name: Optional[str] = Query(None),
) -> dict:
    """月次カレンダー用イベントデータ。日付→イベントリストのマップを返す。"""
    ckey = f"event_calendar:{month}:{hall_name}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    try:
        conn = _get_event_conn()
        q = "SELECT id, hall_name, event_date, event_type, event_title, source, source_url FROM hall_event WHERE event_date LIKE ?"
        params: list = [f"{month}%"]
        if hall_name:
            q += " AND hall_name=?"
            params.append(hall_name)
        q += " ORDER BY event_date, hall_name"
        rows = conn.execute(q, params).fetchall()
        conn.close()

        by_date: dict = {}
        for r in rows:
            d = r["event_date"]
            by_date.setdefault(d, []).append({
                "id": r["id"],
                "hall_name": r["hall_name"],
                "event_type": r["event_type"],
                "event_title": r["event_title"],
                "source": r["source"],
                "source_url": r["source_url"],
            })
        result = {"month": month, "events": by_date}
        _cache_set(ckey, result)
        return result
    except Exception as e:
        return {"month": month, "events": {}, "error": str(e)}


@router.get("/api/events/day", tags=["events"])
def get_event_day(
    date_str: str = Query(..., description="YYYY-MM-DD"),
    hall_name: Optional[str] = Query(None),
) -> dict:
    """特定日のイベント＋みんレポ実績データを返す"""
    ckey = f"event_day_v2:{date_str}:{hall_name}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    try:
        conn = _get_event_conn()
        q = "SELECT id, hall_name, event_type, event_title, source, source_url FROM hall_event WHERE event_date=?"
        params: list = [date_str]
        if hall_name:
            q += " AND hall_name=?"
            params.append(hall_name)
        events = [dict(r) for r in conn.execute(q, params).fetchall()]
        conn.close()

        # みんレポ実績 (hall_day_machine)
        results = []
        rconn = _get_reports_conn()
        if rconn:
            try:
                rq = "SELECT hall_name, machine_name, avg_diff_coins, unit_count FROM hall_day_machine WHERE report_date=?"
                rparams: list = [date_str]
                if hall_name:
                    rq += " AND hall_name=?"
                    rparams.append(hall_name)
                rq += " ORDER BY avg_diff_coins DESC"
                results = [dict(r) for r in rconn.execute(rq, rparams).fetchall()]
            except Exception:
                pass
            rconn.close()

        result = {"date": date_str, "events": events, "results": results}
        _cache_set(ckey, result)
        return result
    except Exception as e:
        return {"date": date_str, "events": [], "results": [], "error": str(e)}


@router.get("/api/events/strength", tags=["events"])
def get_event_strength(hall_name: Optional[str] = Query(None)) -> list[dict]:
    """イベントタイプ別の強度分析（イベント日 vs 通常日の差枚比較）"""
    try:
        rconn = _get_reports_conn()
        econn = _get_event_conn()
        if not rconn:
            return []

        # イベントがある日付を全取得
        eq = "SELECT hall_name, event_date, event_type FROM hall_event"
        eparams: list = []
        if hall_name:
            eq += " WHERE hall_name=?"
            eparams.append(hall_name)
        event_rows = econn.execute(eq, eparams).fetchall()
        econn.close()

        if not event_rows:
            rconn.close()
            return []

        from collections import defaultdict
        type_data: dict = defaultdict(lambda: {"event_diffs": [], "normal_diffs": []})

        # hall_day_machineから全日付の平均差枚を取得
        rq = "SELECT hall_name, report_date, AVG(avg_diff_coins) as avg_diff FROM hall_day_machine WHERE avg_diff_coins IS NOT NULL GROUP BY hall_name, report_date"
        rparams2: list = []
        if hall_name:
            rq += " HAVING hall_name=?"
            rparams2.append(hall_name)
        day_avgs = {(r[0], r[1]): r[2] for r in rconn.execute(rq, rparams2).fetchall()}
        rconn.close()

        event_days: set = set()
        for er in event_rows:
            key = (er["hall_name"], er["event_date"])
            event_days.add(key)
            if key in day_avgs:
                type_data[er["event_type"]]["event_diffs"].append(day_avgs[key])

        # 通常日（イベントなし日）
        for (hname, rdate), avg in day_avgs.items():
            if (hname, rdate) not in event_days:
                for et in type_data:
                    type_data[et]["normal_diffs"].append(avg)

        result = []
        for etype, data in type_data.items():
            ev_diffs = data["event_diffs"]
            no_diffs = data["normal_diffs"]
            if not ev_diffs:
                continue
            avg_ev = sum(ev_diffs) / len(ev_diffs)
            avg_no = sum(no_diffs) / len(no_diffs) if no_diffs else 0
            result.append({
                "event_type": etype,
                "event_days": len(ev_diffs),
                "avg_diff_event": round(avg_ev),
                "avg_diff_normal": round(avg_no),
                "diff_vs_normal": round(avg_ev - avg_no),
                "win_rate_event": round(sum(1 for v in ev_diffs if v > 0) / len(ev_diffs) * 100),
                "strength_score": round((avg_ev - avg_no) / max(abs(avg_no), 1) * 100),
            })
        result.sort(key=lambda x: x["diff_vs_normal"], reverse=True)
        return result
    except Exception as e:
        return [{"error": str(e)}]


@router.get("/api/events/scrape_status", tags=["events"])
def get_event_scrape_status() -> dict:
    """イベントスクレイプの進捗を返す"""
    halls = _EVENT_PROGRESS.get("halls", [])
    done = sum(1 for h in halls if h["status"] == "done")
    failed = sum(1 for h in halls if h["status"] == "failed")
    total = len(halls)
    current = next((h["name"] for h in halls if h["status"] == "running"), None)

    elapsed = 0
    if _EVENT_PROGRESS.get("started_at"):
        import datetime as _dt3
        try:
            elapsed = (_dt3.datetime.now() - _dt3.datetime.fromisoformat(
                _EVENT_PROGRESS["started_at"]
            )).total_seconds()
        except Exception:
            pass

    finished = done + failed
    eta_min = 0
    if finished > 0 and total > finished and elapsed > 0:
        eta_min = round(elapsed / finished * (total - finished) / 60)

    return {
        "running": _EVENT_PROGRESS.get("running", False),
        "total": total,
        "done": done,
        "failed": failed,
        "current_hall": current,
        "eta_min": eta_min,
        "halls": halls,
    }


@router.post("/api/events/scrape", tags=["events"])
def trigger_event_scrape(
    hall_name: Optional[str] = Query(None, description="Noneなら全ホール"),
    background_tasks: BackgroundTasks = ...,
) -> dict:
    """イベントスクレイプをバックグラウンドで実行"""
    if _EVENT_PROGRESS.get("running"):
        return {"ok": False, "message": "すでに実行中です"}

    import datetime as _dt_ev
    halls = [{"hall_name": hall_name}] if hall_name else scheduler._get_active_halls()
    hall_names = [h["hall_name"] if isinstance(h, dict) else h for h in halls]

    _EVENT_PROGRESS.update({
        "running": True,
        "started_at": _dt_ev.datetime.now().isoformat(),
        "halls": [{"name": n, "status": "waiting", "found": 0, "by_source": {}} for n in hall_names],
    })

    def _set_ev_hall(name: str, status: str, found: int = 0, by_source: dict = {}):
        for h in _EVENT_PROGRESS["halls"]:
            if h["name"] == name:
                h["status"] = status
                if found:
                    h["found"] = found
                if by_source:
                    h["by_source"] = by_source
                break

    def _run():
        from scraper.events import scrape_all
        try:
            for hname in hall_names:
                _set_ev_hall(hname, "running")
                try:
                    result = scrape_all(hname, save=True)
                    _set_ev_hall(hname, "done",
                                 found=result.get("total", 0),
                                 by_source=result.get("by_source", {}))
                except Exception as e:
                    _set_ev_hall(hname, "failed", by_source={"error": str(e)[:60]})
                time.sleep(2)
        finally:
            _EVENT_PROGRESS["running"] = False

    background_tasks.add_task(_run)
    return {"ok": True, "message": f"{len(hall_names)}店舗のイベント取得を開始しました"}

@router.post("/api/events/manual", tags=["events"])
def add_manual_event(
    hall_name: str = Query(...),
    event_date: str = Query(..., description="YYYY-MM-DD"),
    event_type: str = Query("その他"),
    event_title: str = Query(""),
) -> dict:
    """手動でイベントを登録"""
    try:
        conn = _get_event_conn()
        conn.execute("""
            INSERT OR IGNORE INTO hall_event (hall_name, event_date, event_type, event_title, source)
            VALUES (?, ?, ?, ?, 'manual')
        """, (hall_name, event_date, event_type, event_title))
        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/events/debug_scrape", tags=["events"])
def debug_event_scrape(
    hall_name: str = Query(...),
    source: str = Query("dste", description="dste | pworld | twitter | google"),
) -> dict:
    """スクレーパーのデバッグ: 何が取れているか確認用"""
    import traceback
    result = {"hall_name": hall_name, "source": source, "events": [], "debug": {}}
    try:
        import requests as _req, urllib.parse as _up
        from scraper.events import HEADERS, NITTER_INSTANCES, _get

        if source == "dste":
            from scraper.events import _dste_search, scrape_dste
            search_url = f"https://dste.jp/search/?q={_up.quote(hall_name)}"
            r0 = _get(search_url)
            result["debug"]["search_url"] = search_url
            result["debug"]["search_status"] = r0.status_code if r0 else "failed"
            result["debug"]["search_html"] = r0.text[:3000] if r0 else ""
            if r0:
                from bs4 import BeautifulSoup as _BS
                soup = _BS(r0.text, "html.parser")
                result["debug"]["all_links"] = [a.get("href","") for a in soup.select("a[href]")][:40]
            hall_url = _dste_search(hall_name)
            result["debug"]["hall_url"] = hall_url
            evs = scrape_dste(hall_name)
            result["events"] = evs

        elif source == "pworld":
            from scraper.events import _pworld_search, scrape_pworld
            hall_url = _pworld_search(hall_name)
            result["debug"]["hall_url"] = hall_url
            evs = scrape_pworld(hall_name)
            result["events"] = evs

        elif source == "twitter":
            result["debug"]["nitter_instances"] = NITTER_INSTANCES
            query = _up.quote(f"{hall_name} イベント")
            for inst in NITTER_INSTANCES[:3]:
                url = f"{inst}/search?q={query}&f=tweets"
                r = _get(url, timeout=10)
                result["debug"][f"{inst}_status"] = r.status_code if r else "failed"
                result["debug"][f"{inst}_html"] = r.text[:800] if r else ""
            from scraper.events import scrape_twitter
            evs = scrape_twitter(hall_name)
            result["events"] = evs

        elif source == "google":
            from scraper.events import scrape_google
            evs = scrape_google(hall_name)
            result["events"] = evs
    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()[-500:]
    return result


@router.delete("/api/events/{event_id}", tags=["events"])
def delete_event(event_id: int) -> dict:
    """イベントを削除"""
    try:
        conn = _get_event_conn()
        conn.execute("DELETE FROM hall_event WHERE id=?", (event_id,))
        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

