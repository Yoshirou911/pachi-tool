"""期待値狙い用SQLiteストレージと厳格な判定ロジック。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Generator, Optional

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

CREATE INDEX IF NOT EXISTS idx_opp_profiles_machine ON opportunity_profiles(machine_name, active);
CREATE INDEX IF NOT EXISTS idx_opp_candidates_status ON opportunity_candidates(status, observed_at);
CREATE INDEX IF NOT EXISTS idx_opp_results_date ON opportunity_results(played_on);
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
        }
        for name, sql_type in migrations.items():
            if name not in columns:
                con.execute(f"ALTER TABLE opportunity_profiles ADD COLUMN {name} {sql_type}")


def _row_dict(row: sqlite3.Row | None) -> Optional[dict]:
    return dict(row) if row is not None else None


def _deserialize_profile(row: sqlite3.Row | dict | None) -> Optional[dict]:
    if row is None:
        return None
    result = dict(row)
    for source_key, target_key in (("source_urls_json", "source_urls"), ("curve_json", "curve_points")):
        try:
            result[target_key] = json.loads(result.get(source_key) or "[]")
        except (TypeError, json.JSONDecodeError):
            result[target_key] = []
        result.pop(source_key, None)
    return result


def save_profile(data: dict) -> dict:
    columns = (
        "catalog_key", "machine_name", "condition_label", "exchange_type", "funding_mode", "reset_status",
        "metric_name", "unit_label", "start_threshold",
        "ceiling_threshold", "expected_value_yen", "estimated_play_minutes", "worst_case_investment_yen",
        "stop_rule", "source_name", "source_url", "source_urls_json", "curve_json",
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
    for profile in raw.get("profiles", []):
        key = profile.get("catalog_key")
        if not key:
            continue
        with _conn() as con:
            row = con.execute("SELECT id FROM opportunity_profiles WHERE catalog_key = ?", (key,)).fetchone()
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
            columns = (
                "machine_name", "condition_label", "exchange_type", "funding_mode", "reset_status",
                "metric_name", "unit_label", "start_threshold",
                "ceiling_threshold", "expected_value_yen", "estimated_play_minutes", "worst_case_investment_yen",
                "stop_rule", "source_name", "source_url", "source_urls_json", "curve_json",
                "verified_on", "confidence", "discrepancy_note", "notes",
            )
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
               (observed_at,hall_name,machine_name,seat_number,current_value,profile_id,notes)
               VALUES (?,?,?,?,?,?,?)""",
            (observed_at, data.get("hall_name", ""), data["machine_name"], data.get("seat_number"),
             data["current_value"], profile_id, data.get("notes", "")),
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


def assess_candidate(candidate: dict, profile: dict | None, risk_capacity_yen: int) -> dict:
    """根拠不足を勝手に補完せず、候補台を判定する。"""
    if not profile:
        return {"judgment": "unknown", "reason": "狙い目ルールが未登録です", "actionable": False, "priority": -1000}

    current = float(candidate["current_value"])
    start = float(profile["start_threshold"])
    ceiling = profile.get("ceiling_threshold")
    delta = current - start
    progress = None
    if ceiling is not None and float(ceiling) > 0:
        progress = min(100, max(0, round(current / float(ceiling) * 100)))

    if current < start:
        return {
            "judgment": "wait", "reason": f"開始ラインまであと{start - current:g}{profile['unit_label']}",
            "actionable": False, "priority": -100 + delta, "progress_pct": progress,
        }

    if (profile.get("confidence") not in {"official", "verified"}
            or not profile.get("verified_on") or not profile.get("source_name")):
        return {
            "judgment": "verify", "reason": "出典または確認日が不十分です",
            "actionable": False, "priority": -50 + delta, "progress_pct": progress,
        }

    curve = sorted(profile.get("curve_points") or [], key=lambda point: float(point["value"]))
    curve_point = next((point for point in reversed(curve) if float(point["value"]) <= current), None)
    expected = (curve_point.get("ev_yen") if curve_point else None)
    if expected is None:
        expected = profile.get("expected_value_yen")
    minutes = (curve_point.get("minutes") if curve_point else None) or profile.get("estimated_play_minutes")
    worst = (curve_point.get("worst_case_yen") if curve_point else None)
    if worst is None:
        worst = profile.get("worst_case_investment_yen")
    if worst is None:
        return {
            "judgment": "verify", "reason": "最悪投資額が未登録です",
            "actionable": False, "priority": -40 + delta, "progress_pct": progress,
        }
    if int(worst) > risk_capacity_yen:
        return {
            "judgment": "insufficient_funds",
            "reason": f"必要資金{int(worst):,}円に対し許容{risk_capacity_yen:,}円",
            "actionable": False, "priority": -20 + delta, "progress_pct": progress,
        }

    ev_per_hour = round(int(expected) * 60 / int(minutes)) if expected is not None and minutes else None
    priority = ev_per_hour if ev_per_hour is not None else (int(expected) if expected is not None else 0)
    return {
        "judgment": "target", "reason": "登録条件と資金条件を満たしています",
        "actionable": True, "priority": priority, "progress_pct": progress,
        "expected_value_yen": expected, "worst_case_investment_yen": worst,
        "ev_per_hour_yen": ev_per_hour,
        "matched_curve_value": curve_point.get("value") if curve_point else None,
    }


def assess_quick_decision(
    profile: dict | None,
    current_value: float,
    risk_capacity_yen: int,
    exchange_type: str,
    funding_mode: str,
    reset_status: str,
    minutes_until_close: int,
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
    if mismatches:
        return {
            "judgment": "condition_mismatch", "reason": "・".join(mismatches),
            "actionable": False, "priority": -60, "warnings": [],
        }

    assessment = assess_candidate(
        {"current_value": current_value}, profile, max(0, int(risk_capacity_yen)),
    )
    assessment["warnings"] = []
    assessment["minutes_until_close"] = minutes_until_close
    assessment["estimated_play_minutes"] = profile.get("estimated_play_minutes")

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

    estimated_minutes = profile.get("estimated_play_minutes")
    required_minutes = int(estimated_minutes) + 30 if estimated_minutes else 120
    assessment["required_minutes_with_buffer"] = required_minutes
    if assessment.get("judgment") == "target" and minutes_until_close < required_minutes:
        detail = (
            f"消化目安{int(estimated_minutes)}分＋余裕30分に対し、閉店まで{minutes_until_close}分"
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
        "metric_name", "unit_label", "start_threshold", "ceiling_threshold",
        "condition_label", "expected_value_yen", "estimated_play_minutes", "worst_case_investment_yen", "stop_rule",
        "source_name", "source_url", "discrepancy_note", "verified_on", "confidence",
    }
    for row in rows:
        item = dict(row)
        profile = {key: item.get(key) for key in profile_keys} if item.get("profile_id") else None
        if profile is not None:
            try:
                profile["source_urls"] = json.loads(item.get("source_urls_json") or "[]")
                profile["curve_points"] = json.loads(item.get("curve_json") or "[]")
            except json.JSONDecodeError:
                profile["source_urls"], profile["curve_points"] = [], []
        assessment = assess_candidate(item, profile, summary["risk_capacity_yen"])
        item.pop("source_urls_json", None)
        item.pop("curve_json", None)
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
