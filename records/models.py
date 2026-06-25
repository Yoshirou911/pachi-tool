"""
実戦履歴 SQLite ストレージ。
DB ファイル: data/sessions.db

テーブル:
  halls           - ホール（店）マスタ
  sessions        - 1実戦セッション
  element_counts  - セッション内の各要素カウント
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, Optional

try:
    from config import SESSIONS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "sessions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS halls (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    date           TEXT    NOT NULL,
    hall_id        INTEGER REFERENCES halls(id),
    machine_name   TEXT    NOT NULL,
    seat_number    INTEGER,
    is_corner      INTEGER NOT NULL DEFAULT 0,
    games_total    INTEGER NOT NULL DEFAULT 0,
    investment     INTEGER NOT NULL DEFAULT 0,
    returns        INTEGER NOT NULL DEFAULT 0,
    diff_coins     INTEGER NOT NULL DEFAULT 0,
    is_event_day   INTEGER NOT NULL DEFAULT 0,
    started_from   INTEGER NOT NULL DEFAULT 0,
    posterior_json TEXT,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS element_counts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    element_name TEXT NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_date    ON sessions(date);
CREATE INDEX IF NOT EXISTS idx_sessions_hall    ON sessions(hall_id);
CREATE INDEX IF NOT EXISTS idx_sessions_machine ON sessions(machine_name);
CREATE INDEX IF NOT EXISTS idx_counts_session   ON element_counts(session_id);
"""


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# ドメインモデル
# ---------------------------------------------------------------------------

@dataclass
class Session:
    date: str                                # YYYY-MM-DD
    machine_name: str
    hall_name: str = ""
    seat_number: Optional[int] = None
    is_corner: bool = False
    games_total: int = 0
    investment: int = 0                      # 投資額（円）
    returns: int = 0                         # 回収額（円）
    diff_coins: int = 0                      # 差枚
    is_event_day: bool = False
    started_from: int = 0                    # 途中拾い開始G数（朝一は0）
    posterior: Optional[dict[str, float]] = None
    element_counts: dict[str, int] = field(default_factory=dict)
    notes: str = ""
    id: Optional[int] = None

    @property
    def diff_yen(self) -> int:
        return self.returns - self.investment

    @property
    def _posterior_json(self) -> Optional[str]:
        return json.dumps(self.posterior, ensure_ascii=False) if self.posterior else None

    @classmethod
    def _from_row(cls, row: sqlite3.Row, counts: list[sqlite3.Row]) -> "Session":
        posterior: Optional[dict[str, float]] = None
        if row["posterior_json"]:
            try:
                posterior = json.loads(row["posterior_json"])
            except json.JSONDecodeError:
                pass
        return cls(
            id=row["id"],
            date=row["date"],
            machine_name=row["machine_name"],
            hall_name=row["hall_name"] or "",
            seat_number=row["seat_number"],
            is_corner=bool(row["is_corner"]),
            games_total=row["games_total"],
            investment=row["investment"],
            returns=row["returns"],
            diff_coins=row["diff_coins"],
            is_event_day=bool(row["is_event_day"]),
            started_from=row["started_from"],
            posterior=posterior,
            element_counts={r["element_name"]: r["count"] for r in counts},
            notes=row["notes"] or "",
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def _get_or_create_hall(con: sqlite3.Connection, name: str) -> Optional[int]:
    if not name:
        return None
    row = con.execute("SELECT id FROM halls WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    return con.execute("INSERT INTO halls (name) VALUES (?)", (name,)).lastrowid


def save_session(s: Session) -> int:
    with _conn() as con:
        hall_id = _get_or_create_hall(con, s.hall_name)
        cur = con.execute(
            """INSERT INTO sessions
               (date,hall_id,machine_name,seat_number,is_corner,
                games_total,investment,returns,diff_coins,
                is_event_day,started_from,posterior_json,notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s.date, hall_id, s.machine_name, s.seat_number, int(s.is_corner),
             s.games_total, s.investment, s.returns, s.diff_coins,
             int(s.is_event_day), s.started_from, s._posterior_json, s.notes),
        )
        sid = cur.lastrowid
        for name, cnt in s.element_counts.items():
            con.execute(
                "INSERT INTO element_counts (session_id,element_name,count) VALUES (?,?,?)",
                (sid, name, cnt),
            )
        return sid


def update_session(session_id: int, **kwargs) -> None:
    _allowed = {
        "games_total", "investment", "returns", "diff_coins",
        "is_event_day", "started_from", "seat_number", "is_corner",
        "notes",
    }
    with _conn() as con:
        if "element_counts" in kwargs:
            counts = kwargs.pop("element_counts")
            con.execute("DELETE FROM element_counts WHERE session_id = ?", (session_id,))
            for name, cnt in counts.items():
                con.execute(
                    "INSERT INTO element_counts (session_id,element_name,count) VALUES (?,?,?)",
                    (session_id, name, cnt),
                )
        if "posterior" in kwargs:
            p = kwargs.pop("posterior")
            kwargs["posterior_json"] = json.dumps(p) if p else None

        cols = {k: v for k, v in kwargs.items() if k in _allowed or k == "posterior_json"}
        if cols:
            sets = ", ".join(f"{k} = ?" for k in cols) + ", updated_at = CURRENT_TIMESTAMP"
            con.execute(
                f"UPDATE sessions SET {sets} WHERE id = ?",
                (*cols.values(), session_id),
            )


def get_session(session_id: int) -> Optional[Session]:
    with _conn() as con:
        row = con.execute(
            "SELECT s.*, h.name AS hall_name FROM sessions s"
            " LEFT JOIN halls h ON h.id = s.hall_id WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        counts = con.execute(
            "SELECT element_name, count FROM element_counts WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        return Session._from_row(row, counts)


def list_sessions(
    hall_name: Optional[str] = None,
    machine_name: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Session]:
    clauses: list[str] = []
    params: list = []
    if hall_name:
        clauses.append("h.name = ?"); params.append(hall_name)
    if machine_name:
        clauses.append("s.machine_name = ?"); params.append(machine_name)
    if date_from:
        clauses.append("s.date >= ?"); params.append(date_from)
    if date_to:
        clauses.append("s.date <= ?"); params.append(date_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn() as con:
        rows = con.execute(
            f"SELECT s.*, h.name AS hall_name FROM sessions s"
            f" LEFT JOIN halls h ON h.id = s.hall_id {where}"
            f" ORDER BY s.date DESC, s.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        result = []
        for row in rows:
            counts = con.execute(
                "SELECT element_name, count FROM element_counts WHERE session_id = ?",
                (row["id"],),
            ).fetchall()
            result.append(Session._from_row(row, counts))
        return result


def delete_session(session_id: int) -> bool:
    with _conn() as con:
        return con.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount > 0


def list_halls() -> list[str]:
    with _conn() as con:
        return [r["name"] for r in con.execute("SELECT name FROM halls ORDER BY name").fetchall()]


def session_to_dict(s: Session) -> dict:
    return {
        "id": s.id,
        "date": s.date,
        "machine_name": s.machine_name,
        "hall_name": s.hall_name,
        "seat_number": s.seat_number,
        "is_corner": s.is_corner,
        "games_total": s.games_total,
        "investment": s.investment,
        "returns": s.returns,
        "diff_coins": s.diff_coins,
        "diff_yen": s.diff_yen,
        "is_event_day": s.is_event_day,
        "started_from": s.started_from,
        "posterior": s.posterior,
        "element_counts": s.element_counts,
        "notes": s.notes,
    }
