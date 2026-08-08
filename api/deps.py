"""API全体で共有する軽量ヘルパー（ロガー・キャッシュ・DB接続・パス解決）。

ルーター(api/routers/*.py)・スケジューラ(api/scheduler.py)からのみ import される。
このモジュール自身は他の api.* モジュールに依存しない(循環import防止のため)。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("pachi_tool")

# ---------------------------------------------------------------------------
# 簡易インメモリキャッシュ（TTLベース）
# ---------------------------------------------------------------------------
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 600  # 10分


def _cache_get(key: str) -> object | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and time.time() - entry[0] < _CACHE_TTL:
            return entry[1]
    return None


def _cache_set(key: str, value: object) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), value)


def _cache_invalidate_prefix(prefix: str) -> None:
    with _CACHE_LOCK:
        keys = [k for k in _CACHE if k.startswith(prefix)]
        for k in keys:
            del _CACHE[k]


try:
    from config import HALL_REPORTS_DB, MACHINES_DIR as _MACHINES_DIR
except ImportError:
    HALL_REPORTS_DB = Path(__file__).parent.parent / "data" / "hall_reports.db"
    _MACHINES_DIR = None

MACHINES_DIR = _MACHINES_DIR or Path(__file__).parent.parent / "data" / "machines"
WEB_DIR = Path(__file__).parent.parent / "web"


def _get_machine_path(machine_name: str) -> Path:
    """機種名を安全にJSONファイルへ解決する。ディレクトリ越境は許可しない。"""
    if not machine_name or Path(machine_name).name != machine_name:
        raise HTTPException(400, "機種名が不正です")
    base = MACHINES_DIR.resolve()
    path = (base / f"{machine_name}.json").resolve()
    try:
        path.relative_to(base)
    except ValueError:
        raise HTTPException(400, "機種名が不正です")
    return path


def _get_reports_conn() -> Optional[sqlite3.Connection]:
    if not HALL_REPORTS_DB.exists():
        return None
    conn = sqlite3.connect(HALL_REPORTS_DB)
    conn.row_factory = sqlite3.Row
    # 古い/部分生成DBでも、存在する表にだけインデックスを作る。
    existing_tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    index_sql = {
        "hall_day_machine": [
            "CREATE INDEX IF NOT EXISTS idx_hdm_hall_date ON hall_day_machine(hall_name, report_date)",
            "CREATE INDEX IF NOT EXISTS idx_hdm_hall_machine ON hall_day_machine(hall_name, machine_name)",
        ],
        "hall_day_seat": [
            "CREATE INDEX IF NOT EXISTS idx_hds_hall_date ON hall_day_seat(hall_name, report_date)",
            "CREATE INDEX IF NOT EXISTS idx_hds_hall_machine ON hall_day_seat(hall_name, machine_name, seat_number)",
            "CREATE INDEX IF NOT EXISTS idx_hds_bb_prob ON hall_day_seat(bb_prob) WHERE bb_prob IS NOT NULL",
        ],
    }
    for table_name, statements in index_sql.items():
        if table_name not in existing_tables:
            continue
        for statement in statements:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                # 旧スキーマに列がない場合でも読み取り可能なAPIは継続する。
                pass
    return conn


def _get_event_conn():
    from scraper.events import get_conn
    return get_conn()
