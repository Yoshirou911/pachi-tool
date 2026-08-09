"""スクレイプ管理エンドポイント (/api/scrape/*)"""
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
from api.routers.hall import _run_scrape

router = APIRouter()


class CookieBody(BaseModel):
    cookie_str: str


_BULK_PROGRESS: dict = {
    "running": False,
    "started_at": None,
    "mode": "",
    "days": 0,
    "halls": [],   # [{name, status, records, error, sources}]
}


@router.post("/api/scrape/cookie", tags=["scrape"])
def set_scrape_cookie(body: CookieBody) -> dict:
    """
    ブラウザからコピーしたCookie文字列をサーバーに保存する。
    cf_clearanceが含まれていれば自動スクレイプで使用される。
    例: "cf_clearance=xxx; _ga=yyy"
    """
    try:
        from scraper.anaslo import save_cookie
        save_cookie(body.cookie_str)
        has_cf = "cf_clearance" in body.cookie_str
        return {
            "ok": True,
            "has_cf_clearance": has_cf,
            "message": "Cookie保存完了" + ("" if has_cf else " ※cf_clearanceが含まれていません"),
            "length": len(body.cookie_str),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/scrape/cookie_status", tags=["scrape"])
def get_cookie_status() -> dict:
    """保存済みCookieの状態を確認する"""
    try:
        import sqlite3 as _sq3
        from scraper.anaslo import get_cookie, DB_PATH
        ck = get_cookie()

        # 保存日時と経過時間
        saved_at = None
        age_hours = None
        try:
            conn2 = _sq3.connect(DB_PATH)
            row = conn2.execute(
                "SELECT updated_at FROM scrape_settings WHERE key='cf_cookie_str'"
            ).fetchone()
            conn2.close()
            if row and row[0]:
                saved_at = row[0]
                from datetime import datetime as _dt
                saved_dt = _dt.fromisoformat(saved_at)
                age_hours = round((_dt.now() - saved_dt).total_seconds() / 3600, 1)
        except Exception:
            pass

        # 直近のCFブロック記録（24時間以内）
        cf_blocked_halls: list[str] = []
        try:
            conn3 = _sq3.connect(DB_PATH)
            rows = conn3.execute(
                """SELECT hall_name FROM scrape_log
                   WHERE status='cf_blocked'
                     AND started_at >= datetime('now','-24 hours','localtime')
                   ORDER BY id DESC LIMIT 5"""
            ).fetchall()
            conn3.close()
            cf_blocked_halls = [r[0] for r in rows]
        except Exception:
            pass

        # curl_cffi が使えるか確認
        try:
            import curl_cffi  # noqa
            curl_cffi_available = True
        except ImportError:
            curl_cffi_available = False

        return {
            "has_cookie": bool(ck),
            "has_cf_clearance": "cf_clearance" in ck if ck else False,
            "curl_cffi_available": curl_cffi_available,
            "preview": ck[:60] + "..." if len(ck) > 60 else ck,
            "saved_at": saved_at,
            "age_hours": age_hours,
            "cf_blocked_halls": cf_blocked_halls,
        }
    except Exception as e:
        return {"has_cookie": False, "has_cf_clearance": False, "curl_cffi_available": False, "error": str(e)}


@router.post("/api/scrape/run", tags=["scrape"])
def trigger_nightly_scrape(
    background_tasks: BackgroundTasks,
    halls: Optional[str] = Query(None, description="カンマ区切りのホール名。省略時は全ホール"),
    days: int = Query(7, ge=1, le=90),
    minrepo_only: bool = Query(False, description="みんレポのみ実行（アナスロをスキップ）"),
) -> dict:
    """
    全店舗スクレイプを手動でトリガーする（バックグラウンド実行）。
    アナスロ → みんレポの順で実行。minrepo_only=trueでみんレポのみ。
    """
    if scheduler.is_scrape_running():
        return {"ok": False, "message": "すでにスクレイプ実行中です。完了後に再試行してください。"}

    if halls:
        hall_list = [{"hall_name": h.strip(), "prefecture": "大阪府"} for h in halls.split(",")]
    else:
        hall_list = scheduler._get_active_halls()

    import datetime as _dt_bulk

    hall_names = [h["hall_name"] if isinstance(h, dict) else h for h in hall_list]
    mode = "みんレポのみ" if minrepo_only else "アナスロ＋みんレポ"

    _BULK_PROGRESS.update({
        "running": True,
        "started_at": _dt_bulk.datetime.now().isoformat(),
        "mode": mode,
        "days": days,
        "halls": [
            {"name": n, "status": "waiting", "records": 0, "error": "", "sources": {}}
            for n in hall_names
        ],
    })

    def _set_hall(
        name: str,
        status: str,
        records: int = 0,
        error: str = "",
        source: str = "",
        source_status: str = "",
        source_records: int = 0,
    ) -> None:
        for h in _BULK_PROGRESS["halls"]:
            if h["name"] == name:
                h["status"] = status
                if records or status in {"done", "partial", "failed"}:
                    h["records"] = records
                if error:
                    h["error"] = error
                if source:
                    h.setdefault("sources", {})[source] = {
                        "status": source_status or status,
                        "records": source_records,
                    }
                break

    def _count_records(hall_name: str, table: str = "hall_day_machine") -> int:
        try:
            if table not in {"hall_day_machine", "hall_day_seat"}:
                return 0
            conn = _get_reports_conn()
            if not conn:
                return 0
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE hall_name=? AND machine_name != '_NODATA_'",
                (hall_name,),
            ).fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception:
            return 0

    def _run() -> None:
        scheduler.set_scrape_running(True)
        try:
            # ① アナスロ
            if not minrepo_only:
                try:
                    from scraper.anaslo import scrape_hall
                    for h in hall_list:
                        hname = h["hall_name"] if isinstance(h, dict) else h
                        pref = h.get("prefecture", "大阪府") if isinstance(h, dict) else "大阪府"
                        _set_hall(hname, "running")
                        try:
                            scrape_hall(hname, prefecture=pref, max_days=days)
                            seat_count = _count_records(hname, "hall_day_seat")
                            _set_hall(
                                hname,
                                "running",
                                source="seat",
                                source_status="done" if seat_count else "unavailable",
                                source_records=seat_count,
                            )
                        except Exception as e:
                            _set_hall(
                                hname,
                                "running",
                                error=str(e)[:120],
                                source="seat",
                                source_status="failed",
                            )
                        time.sleep(30)
                except Exception as e:
                    logger.warning(f"[アナスロ] 全体エラー: {e}")

            # ② みんレポ
            for h in hall_list:
                hname = h["hall_name"] if isinstance(h, dict) else h
                if not minrepo_only:
                    pass  # アナスロ済みステータスを上書きしない
                else:
                    _set_hall(hname, "running")
                try:
                    _run_scrape(hname, days=min(days, 30))
                    recs = _count_records(hname)
                    _set_hall(
                        hname,
                        "running",
                        records=recs,
                        source="machine",
                        source_status="done" if recs else "unavailable",
                        source_records=recs,
                    )
                except Exception as e:
                    _set_hall(
                        hname,
                        "running",
                        error=str(e)[:120],
                        source="machine",
                        source_status="failed",
                    )
                time.sleep(3)

                # ③ P-WORLDの公開店舗ページから現在の設置スマスロを保存。
                # 差枚ページがない店舗でも、分析対象となる機種構成は蓄積する。
                snapshot_count = 0
                snapshot_status = "unavailable"
                try:
                    from scraper.pworld_snapshot import scrape_snapshot, HALL_URLS
                    if hname in HALL_URLS:
                        snapshot_count = scrape_snapshot(hname)
                        snapshot_status = "done" if snapshot_count else "unavailable"
                except Exception as e:
                    snapshot_status = "failed"
                    _set_hall(hname, "running", error=str(e)[:120])

                hall_progress = next(
                    (row for row in _BULK_PROGRESS["halls"] if row["name"] == hname),
                    None,
                )
                source_states = list((hall_progress or {}).get("sources", {}).values())
                source_states.append({"status": snapshot_status, "records": snapshot_count})
                successful_sources = sum(1 for item in source_states if item["status"] == "done")
                failed_sources = sum(1 for item in source_states if item["status"] == "failed")
                final_status = "done" if successful_sources >= 2 else "partial" if successful_sources else "failed"
                if not successful_sources and not failed_sources:
                    final_status = "partial"
                _set_hall(
                    hname,
                    final_status,
                    records=_count_records(hname, "hall_day_seat") + recs + snapshot_count,
                    source="snapshot",
                    source_status=snapshot_status,
                    source_records=snapshot_count,
                )
        finally:
            scheduler.set_scrape_running(False)
            _BULK_PROGRESS["running"] = False

    background_tasks.add_task(_run)
    return {
        "ok": True,
        "message": f"{len(hall_list)}店舗のスクレイプを開始しました（{mode} / {days}日分）",
        "halls": hall_names,
        "days": days,
    }

