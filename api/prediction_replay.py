"""Bounded background replay. Completed reports persist; unfinished work doesn't.

Progress is process-local (the supported deployment uses one API worker).
Restarting the server stops an unfinished run; it never appears as completed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from threading import Lock, Thread

from fastapi import HTTPException

from api.deps import _get_reports_conn, logger
from app_version import APP_VERSION
from hall.machine_scope import is_smartslot_machine
from hall.prediction_benchmark import run_replay, store_replay
from hall.prediction_log import JST
from hall.regions import region_matches

_guard = Lock()
_state = {"running": False, "status": "idle", "completed": 0, "total": 7, "percent": 0}


def status():
    with _guard:
        return dict(_state)


def _set(**fields):
    with _guard:
        _state.update(fields)


def _load_actuals(conn, today):
    """Use the latest public result date, not the most profitable date."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    available = []
    for scope, table, field in (("machine", "hall_day_machine", "avg_diff_coins"), ("seat", "hall_day_seat", "diff_coins")):
        if table not in tables:
            continue
        rows = conn.execute(f"SELECT report_date,hall_name,machine_name FROM {table} WHERE report_date>=? AND report_date<? AND {field} IS NOT NULL",
                            ((today - timedelta(days=180)).isoformat(), today.isoformat()))
        for r in rows:
            if region_matches(r["hall_name"], None, "shijonawate") and is_smartslot_machine(r["machine_name"]):
                try:
                    day = date.fromisoformat(r["report_date"])
                except ValueError:
                    continue
                if day < today:
                    available.append(day)
    if not available:
        raise ValueError("直近180日に比較できる四條畷周辺の公開差枚がありません")
    end = max(available)
    dates = [(end - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    actuals = {}
    for scope, table in (("machine", "hall_day_machine"), ("seat", "hall_day_seat")):
        actuals[scope] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE report_date>=? AND report_date<=?", (dates[0], dates[-1]))] if table in tables else []
    return dates, actuals


def _worker():
    try:
        conn = _get_reports_conn()
        if conn is None:
            raise ValueError("実績DBがありません")
        try:
            dates, actuals = _load_actuals(conn, datetime.now(JST).date())
        finally:
            conn.close()
        from api.routers.hall import _build_target_search
        def progress(done, total, day):
            # 100% means persisted success, not just the final calculation.
            _set(completed=done, total=total, percent=min(99, int(done / total * 100)), current_date=day)
        result = run_replay(dates, _build_target_search, actuals, version=APP_VERSION, progress=progress)
        conn = _get_reports_conn()
        if conn is None:
            raise ValueError("保存先DBがありません")
        try:
            report_id = store_replay(conn, result, created_at=datetime.now(JST).isoformat())
        finally:
            conn.close()
        _set(running=False, status="completed", percent=100, report_id=report_id, finished_at=datetime.now(JST).isoformat())
    except Exception:
        logger.exception("[比較検証] 過去検証に失敗")
        _set(running=False, status="failed", error="比較検証に失敗しました。実績DBとサーバーログを確認してください。完了済みの過去レポートは保持しています。")


def start():
    with _guard:
        if _state["running"]:
            raise HTTPException(409, "過去の比較検証を実行中です")
        _state.clear()
        _state.update(running=True, status="running", completed=0, total=7, percent=0, started_at=datetime.now(JST).isoformat())
        initial = dict(_state)
    try:
        Thread(target=_worker, daemon=True, name="prediction-benchmark").start()
    except Exception:
        _set(running=False, status="failed", error="検証を開始できませんでした")
        raise
    return initial
