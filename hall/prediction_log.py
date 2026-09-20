"""Prospective, append-only prediction batches and independently stored outcomes.

Only server-computed tomorrow forecasts enter this ledger. Retrospective tests
and client-provided session snapshots never count as prospective evidence.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from hall.machine_scope import normalize_machine_key
from hall.names import canonical_hall_name
from hall.prediction_quality import audit_rows

JST = timezone(timedelta(hours=9))
POLICY = "smartslot-prospective-v1"


def jst_today():
    return datetime.now(JST).date()


def initialize(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS prediction_batch (
            id INTEGER PRIMARY KEY, target_date TEXT NOT NULL, region TEXT NOT NULL,
            policy TEXT NOT NULL, version TEXT NOT NULL, started_at TEXT NOT NULL,
            saved_at TEXT NOT NULL, cutoff_date TEXT NOT NULL, payload TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL, UNIQUE(target_date,region,policy)
        );
        CREATE TABLE IF NOT EXISTS prediction_subject (
            id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES prediction_batch(id),
            scope TEXT NOT NULL, hall_name TEXT NOT NULL, machine_key TEXT NOT NULL,
            seat_number INTEGER NOT NULL, projected REAL NOT NULL, probability REAL NOT NULL,
            recommended INTEGER NOT NULL, payload TEXT NOT NULL,
            UNIQUE(batch_id,scope,hall_name,machine_key,seat_number)
        );
        CREATE TABLE IF NOT EXISTS prediction_outcome (
            subject_id INTEGER PRIMARY KEY REFERENCES prediction_subject(id),
            actual REAL NOT NULL, positive INTEGER NOT NULL, resolved_at TEXT NOT NULL,
            evidence TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_prediction_subject_batch ON prediction_subject(batch_id);
        CREATE TRIGGER IF NOT EXISTS prediction_batch_no_update BEFORE UPDATE ON prediction_batch
          BEGIN SELECT RAISE(ABORT,'prediction is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS prediction_batch_no_delete BEFORE DELETE ON prediction_batch
          BEGIN SELECT RAISE(ABORT,'prediction is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS prediction_subject_no_update BEFORE UPDATE ON prediction_subject
          BEGIN SELECT RAISE(ABORT,'prediction is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS prediction_subject_no_delete BEFORE DELETE ON prediction_subject
          BEGIN SELECT RAISE(ABORT,'prediction is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS prediction_outcome_no_update BEFORE UPDATE ON prediction_outcome
          BEGIN SELECT RAISE(ABORT,'outcome is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS prediction_outcome_no_delete BEFORE DELETE ON prediction_outcome
          BEGIN SELECT RAISE(ABORT,'outcome is immutable'); END;
    """)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def save_batch(conn, prediction, *, version, started_at, now=None):
    now = now or datetime.now(JST)
    if now.tzinfo is None or started_at.tzinfo is None:
        raise ValueError("timezone required")
    target = date.fromisoformat(prediction["visit_date"])
    today = now.astimezone(JST).date()
    if target != today + timedelta(days=1) or started_at > now:
        raise ValueError("翌日の予測だけ事前保存できます。過去日への後付け保存はできません。")
    if not prediction.get("forecast_subjects"):
        return {"recorded": 0, "status": "no_candidates"}
    cutoff = date.fromisoformat(prediction["input_cutoff_date"])
    if cutoff >= started_at.astimezone(JST).date() or cutoff >= target:
        raise ValueError("当日途中・未来実績の混入")
    inputs = prediction.get("frozen_inputs")
    if not inputs or not (inputs.get("machine_rows") or inputs.get("seat_rows")):
        raise ValueError("予測入力の保存が必要です")
    for name in ("machine_rows", "seat_rows"):
        if any(date.fromisoformat(r["report_date"]) > cutoff for r in inputs.get(name, [])):
            raise ValueError("入力期限後の実績は保存できません")
    initialize(conn)
    # Freeze comparison forecasts at the same instant, never backfill old batches.
    from hall.prediction_benchmark import freeze_comparison
    from hall.model_selection import freeze_selection
    from hall.probability_validation import freeze_calibration
    from hall.candidate_evaluation import freeze_candidates
    prediction = {**prediction, "comparison": freeze_comparison(prediction),
                  "model_selection": freeze_selection(prediction)}
    prediction["probability_validation"] = freeze_calibration(conn, prediction, version=version, started_at=started_at)
    prediction["candidate_evaluation"] = freeze_candidates(prediction)
    payload = dumps(prediction)
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO prediction_batch(target_date,region,policy,version,started_at,saved_at,cutoff_date,payload,payload_sha256) VALUES (?,?,?,?,?,?,?,?,?)",
            (target.isoformat(), prediction["region"], POLICY, version, started_at.isoformat(), now.isoformat(), cutoff.isoformat(), payload, hashlib.sha256(payload.encode()).hexdigest()),
        )
        if not cur.rowcount:
            return {"recorded": 0, "status": "already_saved"}
        batch_id = cur.lastrowid
        count = 0
        for item in prediction["forecast_subjects"]:
            if item["scope"] not in {"machine", "seat"}:
                raise ValueError("unsupported forecast scope")
            probability = float(item["probability_pct"]) / 100
            if not 0 <= probability <= 1:
                raise ValueError("invalid probability")
            conn.execute(
                "INSERT INTO prediction_subject(batch_id,scope,hall_name,machine_key,seat_number,projected,probability,recommended,payload) VALUES (?,?,?,?,?,?,?,?,?)",
                (batch_id, item["scope"], canonical_hall_name(item["hall_name"]), normalize_machine_key(item["machine_name"]), int(item["seat_number"]), item["projected"], probability, int(item["action"].startswith("狙う") and item["quality"]["analysis_eligible"]), dumps(item)),
            )
            count += 1
    return {"recorded": count, "status": "saved", "target_date": target.isoformat()}