@router.get("/api/scrape/status", tags=["scrape"])
def get_auto_scrape_status() -> dict:
    """スクレイプスケジューラーの状態と直近実行ログを返す"""
    try:
        from scraper.anaslo import get_scrape_logs, get_cookie
        logs = get_scrape_logs(limit=20)
        ck = get_cookie()
        sched = scheduler.get_scheduler()
        next_run = None
        if sched and sched.running:
            job = sched.get_job("nightly_scrape")
            if job and job.next_run_time:
                next_run = job.next_run_time.isoformat()
        return {
            "scheduler_running": sched is not None and sched.running,
            "scrape_running": scheduler.is_scrape_running(),
            "next_scheduled_run": next_run,
            "has_cookie": bool(ck),
            "has_cf_clearance": "cf_clearance" in ck if ck else False,
            "total_halls": len(scheduler._get_active_halls()),
            "recent_logs": logs,
        }
    except Exception as e:
        return {"error": str(e), "scheduler_running": False, "scrape_running": scheduler.is_scrape_running()}

@router.get("/api/scrape/bulk_status", tags=["scrape"])
def get_bulk_scrape_status() -> dict:
    """全店舗一括スクレイプの進捗を返す"""
    halls = _BULK_PROGRESS.get("halls", [])
    done = sum(1 for h in halls if h["status"] in {"done", "partial"})
    partial = sum(1 for h in halls if h["status"] == "partial")
    failed = sum(1 for h in halls if h["status"] == "failed")
    total = len(halls)
    running_hall = next((h["name"] for h in halls if h["status"] == "running"), None)

    elapsed_sec = 0
    if _BULK_PROGRESS.get("started_at"):
        import datetime as _dt2
        try:
            elapsed_sec = (
                _dt2.datetime.now() - _dt2.datetime.fromisoformat(_BULK_PROGRESS["started_at"])
            ).total_seconds()
        except Exception:
            pass

    finished = done + failed
    eta_min = 0
    if finished > 0 and total > finished and elapsed_sec > 0:
        per_hall = elapsed_sec / finished
        eta_min = round(per_hall * (total - finished) / 60)

    return {
        "running": _BULK_PROGRESS.get("running", False),
        "mode": _BULK_PROGRESS.get("mode", ""),
        "days": _BULK_PROGRESS.get("days", 0),
        "total": total,
        "done": done,
        "partial": partial,
        "failed": failed,
        "current_hall": running_hall,
        "eta_min": eta_min,
        "halls": halls,
    }


