"""期待値狙い用SQLiteストレージと厳格な判定ロジック。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Generator, Optional

from opportunity.machine_models import (
    apply_catalog_enrichment,
    calculate_machine_adjustment,
    estimate_duration,
)

try:
    from config import OPPORTUNITIES_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "opportunities.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunity_profiles (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_key                TEXT,
    machine_name               TEXT NOT NULL,
    condition_label            TEXT NOT NULL DEFAULT '条件未設定',
    exchange_type              TEXT NOT NULL DEFAULT 'unknown',
    funding_mode               TEXT NOT NULL DEFAULT 'any',
    reset_status               TEXT NOT NULL DEFAULT 'unknown',
    metric_name                TEXT NOT NULL DEFAULT '現在ゲーム数',
    unit_label                 TEXT NOT NULL DEFAULT 'G',
    start_threshold            REAL NOT NULL,
    ceiling_threshold          REAL,
    expected_value_yen         INTEGER,
    estimated_play_minutes     INTEGER,
    worst_case_investment_yen  INTEGER,
    stop_rule                  TEXT NOT NULL,
    source_name                TEXT NOT NULL DEFAULT '',
    source_url                 TEXT NOT NULL DEFAULT '',
    source_urls_json           TEXT NOT NULL DEFAULT '[]',
    curve_json                 TEXT NOT NULL DEFAULT '[]',
    duration_model_json        TEXT NOT NULL DEFAULT '{}',
    input_fields_json          TEXT NOT NULL DEFAULT '[]',
    requirements_json          TEXT NOT NULL DEFAULT '[]',
    duration_basis             TEXT NOT NULL DEFAULT '',
    duration_confidence        TEXT NOT NULL DEFAULT 'unknown',
    safe_play_minutes          INTEGER,
    crawler_approved_at        TEXT,
    crawler_source_hash        TEXT,
    discrepancy_note           TEXT NOT NULL DEFAULT '',
    verified_on                TEXT,
    confidence                 TEXT NOT NULL DEFAULT 'unverified',
    notes                      TEXT NOT NULL DEFAULT '',
    active                     INTEGER NOT NULL DEFAULT 1,
    created_at                 TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                 TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS opportunity_candidates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at    TEXT NOT NULL,
    hall_name      TEXT NOT NULL DEFAULT '',
    machine_name   TEXT NOT NULL,
    seat_number    INTEGER,
    current_value  REAL NOT NULL,
    profile_id     INTEGER REFERENCES opportunity_profiles(id),
    status         TEXT NOT NULL DEFAULT 'open',
    notes          TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS opportunity_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER NOT NULL UNIQUE REFERENCES opportunity_candidates(id) ON DELETE CASCADE,
    played_on       TEXT NOT NULL,
    investment_yen  INTEGER NOT NULL DEFAULT 0,
    returns_yen     INTEGER NOT NULL DEFAULT 0,
    played_minutes  INTEGER NOT NULL DEFAULT 0,
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS opportunity_budgets (
    month              TEXT PRIMARY KEY,
    starting_bankroll  INTEGER NOT NULL DEFAULT 0,
    loss_limit_yen     INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mobile_sync_state (
    sync_key       TEXT PRIMARY KEY,
    payload_json   TEXT NOT NULL,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS opportunity_observations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_key       TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    session_id     TEXT,
    observed_at    TEXT NOT NULL,
    hall_name      TEXT NOT NULL DEFAULT '',
    seat_number    INTEGER,
    machine_name   TEXT NOT NULL DEFAULT '',
    current_value  REAL NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'watch',
    time_bucket    TEXT NOT NULL DEFAULT '',
    extra_json     TEXT NOT NULL DEFAULT '{}',
    UNIQUE(sync_key, observation_id)
);

CREATE TABLE IF NOT EXISTS opportunity_reset_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_key       TEXT NOT NULL,
    record_id      TEXT NOT NULL,
    recorded_on    TEXT NOT NULL,
    hall_name      TEXT NOT NULL,
    machine_name   TEXT NOT NULL DEFAULT '',
    seat_number    INTEGER,
    weekday        INTEGER NOT NULL,
    reset_status   TEXT NOT NULL,
    evidence       TEXT NOT NULL DEFAULT '',
    notes          TEXT NOT NULL DEFAULT '',
    context_json   TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sync_key, record_id)
);

CREATE INDEX IF NOT EXISTS idx_opp_profiles_machine ON opportunity_profiles(machine_name, active);
CREATE INDEX IF NOT EXISTS idx_opp_candidates_status ON opportunity_candidates(status, observed_at);
CREATE INDEX IF NOT EXISTS idx_opp_results_date ON opportunity_results(played_on);
CREATE INDEX IF NOT EXISTS idx_opp_observations_hall_time ON opportunity_observations(hall_name, observed_at);
CREATE INDEX IF NOT EXISTS idx_opp_reset_hall_weekday ON opportunity_reset_records(hall_name, weekday, machine_name);
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
        columns = {row["name"] for row in con.execute("PRAGMA table_info(opportunity_profiles)").fetchall()}
        migrations = {
            "estimated_play_minutes": "INTEGER",
            "catalog_key": "TEXT",
            "condition_label": "TEXT NOT NULL DEFAULT '条件未設定'",
            "exchange_type": "TEXT NOT NULL DEFAULT 'unknown'",
            "funding_mode": "TEXT NOT NULL DEFAULT 'any'",
            "reset_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "source_urls_json": "TEXT NOT NULL DEFAULT '[]'",
            "curve_json": "TEXT NOT NULL DEFAULT '[]'",
            "discrepancy_note": "TEXT NOT NULL DEFAULT ''",
            "duration_model_json": "TEXT NOT NULL DEFAULT '{}'",
            "input_fields_json": "TEXT NOT NULL DEFAULT '[]'",
            "requirements_json": "TEXT NOT NULL DEFAULT '[]'",
            "duration_basis": "TEXT NOT NULL DEFAULT ''",
            "duration_confidence": "TEXT NOT NULL DEFAULT 'unknown'",
            "safe_play_minutes": "INTEGER",
            "crawler_approved_at": "TEXT",
            "crawler_source_hash": "TEXT",
        }
        for name, sql_type in migrations.items():
            if name not in columns:
                con.execute(f"ALTER TABLE opportunity_profiles ADD COLUMN {name} {sql_type}")
        candidate_columns = {row["name"] for row in con.execute("PRAGMA table_info(opportunity_candidates)").fetchall()}
        if "context_json" not in candidate_columns:
            con.execute("ALTER TABLE opportunity_candidates ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'")


def save_mobile_sync(sync_key: str, state: dict) -> dict:
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > 2_000_000:
        raise ValueError("同期データが2MBを超えています")
    observations = state.get("patrol_observations") if isinstance(state, dict) else []
    reset_records = state.get("hall_reset_records") if isinstance(state, dict) else []
    with _conn() as con:
        con.execute(
            """INSERT INTO mobile_sync_state(sync_key,payload_json) VALUES(?,?)
               ON CONFLICT(sync_key) DO UPDATE SET payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP""",
            (sync_key, payload),
        )
        for row in observations if isinstance(observations, list) else []:
            if not isinstance(row, dict) or not row.get("id") or not row.get("observed_at"):
                continue
            con.execute(
                """INSERT INTO opportunity_observations
                   (sync_key,observation_id,session_id,observed_at,hall_name,seat_number,machine_name,current_value,status,time_bucket,extra_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(sync_key,observation_id) DO UPDATE SET
                   session_id=excluded.session_id,observed_at=excluded.observed_at,hall_name=excluded.hall_name,
                   seat_number=excluded.seat_number,machine_name=excluded.machine_name,current_value=excluded.current_value,
                   status=excluded.status,time_bucket=excluded.time_bucket,extra_json=excluded.extra_json""",
                (sync_key, str(row["id"]), str(row.get("session_id") or ""), str(row["observed_at"]),
                 str(row.get("hall_name") or ""), row.get("seat_number"), str(row.get("machine_name") or ""),
                 float(row.get("current_value") or 0), str(row.get("status") or "watch"),
                 str(row.get("time_bucket") or ""), json.dumps(row, ensure_ascii=False)),
            )
        for row in reset_records if isinstance(reset_records, list) else []:
            if not isinstance(row, dict) or not row.get("id") or not row.get("recorded_on") or not row.get("hall_name"):
                continue
            try:
                recorded = date.fromisoformat(str(row["recorded_on"])[:10])
            except ValueError:
                continue
            status = str(row.get("reset_status") or "unknown")
            if status not in {"reset_confirmed", "normal", "unknown"}:
                continue
            con.execute(
                """INSERT INTO opportunity_reset_records
                   (sync_key,record_id,recorded_on,hall_name,machine_name,seat_number,weekday,reset_status,evidence,notes)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(sync_key,record_id) DO UPDATE SET
                   recorded_on=excluded.recorded_on,hall_name=excluded.hall_name,machine_name=excluded.machine_name,
                   seat_number=excluded.seat_number,weekday=excluded.weekday,reset_status=excluded.reset_status,
                   evidence=excluded.evidence,notes=excluded.notes""",
                (sync_key, str(row["id"]), recorded.isoformat(), str(row["hall_name"]),
                 str(row.get("machine_name") or ""), row.get("seat_number"), recorded.weekday(), status,
                 str(row.get("evidence") or ""), str(row.get("notes") or "")),
            )
        saved = con.execute("SELECT updated_at FROM mobile_sync_state WHERE sync_key=?", (sync_key,)).fetchone()
    return {"ok": True, "updated_at": saved["updated_at"], "observation_count": len(observations or []),
            "reset_record_count": len(reset_records or [])}


def load_mobile_sync(sync_key: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute("SELECT payload_json,updated_at FROM mobile_sync_state WHERE sync_key=?", (sync_key,)).fetchone()
    if not row:
        return None
    return {"state": json.loads(row["payload_json"]), "updated_at": row["updated_at"]}


def get_intraday_coverage(hall_name: str) -> dict:
    with _conn() as con:
        row = con.execute(
            """SELECT COUNT(*) AS records, COUNT(DISTINCT substr(observed_at,1,10)) AS days,
                      MIN(substr(observed_at,1,10)) AS first_date, MAX(substr(observed_at,1,10)) AS latest_date
               FROM opportunity_observations WHERE hall_name=?""", (hall_name,),
        ).fetchone()
    records, days = int(row["records"] or 0), int(row["days"] or 0)
    return {"records": records, "days": days, "first_date": row["first_date"], "latest_date": row["latest_date"],
            "ready": records >= 30 and days >= 3,
            "note": "30件・3日以上で時間帯別の実測分析を開始します。"}


def get_reset_tendency(hall_name: str, machine_name: str = "", weekday: int | None = None) -> dict:
    """店舗・曜日・機種別のリセット率と安全側の自動候補を返す。"""
    with _conn() as con:
        rows = con.execute(
            """SELECT recorded_on,machine_name,weekday,reset_status,evidence,notes
               FROM opportunity_reset_records WHERE hall_name=?
                 AND reset_status IN ('reset_confirmed','normal')
               ORDER BY recorded_on DESC,id DESC""",
            (hall_name,),
        ).fetchall()
    records = [dict(row) for row in rows]

    def summarize(items: list[dict]) -> dict:
        total = len(items)
        resets = sum(1 for item in items if item["reset_status"] == "reset_confirmed")
        rate = resets / total if total else None
        suggestion = "unknown"
        if total >= 3 and rate is not None:
            if rate >= 0.70:
                suggestion = "reset_confirmed"
            elif rate <= 0.30:
                suggestion = "normal"
        return {"samples": total, "resets": resets, "carryovers": total - resets,
                "reset_rate": round(rate * 100, 1) if rate is not None else None,
                "suggestion": suggestion}

    selected = records
    if weekday is not None:
        selected = [item for item in selected if int(item["weekday"]) == int(weekday)]
    if machine_name:
        machine_selected = [item for item in selected if item["machine_name"] == machine_name]
        if len(machine_selected) >= 3:
            selected = machine_selected
    by_weekday = {str(day): summarize([item for item in records if int(item["weekday"]) == day]) for day in range(7)}
    machines = sorted({item["machine_name"] for item in records if item["machine_name"]})
    by_machine = {name: summarize([item for item in records if item["machine_name"] == name]) for name in machines}
    return {"hall_name": hall_name, "machine_name": machine_name, "weekday": weekday,
            "overall": summarize(records), "selected": summarize(selected),
            "by_weekday": by_weekday, "by_machine": by_machine, "recent": records[:20]}


def _row_dict(row: sqlite3.Row | None) -> Optional[dict]:
    return dict(row) if row is not None else None


def _deserialize_profile(row: sqlite3.Row | dict | None) -> Optional[dict]:
    if row is None:
        return None
    result = dict(row)
    for source_key, target_key, fallback in (
        ("source_urls_json", "source_urls", []),
        ("curve_json", "curve_points", []),
        ("duration_model_json", "duration_model", {}),
        ("input_fields_json", "input_fields", []),
        ("requirements_json", "requirements", []),
    ):
        try:
            result[target_key] = json.loads(result.get(source_key) or json.dumps(fallback))
        except (TypeError, json.JSONDecodeError):
            result[target_key] = fallback
        result.pop(source_key, None)
    return result


def save_profile(data: dict) -> dict:
    columns = (
        "catalog_key", "machine_name", "condition_label", "exchange_type", "funding_mode", "reset_status",
        "metric_name", "unit_label", "start_threshold",
        "ceiling_threshold", "expected_value_yen", "estimated_play_minutes", "worst_case_investment_yen",
        "stop_rule", "source_name", "source_url", "source_urls_json", "curve_json",
        "duration_model_json", "input_fields_json", "requirements_json",
        "duration_basis", "duration_confidence", "safe_play_minutes",
        "verified_on", "confidence", "discrepancy_note", "notes",
    )
    values = dict(data)
    values["condition_label"] = data.get("condition_label") or "条件未設定"
    values["exchange_type"] = data.get("exchange_type") or "unknown"
    values["funding_mode"] = data.get("funding_mode") or "any"
    values["reset_status"] = data.get("reset_status") or "unknown"
    for key in ("source_name", "source_url", "discrepancy_note", "notes"):
        values[key] = data.get(key) or ""
    source_urls = data.get("source_urls") or ([data["source_url"]] if data.get("source_url") else [])
    values["source_urls_json"] = json.dumps(source_urls, ensure_ascii=False)
    values["curve_json"] = json.dumps(data.get("curve_points", []), ensure_ascii=False)
    values["duration_model_json"] = json.dumps(data.get("duration_model", {}), ensure_ascii=False)
    values["input_fields_json"] = json.dumps(data.get("input_fields", []), ensure_ascii=False)
    values["requirements_json"] = json.dumps(data.get("requirements", []), ensure_ascii=False)
    values["duration_basis"] = data.get("duration_basis") or ""
    values["duration_confidence"] = data.get("duration_confidence") or "unknown"
    with _conn() as con:
        cur = con.execute(
            f"INSERT INTO opportunity_profiles ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(values.get(column) for column in columns),
        )
        return _deserialize_profile(con.execute("SELECT * FROM opportunity_profiles WHERE id = ?", (cur.lastrowid,)).fetchone()) or {}


def list_profiles(machine_name: str | None = None, active_only: bool = True) -> list[dict]:
    clauses, params = [], []
    if machine_name:
        clauses.append("machine_name = ?")
        params.append(machine_name)
    if active_only:
        clauses.append("active = 1")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM opportunity_profiles" + where + " ORDER BY machine_name, start_threshold DESC, id DESC",
            params,
        ).fetchall()
        return [_deserialize_profile(row) or {} for row in rows]


def get_profile(profile_id: int) -> Optional[dict]:
    with _conn() as con:
        return _deserialize_profile(con.execute("SELECT * FROM opportunity_profiles WHERE id = ?", (profile_id,)).fetchone())


def seed_catalog(path: Path | None = None) -> int:
    """調査済みカタログを冪等に取り込む。ユーザー作成ルールは変更しない。"""
    catalog_path = path or Path(__file__).parent.parent / "data" / "opportunity_catalog.json"
    if not catalog_path.exists():
        return 0
    raw = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    count = 0
    for raw_profile in raw.get("profiles", []):
        profile = apply_catalog_enrichment(raw_profile)
        key = profile.get("catalog_key")
        if not key:
            continue
        with _conn() as con:
            row = con.execute(
                "SELECT id,verified_on,crawler_approved_at FROM opportunity_profiles WHERE catalog_key = ?", (key,)
            ).fetchone()
        if row:
            values = dict(profile)
            values["condition_label"] = profile.get("condition_label") or "条件未設定"
            values["exchange_type"] = profile.get("exchange_type") or "unknown"
            values["funding_mode"] = profile.get("funding_mode") or "any"
            values["reset_status"] = profile.get("reset_status") or "unknown"
            for field in ("source_name", "source_url", "discrepancy_note", "notes"):
                values[field] = profile.get(field) or ""
            values["source_urls_json"] = json.dumps(profile.get("source_urls", []), ensure_ascii=False)
            values["curve_json"] = json.dumps(profile.get("curve_points", []), ensure_ascii=False)
            values["duration_model_json"] = json.dumps(profile.get("duration_model", {}), ensure_ascii=False)
            values["input_fields_json"] = json.dumps(profile.get("input_fields", []), ensure_ascii=False)
            values["requirements_json"] = json.dumps(profile.get("requirements", []), ensure_ascii=False)
            values["duration_basis"] = profile.get("duration_basis") or ""
            values["duration_confidence"] = profile.get("duration_confidence") or "unknown"
            columns = (
                "machine_name", "condition_label", "exchange_type", "funding_mode", "reset_status",
                "metric_name", "unit_label", "start_threshold",
                "ceiling_threshold", "expected_value_yen", "estimated_play_minutes", "worst_case_investment_yen",
                "stop_rule", "source_name", "source_url", "source_urls_json", "curve_json",
                "duration_model_json", "input_fields_json", "requirements_json",
                "duration_basis", "duration_confidence", "safe_play_minutes",
                "verified_on", "confidence", "discrepancy_note", "notes",
            )
            if row["crawler_approved_at"] and str(profile.get("verified_on") or "") <= str(row["verified_on"] or ""):
                columns = tuple(column for column in columns if column not in {"expected_value_yen", "curve_json", "verified_on"})
            with _conn() as con:
                sets = ",".join(f"{column} = ?" for column in columns)
                con.execute(
                    f"UPDATE opportunity_profiles SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (*[values.get(column) for column in columns], row["id"]),
                )
        else:
            save_profile(profile)
        count += 1
    return count


def deactivate_profile(profile_id: int) -> bool:
    with _conn() as con:
        return con.execute(
            "UPDATE opportunity_profiles SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (profile_id,),
        ).rowcount > 0


def _best_profile(con: sqlite3.Connection, machine_name: str) -> Optional[int]:
    row = con.execute(
        """SELECT id FROM opportunity_profiles
           WHERE machine_name = ? AND active = 1
           ORDER BY CASE confidence
             WHEN 'official' THEN 4 WHEN 'verified' THEN 3
             WHEN 'reference' THEN 2 ELSE 1 END DESC,
             verified_on DESC, id DESC LIMIT 1""",
        (machine_name,),
    ).fetchone()
    return row["id"] if row else None


def save_candidate(data: dict) -> dict:
    observed_at = data.get("observed_at") or datetime.now().isoformat(timespec="minutes")
    with _conn() as con:
        profile_id = data.get("profile_id") or _best_profile(con, data["machine_name"])
        cur = con.execute(
            """INSERT INTO opportunity_candidates
               (observed_at,hall_name,machine_name,seat_number,current_value,profile_id,notes,context_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (observed_at, data.get("hall_name", ""), data["machine_name"], data.get("seat_number"),
             data["current_value"], profile_id, data.get("notes", ""),
             json.dumps(data.get("context") or {}, ensure_ascii=False)),
        )
        return _row_dict(con.execute("SELECT * FROM opportunity_candidates WHERE id = ?", (cur.lastrowid,)).fetchone()) or {}


def set_candidate_status(candidate_id: int, status: str) -> bool:
    with _conn() as con:
        return con.execute(
            "UPDATE opportunity_candidates SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, candidate_id),
        ).rowcount > 0


def save_result(candidate_id: int, data: dict) -> dict:
    with _conn() as con:
        exists = con.execute("SELECT id FROM opportunity_candidates WHERE id = ?", (candidate_id,)).fetchone()
        if not exists:
            raise KeyError(candidate_id)
        cur = con.execute(
            """INSERT INTO opportunity_results
               (candidate_id,played_on,investment_yen,returns_yen,played_minutes,notes)
               VALUES (?,?,?,?,?,?)""",
            (candidate_id, data.get("played_on") or date.today().isoformat(), data.get("investment_yen", 0),
             data.get("returns_yen", 0), data.get("played_minutes", 0), data.get("notes", "")),
        )
        con.execute(
            "UPDATE opportunity_candidates SET status = 'played', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (candidate_id,),
        )
        return _row_dict(con.execute("SELECT * FROM opportunity_results WHERE id = ?", (cur.lastrowid,)).fetchone()) or {}


def get_profile_calibration(profile_id: int) -> dict:
    """同一ルールの実戦30件以上から、期待値を安全側へだけ縮小補正する。"""
    with _conn() as con:
        profile = _deserialize_profile(
            con.execute("SELECT * FROM opportunity_profiles WHERE id=?", (profile_id,)).fetchone()
        )
        rows = con.execute(
            """
            SELECT c.current_value,r.investment_yen,r.returns_yen
              FROM opportunity_results r
              JOIN opportunity_candidates c ON c.id=r.candidate_id
             WHERE c.profile_id=?
             ORDER BY r.played_on,r.id
            """,
            (profile_id,),
        ).fetchall()
    if not profile:
        return {"active": False, "count": 0, "factor": 1.0, "factor_pct": 100}
    curve = sorted(profile.get("curve_points") or [], key=lambda item: float(item["value"]))
    expected_total = 0
    actual_total = 0
    for row in rows:
        point = next(
            (item for item in reversed(curve) if float(item["value"]) <= float(row["current_value"])),
            None,
        )
        expected = point.get("ev_yen") if point else profile.get("expected_value_yen")
        if expected is None or float(expected) <= 0:
            continue
        expected_total += int(expected)
        actual_total += int(row["returns_yen"] or 0) - int(row["investment_yen"] or 0)
    count = len(rows)
    observed = actual_total / expected_total if expected_total > 0 else 1.0
    conservative = max(0.5, min(1.0, observed))
    reliability = count / (count + 30) if count >= 30 else 0.0
    factor = 1.0 - (1.0 - conservative) * reliability
    factor = round(factor, 3)
    return {
        "active": count >= 30 and factor < 0.995,
        "count": count,
        "expected_total_yen": expected_total,
        "actual_total_yen": actual_total,
        "gap_yen": actual_total - expected_total,
        "factor": factor,
        "factor_pct": round(factor * 100),
        "method": "conservative_shrinkage_v1",
        "note": "30件未満では補正せず、実収支が期待値を下回る場合だけ安全側へ縮小します。",
    }


def save_budget(month: str, starting_bankroll: int, loss_limit_yen: int) -> dict:
    with _conn() as con:
        con.execute(
            """INSERT INTO opportunity_budgets (month,starting_bankroll,loss_limit_yen)
               VALUES (?,?,?) ON CONFLICT(month) DO UPDATE SET
               starting_bankroll=excluded.starting_bankroll,
               loss_limit_yen=excluded.loss_limit_yen,
               updated_at=CURRENT_TIMESTAMP""",
            (month, starting_bankroll, loss_limit_yen),
        )
        return _row_dict(con.execute("SELECT * FROM opportunity_budgets WHERE month = ?", (month,)).fetchone()) or {}


def get_budget_summary(month: str) -> dict:
    with _conn() as con:
        budget = _row_dict(con.execute("SELECT * FROM opportunity_budgets WHERE month = ?", (month,)).fetchone())
        result = con.execute(
            """SELECT COUNT(*) AS plays,
                      COALESCE(SUM(investment_yen),0) AS investment_yen,
                      COALESCE(SUM(returns_yen),0) AS returns_yen,
                      COALESCE(SUM(played_minutes),0) AS played_minutes,
                      COALESCE(SUM(CASE WHEN returns_yen > investment_yen THEN 1 ELSE 0 END),0) AS wins
               FROM opportunity_results WHERE substr(played_on,1,7) = ?""",
            (month,),
        ).fetchone()
        open_count = con.execute("SELECT COUNT(*) FROM opportunity_candidates WHERE status = 'open'").fetchone()[0]

    starting = int((budget or {}).get("starting_bankroll", 0))
    loss_limit = int((budget or {}).get("loss_limit_yen", 0))
    investment = int(result["investment_yen"])
    returns = int(result["returns_yen"])
    net = returns - investment
    current_bankroll = max(0, starting + net)
    used_loss = max(0, -net)
    remaining_loss = max(0, loss_limit - used_loss)
    return {
        "month": month,
        "configured": budget is not None,
        "starting_bankroll": starting,
        "loss_limit_yen": loss_limit,
        "investment_yen": investment,
        "returns_yen": returns,
        "net_profit_yen": net,
        "current_bankroll": current_bankroll,
        "used_loss_yen": used_loss,
        "remaining_loss_yen": remaining_loss,
        "risk_capacity_yen": min(current_bankroll, remaining_loss),
        "plays": int(result["plays"]),
        "wins": int(result["wins"]),
        "played_minutes": int(result["played_minutes"]),
        "open_candidates": int(open_count),
    }


def _finite_number(value, default=None):
    if value in (None, ""):
        return default
    try:
        number = float(value)
        return number if number == number and number not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


def calculate_section_adjustment(profile: dict, section_difference_coins=None, context: dict | None = None) -> dict:
    """機種専用ルールを優先し、未確認の汎用差枚補正は着席許可に使わない。"""
    combined = dict(context or {})
    if section_difference_coins not in (None, ""):
        combined["section_difference_coins"] = section_difference_coins
    difference = _finite_number(section_difference_coins)
    # 旧来の線形補正は、カタログ側に明示した検証済みルールだけ互換利用する。
    enabled = bool(profile.get("section_cut_enabled") or profile.get("section_cut_verified")
                   or profile.get("section_cut_target_coins") is not None
                   or (not profile.get("catalog_key") and "スマスロ" in str(profile.get("machine_name") or "")))
    if not enabled:
        return calculate_machine_adjustment(profile, combined)
    if difference is None:
        return {"active": False}
    target = _finite_number(profile.get("section_cut_target_coins"), 2400.0)
    window = max(1.0, _finite_number(profile.get("section_cut_window_coins"), 1200.0))
    max_reduction = max(0.0, _finite_number(profile.get("section_cut_max_border_reduction"), 100.0))
    remaining = target - difference
    if remaining <= 0 or remaining > window:
        return {"active": False, "current_difference_coins": difference,
                "remaining_to_cut_coins": max(0, round(remaining)), "target_coins": target}
    proximity = max(0.0, min(1.0, 1.0 - remaining / window))
    reduction = int(max_reduction * proximity // 10 * 10)
    return {
        "active": reduction > 0,
        "verified": bool(profile.get("section_cut_verified")),
        "current_difference_coins": difference,
        "remaining_to_cut_coins": round(remaining),
        "target_coins": target,
        "proximity": proximity,
        "border_reduction": reduction,
    }


def calculate_replay_adjustment(
    profile: dict,
    exchange_type: str = "equivalent",
    funding_mode: str = "cash",
    replay_limit_medals=0,
    replay_used_medals=0,
    exchange_rate=None,
) -> dict:
    """再プレイ上限を超える現金投資の交換ギャップを期待値から控除する。"""
    rate = _finite_number(exchange_rate, 5.6 if exchange_type == "56" else None)
    limit = max(0.0, _finite_number(replay_limit_medals, 0.0))
    used = max(0.0, _finite_number(replay_used_medals, 0.0))
    if exchange_type == "equivalent" or funding_mode != "medals" or not rate or rate <= 5 or limit <= 0:
        return {"active": False, "remaining_replay_medals": max(0, limit - used), "cash_gap_yen": 0}
    remaining = max(0.0, limit - used)
    gap_per_medal = max(0.0, 20.0 - 100.0 / rate)
    target_ev = max(0.0, _finite_number(profile.get("expected_value_yen"), 0.0))
    points = []
    for point in sorted(profile.get("curve_points") or [], key=lambda item: float(item["value"])):
        worst = max(0.0, _finite_number(point.get("worst_case_yen"), profile.get("worst_case_investment_yen") or 0))
        required = int(-(-worst // 20))
        cash_medals = max(0.0, required - remaining)
        cash_gap = round(cash_medals * gap_per_medal)
        points.append({**point, "required_medals": required, "cash_medals": cash_medals,
                       "cash_gap_yen": cash_gap, "adjusted_ev_yen": int(point.get("ev_yen") or 0) - cash_gap})
    threshold = next((point for point in points if point["adjusted_ev_yen"] >= target_ev), None)
    return {
        "active": True,
        "exchange_rate": rate,
        "replay_limit_medals": limit,
        "replay_used_medals": used,
        "remaining_replay_medals": remaining,
        "gap_per_cash_medal_yen": gap_per_medal,
        "adjusted_start_threshold": float(threshold["value"]) if threshold else None,
        "adjusted_points": points,
    }


def validate_profile_inputs(profile: dict, extra_inputs: dict | None = None) -> list[str]:
    values = extra_inputs or {}
    errors: list[str] = []
    for field in profile.get("input_fields") or []:
        value = values.get(field.get("id"))
        if field.get("required") and value in (None, ""):
            errors.append(f"{field.get('label') or field.get('id')}が未入力")
    for rule in profile.get("requirements") or []:
        raw = values.get(rule.get("field"))
        if raw in (None, ""):
            continue
        operator, expected = rule.get("operator"), rule.get("value")
        matched = True
        if operator in {"gte", "lte"}:
            actual = _finite_number(raw)
            target = _finite_number(expected)
            matched = actual is not None and target is not None and (actual >= target if operator == "gte" else actual <= target)
        elif operator == "eq":
            matched = str(raw) == str(expected)
        elif operator == "in":
            matched = str(raw) in {str(item) for item in (expected or [])}
        if not matched:
            errors.append(str(rule.get("message") or f"{rule.get('field')}が条件外"))
    return errors


def assess_candidate(candidate: dict, profile: dict | None, risk_capacity_yen: int, context: dict | None = None) -> dict:
    """根拠不足を勝手に補完せず、動的補正を含めて候補台を判定する。"""
    if not profile:
        return {"judgment": "unknown", "reason": "狙い目ルールが未登録です", "actionable": False, "priority": -1000}

    context = context or {}
    current = float(candidate["current_value"])
    start = float(profile["start_threshold"])
    ceiling = profile.get("ceiling_threshold")
    delta = current - start
    progress = None
    if ceiling is not None and float(ceiling) > 0:
        progress = min(100, max(0, round(current / float(ceiling) * 100)))

    section = calculate_section_adjustment(profile, context.get("section_difference_coins"), context)
    if section.get("active") and section.get("adjusted_start_threshold") is not None:
        section_start = max(0, float(section["adjusted_start_threshold"]))
    else:
        section_start = max(0, start - section.get("border_reduction", 0)) if section.get("active") else start
    replay = calculate_replay_adjustment(
        profile,
        exchange_type=context.get("exchange_type", "equivalent"),
        funding_mode=context.get("funding_mode", "cash"),
        replay_limit_medals=context.get("replay_limit_medals", 0),
        replay_used_medals=context.get("replay_used_medals", 0),
        exchange_rate=context.get("exchange_rate"),
    )
    replay_start = replay.get("adjusted_start_threshold") if replay.get("active") else None
    if replay.get("active") and replay_start is None:
        return {
            "judgment": "verify",
            "reason": "現金ギャップを含めた安全な着席ラインを算出できません",
            "actionable": False,
            "priority": -60 + delta,
            "progress_pct": progress,
            "base_start_threshold": start,
            "adjusted_start_threshold": None,
            "section_adjustment": section,
            "replay_adjustment": replay,
        }
    adjusted_start = section_start if replay_start is None else max(section_start, replay_start)
    if current < adjusted_start:
        return {
            "judgment": "wait", "reason": f"補正後ラインまであと{adjusted_start - current:g}{profile['unit_label']}",
            "actionable": False, "priority": -100 + delta, "progress_pct": progress,
            "base_start_threshold": start, "adjusted_start_threshold": adjusted_start,
            "section_adjustment": section, "replay_adjustment": replay,
        }

    if (profile.get("confidence") not in {"official", "verified"}
            or not profile.get("verified_on") or not profile.get("source_name")):
        return {
            "judgment": "verify", "reason": "出典または確認日が不十分です",
            "actionable": False, "priority": -50 + delta, "progress_pct": progress,
        }

    curve = sorted(profile.get("curve_points") or [], key=lambda point: float(point["value"]))
    curve_point = next((point for point in reversed(curve) if float(point["value"]) <= current), None)
    base_expected = (curve_point.get("ev_yen") if curve_point else None)
    if base_expected is None:
        base_expected = profile.get("expected_value_yen")
    if base_expected is None:
        return {"judgment": "verify", "reason": "期待値が未登録です", "actionable": False, "priority": -40 + delta}
    duration = estimate_duration(profile, current)
    minutes = (curve_point.get("minutes") if curve_point else None) or duration.get("estimated_play_minutes")
    worst = (curve_point.get("worst_case_yen") if curve_point else None)
    if worst is None:
        worst = profile.get("worst_case_investment_yen")
    if worst is None:
        return {
            "judgment": "verify", "reason": "最悪投資額が未登録です",
            "actionable": False, "priority": -40 + delta, "progress_pct": progress,
        }

    cash_gap = 0
    if replay.get("active"):
        adjusted_point = next((point for point in reversed(replay.get("adjusted_points") or [])
                               if float(point["value"]) <= current), None)
        required = int(-(-float(worst) // 20))
        cash_medals = max(0, required - float(replay.get("remaining_replay_medals") or 0))
        cash_gap = int(adjusted_point["cash_gap_yen"]) if adjusted_point else round(
            cash_medals * float(replay.get("gap_per_cash_medal_yen") or 0)
        )
    expected = int(base_expected) - cash_gap
    if expected <= 0:
        return {
            "judgment": "wait", "reason": "現金ギャップ反映後の期待値がプラスになりません",
            "actionable": False, "priority": -80 + delta, "progress_pct": progress,
            "base_expected_value_yen": int(base_expected), "expected_value_yen": expected,
            "cash_gap_yen": cash_gap, "base_start_threshold": start,
            "adjusted_start_threshold": adjusted_start, "section_adjustment": section,
            "replay_adjustment": replay,
        }
    if int(worst) > risk_capacity_yen:
        return {
            "judgment": "insufficient_funds",
            "reason": f"必要資金{int(worst):,}円に対し許容{risk_capacity_yen:,}円",
            "actionable": False, "priority": -20 + delta, "progress_pct": progress,
            "expected_value_yen": expected, "base_expected_value_yen": int(base_expected),
            "cash_gap_yen": cash_gap, "worst_case_investment_yen": worst,
            "base_start_threshold": start, "adjusted_start_threshold": adjusted_start,
            "section_adjustment": section, "replay_adjustment": replay,
        }

    ev_per_hour = round(expected * 60 / int(minutes)) if minutes else None
    priority = ev_per_hour if ev_per_hour is not None else expected
    section_needs_verification = current < start and section.get("active") and not section.get("verified")
    return {
        "judgment": "verify" if section_needs_verification else "target",
        "reason": ("有利区間差枚でボーダーが浅くなりますが、機種固有の補正値が未検証です"
                   if section_needs_verification else
                   (section.get("reason") if section.get("active") and section.get("reason") else
                    ("現金ギャップを差し引いても条件を満たしています" if cash_gap else "登録条件と資金条件を満たしています"))),
        "actionable": not section_needs_verification,
        "priority": priority if not section_needs_verification else -45,
        "progress_pct": progress,
        "expected_value_yen": expected, "base_expected_value_yen": int(base_expected),
        "cash_gap_yen": cash_gap, "worst_case_investment_yen": worst,
        "ev_per_hour_yen": ev_per_hour,
        "estimated_play_minutes": int(minutes) if minutes else None,
        "safe_play_minutes": duration.get("safe_play_minutes"),
        "duration_breakdown": duration.get("breakdown", {}),
        "duration_confidence": duration.get("confidence"),
        "duration_basis": duration.get("basis"),
        "matched_curve_value": curve_point.get("value") if curve_point else None,
        "base_start_threshold": start, "adjusted_start_threshold": adjusted_start,
        "section_adjustment": section, "replay_adjustment": replay,
    }


def assess_quick_decision(
    profile: dict | None,
    current_value: float,
    risk_capacity_yen: int,
    exchange_type: str,
    funding_mode: str,
    reset_status: str,
    minutes_until_close: int,
    section_difference_coins=None,
    replay_limit_medals=0,
    replay_used_medals=0,
    exchange_rate=None,
    extra_inputs: dict | None = None,
) -> dict:
    """現場入力とルール条件を照合し、閉店時間を含めて安全側に判定する。"""
    if not profile:
        return {
            "judgment": "unknown", "reason": "一致する狙い目ルールがありません",
            "actionable": False, "priority": -1000, "warnings": [],
        }

    mismatches: list[str] = []
    profile_exchange = profile.get("exchange_type") or "unknown"
    profile_funding = profile.get("funding_mode") or "any"
    profile_reset = profile.get("reset_status") or "unknown"
    if profile_exchange == "unknown":
        mismatches.append("ルールの交換条件が未登録")
    elif profile_exchange != exchange_type:
        mismatches.append("交換条件がルールと不一致")
    if profile_funding not in {"any", funding_mode}:
        mismatches.append("現金／持ちメダル条件が不一致")
    if reset_status == "unknown":
        mismatches.append("据え置き／リセット状況が未確認")
    elif profile_reset == "unknown":
        mismatches.append("ルールのリセット条件が未登録")
    elif profile_reset not in {"any", reset_status}:
        mismatches.append("リセット条件がルールと不一致")
    mismatches.extend(validate_profile_inputs(profile, extra_inputs))
    if mismatches:
        return {
            "judgment": "condition_mismatch", "reason": "・".join(mismatches),
            "actionable": False, "priority": -60, "warnings": [],
        }

    assessment = assess_candidate(
        {"current_value": current_value}, profile, max(0, int(risk_capacity_yen)),
        {
            "section_difference_coins": section_difference_coins,
            "replay_limit_medals": replay_limit_medals,
            "replay_used_medals": replay_used_medals,
            "exchange_rate": exchange_rate,
            "exchange_type": exchange_type,
            "funding_mode": funding_mode,
            **(extra_inputs or {}),
        },
    )
    assessment["warnings"] = []
    assessment["minutes_until_close"] = minutes_until_close
    duration = estimate_duration(profile, current_value)
    assessment["estimated_play_minutes"] = assessment.get("estimated_play_minutes") or duration.get("estimated_play_minutes")
    assessment["safe_play_minutes"] = assessment.get("safe_play_minutes") or duration.get("safe_play_minutes")
    assessment["duration_breakdown"] = assessment.get("duration_breakdown") or duration.get("breakdown", {})
    assessment["duration_confidence"] = assessment.get("duration_confidence") or duration.get("confidence")
    assessment["duration_basis"] = assessment.get("duration_basis") or duration.get("basis")

    verified_on = profile.get("verified_on")
    if verified_on:
        try:
            age_days = (date.today() - date.fromisoformat(verified_on)).days
            assessment["source_age_days"] = age_days
            if age_days > 180 and assessment.get("actionable"):
                assessment.update({
                    "judgment": "verify", "reason": "情報確認から180日を超えています",
                    "actionable": False, "priority": -45,
                })
        except ValueError:
            assessment["warnings"].append("確認日の形式が不正です")

    if minutes_until_close <= 0:
        assessment.update({
            "judgment": "closing_risk", "reason": "閉店時刻を過ぎています",
            "actionable": False, "priority": -30,
        })
        return assessment

    estimated_minutes = assessment.get("estimated_play_minutes")
    required_minutes = int(assessment.get("safe_play_minutes") or 120)
    assessment["required_minutes_with_buffer"] = required_minutes
    if assessment.get("judgment") == "target" and minutes_until_close < required_minutes:
        detail = (
            f"平均{int(estimated_minutes)}分・安全側{required_minutes}分に対し、閉店まで{minutes_until_close}分"
            if estimated_minutes else
            f"消化時間が未登録のため、閉店2時間以内は見送り（残り{minutes_until_close}分）"
        )
        assessment.update({
            "judgment": "closing_risk", "reason": detail,
            "actionable": False, "priority": -30,
        })
    elif assessment.get("judgment") == "target" and not estimated_minutes:
        assessment["warnings"].append("消化時間未登録：閉店リスクは安全側の120分基準")
    return assessment


def get_dashboard(month: str) -> dict:
    summary = get_budget_summary(month)
    with _conn() as con:
        rows = con.execute(
            """SELECT c.*, p.metric_name, p.unit_label, p.start_threshold, p.ceiling_threshold,
                      p.condition_label, p.expected_value_yen, p.estimated_play_minutes,
                      p.worst_case_investment_yen, p.stop_rule, p.source_name, p.source_url,
                      p.source_urls_json, p.curve_json, p.discrepancy_note, p.verified_on, p.confidence
                      ,p.duration_model_json,p.input_fields_json,p.requirements_json,
                      p.duration_basis,p.duration_confidence,p.safe_play_minutes
               FROM opportunity_candidates c
               LEFT JOIN opportunity_profiles p ON p.id = c.profile_id
               WHERE c.status = 'open' ORDER BY c.observed_at DESC, c.id DESC"""
        ).fetchall()
        result_rows = con.execute(
            """SELECT r.*, c.machine_name, c.hall_name, c.seat_number,
                      c.current_value, p.metric_name, p.unit_label, p.start_threshold,
                      p.expected_value_yen, p.stop_rule
               FROM opportunity_results r
               JOIN opportunity_candidates c ON c.id = r.candidate_id
               LEFT JOIN opportunity_profiles p ON p.id = c.profile_id
               ORDER BY r.played_on DESC, r.id DESC LIMIT 20"""
        ).fetchall()

    candidates = []
    profile_keys = {
        "machine_name", "metric_name", "unit_label", "start_threshold", "ceiling_threshold",
        "condition_label", "expected_value_yen", "estimated_play_minutes", "worst_case_investment_yen", "stop_rule",
        "source_name", "source_url", "discrepancy_note", "verified_on", "confidence",
        "duration_basis", "duration_confidence", "safe_play_minutes",
    }
    for row in rows:
        item = dict(row)
        profile = {key: item.get(key) for key in profile_keys} if item.get("profile_id") else None
        if profile is not None:
            try:
                profile["source_urls"] = json.loads(item.get("source_urls_json") or "[]")
                profile["curve_points"] = json.loads(item.get("curve_json") or "[]")
                profile["duration_model"] = json.loads(item.get("duration_model_json") or "{}")
                profile["input_fields"] = json.loads(item.get("input_fields_json") or "[]")
                profile["requirements"] = json.loads(item.get("requirements_json") or "[]")
            except json.JSONDecodeError:
                profile["source_urls"], profile["curve_points"] = [], []
                profile["duration_model"], profile["input_fields"], profile["requirements"] = {}, [], []
        try:
            context = json.loads(item.get("context_json") or "{}")
        except json.JSONDecodeError:
            context = {}
        assessment = assess_candidate(item, profile, summary["risk_capacity_yen"], context)
        item.pop("context_json", None)
        item.pop("source_urls_json", None)
        item.pop("curve_json", None)
        item.pop("duration_model_json", None)
        item.pop("input_fields_json", None)
        item.pop("requirements_json", None)
        candidates.append({**item, **assessment})
    candidates.sort(key=lambda item: (item["actionable"], item["priority"]), reverse=True)
    for rank, item in enumerate(candidates, 1):
        item["rank"] = rank
    recent_results = []
    for row in result_rows:
        item = dict(row)
        item["net_profit_yen"] = item["returns_yen"] - item["investment_yen"]
        recent_results.append(item)
    return {
        "summary": summary,
        "profiles": list_profiles(),
        "candidates": candidates,
        "recent_results": recent_results,
    }


init_db()
seed_catalog()
