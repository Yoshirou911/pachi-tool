"""自動収集の成功・失敗を永続化し、アプリから確認できるようにする。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"


def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_run_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            started_at  TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            status      TEXT NOT NULL,
            records     INTEGER NOT NULL DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '{}',
            error       TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_collection_run_source ON collection_run_log(source,finished_at DESC)"
    )
    conn.commit()
    return conn


def _count_records(result) -> int:
    if isinstance(result, int):
        return max(0, result)
    if isinstance(result, dict):
        total = 0
        for key, value in result.items():
            if key in {"rows", "records", "machines", "floor_maps", "saved"} and isinstance(value, (int, float)):
                total += max(0, int(value))
            elif isinstance(value, (dict, list)):
                total += _count_records(value)
        return total
    if isinstance(result, list):
        return sum(_count_records(item) for item in result)
    return 0


def _status_counts(result) -> tuple[int, int]:
    """収集器ごとに形が違う戻り値から成功・失敗件数を再帰的に拾う。"""
    good = bad = 0
    if isinstance(result, dict):
        raw_status = result.get("status")
        if isinstance(raw_status, str):
            normalized = raw_status.strip().lower()
            if normalized in {"ok", "success", "done"}:
                good += 1
            elif normalized in {"failed", "failure", "error"} or normalized.startswith("error"):
                bad += 1
        for value in result.values():
            child_good, child_bad = _status_counts(value)
            good += child_good
            bad += child_bad
    elif isinstance(result, list):
        for item in result:
            child_good, child_bad = _status_counts(item)
            good += child_good
            bad += child_bad
    return good, bad


def run_logged(source: str, runner: Callable):
    started = datetime.now().isoformat(timespec="seconds")
    caught_exception = False
    try:
        result = runner()
        status = "success"
        error = ""
        good, bad = _status_counts(result)
        if bad and not good:
            status = "failed"
        elif bad:
            status = "partial"
        elif result not in (None, {}, []) and _count_records(result) == 0:
            # 空取得を成功と断定しない。サイト側のHTML変更や通信遮断を見逃さないため。
            status = "partial"
    except Exception as exc:
        result = None
        status = "failed"
        error = str(exc)[:1000]
        caught_exception = True
    finished = datetime.now().isoformat(timespec="seconds")
    conn = init_db()
    try:
        conn.execute(
            "INSERT INTO collection_run_log(source,started_at,finished_at,status,records,details_json,error) VALUES(?,?,?,?,?,?,?)",
            (
                source,
                started,
                finished,
                status,
                _count_records(result),
                json.dumps(result, ensure_ascii=False, default=str)[:20000] if result is not None else "{}",
                error,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    if caught_exception:
        raise RuntimeError(error)
    return result


def get_health(limit: int = 20) -> dict:
    conn = init_db()
    try:
        latest = conn.execute(
            """
            SELECT log.* FROM collection_run_log log
            JOIN (SELECT source,MAX(id) AS id FROM collection_run_log GROUP BY source) newest
              ON newest.id=log.id
            ORDER BY log.finished_at DESC
            """
        ).fetchall()
        recent = conn.execute(
            "SELECT * FROM collection_run_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()

    def serialize(row):
        item = dict(row)
        try:
            item["details"] = json.loads(item.pop("details_json"))
        except json.JSONDecodeError:
            item["details"] = {}
            item.pop("details_json", None)
        return item

    sources = [serialize(row) for row in latest]
    return {
        "overall": (
            "healthy" if sources and all(item["status"] == "success" for item in sources)
            else "warning" if sources else "not_started"
        ),
        "sources": sources,
        "recent_runs": [serialize(row) for row in recent],
    }