@router.get("/api/scrape/halls", tags=["scrape"])
def list_scrape_halls() -> list[dict]:
    """スクレイプ対象ホール一覧を返す（最終スクレイプ日・データ件数付き）"""
    from scraper.anaslo import get_hall_configs
    halls = get_hall_configs(enabled_only=False)
    base = halls if halls else _DEFAULT_HALLS
    # DBから各ホールの最終スクレイプ日を補完
    conn = _get_reports_conn()
    if conn:
        try:
            seat_stats = {
                r[0]: {"last_date": r[1], "record_count": r[2]}
                for r in conn.execute(
                    """SELECT hall_name, MAX(report_date), COUNT(*)
                       FROM hall_day_seat WHERE machine_name != '_NODATA_' GROUP BY hall_name"""
                ).fetchall()
            }
            machine_stats = {
                r[0]: {"last_date": r[1], "record_count": r[2]}
                for r in conn.execute(
                    """SELECT hall_name, MAX(report_date), COUNT(*)
                       FROM hall_day_machine WHERE machine_name != '_NODATA_' GROUP BY hall_name"""
                ).fetchall()
            }
            snapshot_stats = {
                r[0]: {"last_date": r[1], "record_count": r[2]}
                for r in conn.execute(
                    """SELECT hall_name, MAX(snapshot_date), COUNT(*)
                       FROM hall_machine_snapshot GROUP BY hall_name"""
                ).fetchall()
            } if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hall_machine_snapshot'"
            ).fetchone() else {}
            event_stats = {
                r[0]: {"last_date": r[1], "record_count": r[2]}
                for r in conn.execute(
                    "SELECT hall_name, MAX(event_date), COUNT(*) FROM hall_event GROUP BY hall_name"
                ).fetchall()
            } if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hall_event'"
            ).fetchone() else {}
            for h in base:
                name = h["hall_name"]
                seat = seat_stats.get(name, {})
                machine = machine_stats.get(name, {})
                snapshot = snapshot_stats.get(name, {})
                event = event_stats.get(name, {})
                dates = [
                    item.get("last_date")
                    for item in (seat, machine, snapshot)
                    if item.get("last_date")
                ]
                h["last_scraped_date"] = max(dates) if dates else None
                h["db_record_count"] = sum(
                    int(item.get("record_count", 0)) for item in (seat, machine, snapshot)
                )
                h["coverage"] = {
                    "seat": {"last_date": seat.get("last_date"), "records": seat.get("record_count", 0)},
                    "machine": {"last_date": machine.get("last_date"), "records": machine.get("record_count", 0)},
                    "snapshot": {"last_date": snapshot.get("last_date"), "records": snapshot.get("record_count", 0)},
                    "event": {"last_date": event.get("last_date"), "records": event.get("record_count", 0)},
                }
                if seat.get("record_count"):
                    h["data_level"] = "台番号あり"
                elif machine.get("record_count"):
                    h["data_level"] = "機種傾向あり"
                elif snapshot.get("record_count"):
                    h["data_level"] = "設置機種のみ"
                else:
                    h["data_level"] = "未取得"
        except Exception:
            pass
        conn.close()
    return base


