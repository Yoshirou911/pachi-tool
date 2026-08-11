"""
店舗稼働率トラッキング（ワンタップ手動記録）。
DB ファイル: data/hall_reports.db（scraper/anaslo.py の scrape_hall_config /
hall_day_seat と同じファイル。hall_name 文字列キーの既存慣習に合わせる）

テーブル:
  hall_occupancy_records - 1回の巡回記録（高/中/低 + 任意の平均回転数）

設計方針:
  - recorded_at (ISO8601) を正とする。weekday/time_bucket はそこから導出した
    冗長列で、店舗×曜日×時間帯の集計を後から素直にGROUP BYできるようにする
    ためのもの。集計ロジック自体は今回実装しない（手動記録が溜まってから着手）。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"

LEVELS = ("high", "mid", "low")

# 巡回優先度: 直近レベルが high ほど「まだ熱いうちに再訪する価値がある」として
# 経過時間を大きめに評価し、low ほど「しばらく様子見でよい」として小さめに評価する。
_LEVEL_URGENCY = {"high": 1.4, "mid": 1.0, "low": 0.6}
_NEVER_RECORDED_URGENCY = 1.2  # 記録が一度も無いホールは「未知」としてやや優先

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hall_occupancy_records (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    hall_name                    TEXT    NOT NULL,
    level                        TEXT    NOT NULL CHECK(level IN ('high','mid','low')),
    avg_rotation_games_per_hour  REAL,
    recorded_at                  TEXT    NOT NULL,
    weekday                      INTEGER NOT NULL,
    time_bucket                  TEXT,
    created_at                   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_occ_hall_recorded  ON hall_occupancy_records(hall_name, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_occ_hall_weekday   ON hall_occupancy_records(hall_name, weekday, time_bucket);
"""


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> sqlite3.Connection:
    """他の hall_reports.db 初期化関数(scraper.anaslo.init_db等)と同じく、
    呼び出し元が close() する接続を返す(api.main._init_auxiliary_databases の規約に合わせる)。"""
    con = sqlite3.connect(str(DB_PATH))
    con.executescript(_SCHEMA)
    con.commit()
    return con


def _time_bucket(dt: datetime) -> str:
    start = (dt.hour // 3) * 3
    end = (start + 3) % 24
    return f"{start:02d}-{end:02d}"


def _parse_recorded_at(recorded_at: Optional[str]) -> datetime:
    if not recorded_at:
        return datetime.now()
    try:
        return datetime.fromisoformat(recorded_at)
    except ValueError:
        raise ValueError(f"recorded_at の形式が不正です(ISO8601で指定してください): {recorded_at}")


def record_occupancy(
    hall_name: str,
    level: str,
    avg_rotation_games_per_hour: Optional[float] = None,
    recorded_at: Optional[str] = None,
) -> dict:
    """ワンタップ記録: 店舗の稼働状況(高/中/低)を1件保存する。"""
    if not hall_name:
        raise ValueError("hall_name は必須です")
    if level not in LEVELS:
        raise ValueError(f"level は {LEVELS} のいずれかで指定してください: {level}")

    dt = _parse_recorded_at(recorded_at)
    recorded_at_str = dt.isoformat(timespec="seconds")
    weekday = dt.weekday()  # 0=月曜 .. 6=日曜
    time_bucket = _time_bucket(dt)

    with _conn() as con:
        cur = con.execute(
            """INSERT INTO hall_occupancy_records
               (hall_name, level, avg_rotation_games_per_hour, recorded_at, weekday, time_bucket)
               VALUES (?,?,?,?,?,?)""",
            (hall_name, level, avg_rotation_games_per_hour, recorded_at_str, weekday, time_bucket),
        )
        row = con.execute(
            "SELECT * FROM hall_occupancy_records WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def get_patrol_list(hall_names: Optional[list[str]] = None) -> list[dict]:
    """巡回優先度順のホール一覧を返す。

    母集団は scraper.anaslo.get_hall_configs(enabled_only=True) の有効ホール
    (hall_names を渡した場合はそれで絞り込む)。各ホールの最新の稼働記録を
    LEFT JOIN し、記録が無い/古いホールほど、また直近が high だったホールほど
    priority_score が大きくなるようにする(降順ソートで上位=優先訪問先)。
    """
    try:
        from scraper.anaslo import get_hall_configs
        halls = get_hall_configs(enabled_only=True)
    except Exception:
        halls = []

    if hall_names:
        wanted = set(hall_names)
        halls = [h for h in halls if h["hall_name"] in wanted] or [
            {"hall_name": n, "prefecture": None} for n in hall_names
        ]

    now = datetime.now()
    with _conn() as con:
        latest_by_hall: dict[str, sqlite3.Row] = {}
        rows = con.execute(
            """SELECT hall_name, level, recorded_at, avg_rotation_games_per_hour
               FROM hall_occupancy_records
               WHERE id IN (
                   SELECT MAX(id) FROM hall_occupancy_records GROUP BY hall_name
               )"""
        ).fetchall()
        for r in rows:
            latest_by_hall[r["hall_name"]] = r

    result = []
    for h in halls:
        name = h["hall_name"]
        latest = latest_by_hall.get(name)
        if latest is None:
            hours_since = None
            last_level = None
            last_recorded_at = None
            urgency = _NEVER_RECORDED_URGENCY
            priority_score = 24 * 30 * urgency  # 未記録は「30日相当」として十分優先度を高くする
        else:
            last_recorded_at = latest["recorded_at"]
            last_level = latest["level"]
            try:
                hours_since = max(0.0, (now - datetime.fromisoformat(last_recorded_at)).total_seconds() / 3600)
            except ValueError:
                hours_since = 0.0
            urgency = _LEVEL_URGENCY.get(last_level, 1.0)
            priority_score = hours_since * urgency

        result.append({
            "hall_name": name,
            "prefecture": h.get("prefecture"),
            "last_level": last_level,
            "last_recorded_at": last_recorded_at,
            "hours_since": round(hours_since, 1) if hours_since is not None else None,
            "priority_score": round(priority_score, 2),
        })

    result.sort(key=lambda r: r["priority_score"], reverse=True)
    return result