def resolve_outcomes(conn, *, today=None):
    today = today or jst_today()
    initialize(conn)
    conn.row_factory = sqlite3.Row
    pending = conn.execute("""SELECT s.*, b.target_date FROM prediction_subject s
        JOIN prediction_batch b ON b.id=s.batch_id
        LEFT JOIN prediction_outcome o ON o.subject_id=s.id
        WHERE o.subject_id IS NULL AND b.target_date < ?""", (today.isoformat(),)).fetchall()
    grouped = defaultdict(list)
    for row in pending:
        grouped[(row["target_date"], row["scope"])].append(row)
    resolved = 0
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with conn:
        for (target, scope), subjects in grouped.items():
            table = "hall_day_seat" if scope == "seat" else "hall_day_machine"
            if table not in tables:
                continue
            raw = conn.execute(f"SELECT * FROM {table} WHERE report_date=?", (target,)).fetchall()
            actuals, audit = audit_rows(raw, cutoff=date.fromisoformat(target), scope=scope)
            lookup = {(r["hall_name"], normalize_machine_key(r["machine_name"]), r.get("seat_number", 0)): r for r in actuals}
            for subject in subjects:
                actual = lookup.get((subject["hall_name"], subject["machine_key"], subject["seat_number"]))
                if actual is None:
                    continue  # missing/conflicting/replaced machine != loss
                diff = actual["diff_coins" if scope == "seat" else "avg_diff_coins"]
                cur = conn.execute(
                    "INSERT OR IGNORE INTO prediction_outcome VALUES (?,?,?,?,?)",
                    (subject["id"], diff, int(diff > 0), datetime.now(JST).isoformat(), dumps({"row": actual, "audit": audit})),
                )
                resolved += cur.rowcount
    return resolved


def production_summary(conn, *, region="shijonawate", limit=30):
    """Read only; never generate or resolve predictions during a GET."""
    empty = {"batches": 0, "by_scope": [], "recent": [], "latest_saved_at": None,
             "definition": "機種別の平均差枚／台番号の差枚が0枚超。高設定的中率・本人勝率ではありません。",
             "policy": POLICY, "scope_notice": "スマスロの機種・台番号を前日固定。店舗全体・ジャグラーはこの事前検証の対象外。"}
    if conn is None:
        return empty
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "prediction_batch" not in tables:
        return empty
    conn.row_factory = sqlite3.Row
    batches = conn.execute("SELECT COUNT(*),MAX(saved_at) FROM prediction_batch WHERE region=? AND policy=?", (region, POLICY)).fetchone()
    empty.update(batches=batches[0], latest_saved_at=batches[1])
    rows = conn.execute("""SELECT s.*,b.target_date,b.saved_at,b.version,o.actual,o.positive,o.resolved_at
        FROM prediction_subject s JOIN prediction_batch b ON b.id=s.batch_id
        LEFT JOIN prediction_outcome o ON o.subject_id=s.id
        WHERE b.region=? AND b.policy=? ORDER BY b.target_date DESC,s.id""", (region, POLICY)).fetchall()
    for scope in ("machine", "seat"):
        group = [r for r in rows if r["scope"] == scope]
        answered = [r for r in group if r["positive"] is not None]
        recommended = [r for r in answered if r["recommended"]]
        hits = sum(r["positive"] for r in recommended)
        n = len(recommended)
        p = hits / n if n else 0
        z = 1.96
        lower = ((p + z*z/(2*n) - z*((p*(1-p)/n + z*z/(4*n*n))**.5)) / (1+z*z/n) * 100) if n else None
        empty["by_scope"].append({
            "scope": scope, "predictions": len(group), "resolved": len(answered),
            "pending": len(group) - len(answered), "recommended": sum(r["recommended"] for r in group),
            "recommended_resolved": n, "hits": hits, "success_pct": round(p*100, 1) if n else None,
            "lower_bound_pct": round(lower, 1) if lower is not None else None,
            "distinct_evaluation_days": len({r["target_date"] for r in recommended}),
            "brier_score": round(sum((r["probability"]-r["positive"])**2 for r in answered)/len(answered), 4) if answered else None,
            "notice": "同日・同店舗の結果は相関します。件数だけで高信頼へ昇格させません。",
        })
    for row in rows[:limit]:
        item = json.loads(row["payload"])
        empty["recent"].append({
            "target_date": row["target_date"], "saved_at": row["saved_at"], "version": row["version"],
            "scope": row["scope"], "hall_name": row["hall_name"], "machine_name": item["machine_name"],
            "seat_number": row["seat_number"], "action": item["action"], "projected": row["projected"],
            "probability_pct": round(row["probability"]*100, 1), "actual": row["actual"],
            "result": "結果待ち" if row["positive"] is None else "プラス" if row["positive"] else "非プラス",
        })
    return empty