@router.post("/api/scrape/halls", tags=["scrape"])
def add_scrape_hall(
    hall_name: str = Query(..., description="ホール名"),
    prefecture: str = Query("大阪府", description="都道府県"),
    url_override: str = Query("", description="URLが自動解決できない場合の手動指定"),
) -> dict:
    """スクレイプ対象ホールを追加または更新"""
    from scraper.anaslo import upsert_hall_config
    upsert_hall_config(hall_name, prefecture, url_override)
    return {"ok": True, "hall_name": hall_name, "prefecture": prefecture}


@router.delete("/api/scrape/halls", tags=["scrape"])
def remove_scrape_hall(hall_name: str = Query(..., description="削除するホール名")) -> dict:
    """スクレイプ対象ホールを削除"""
    from scraper.anaslo import delete_hall_config
    deleted = delete_hall_config(hall_name)
    return {"ok": deleted, "hall_name": hall_name}


@router.patch("/api/scrape/halls", tags=["scrape"])
def toggle_scrape_hall(
    hall_name: str = Query(...),
    enabled: bool = Query(...),
) -> dict:
    """ホールの有効/無効を切り替え"""
    from scraper.anaslo import upsert_hall_config, get_hall_configs
    halls = get_hall_configs(enabled_only=False)
    existing = next((h for h in halls if h["hall_name"] == hall_name), None)
    if not existing:
        return {"ok": False, "error": "not found"}
    upsert_hall_config(hall_name, existing["prefecture"], existing.get("url_override") or "", enabled)
    return {"ok": True, "hall_name": hall_name, "enabled": enabled}


@router.get("/api/scrape/logs", tags=["scrape"])
def get_scrape_logs_api(limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    """スクレイプ実行ログを返す"""
    from scraper.anaslo import get_scrape_logs
    return get_scrape_logs(limit=limit)

