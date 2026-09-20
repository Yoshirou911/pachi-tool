"""Fixed, paired benchmark; never promotes a model or changes live decisions.

The prospective protocol is frozen alongside forecasts. Retrospective replay
uses the same full search pipeline, but is research evidence, not live accuracy.
"""
from __future__ import annotations

import hashlib
import base64
import gzip
import json
import math
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

from hall.machine_scope import normalize_machine_key
from hall.names import canonical_hall_name
from hall.prediction_quality import audit_rows

PROTOCOL = "paired-baselines-v1"
MODELS = {"current": "現在の予測", "mean": "単純平均", "recent14": "直近14日平均", "weekday": "同じ曜日の平均"}
SPEC = {
    "protocol": PROTOCOL, "minimum_history_days": 14,
    "recent_calendar_days": 14, "weekday_minimum_days": 3,
    "weekday_fallback": "mean", "probability_prior": [2, 2],
    "signal_probability": 0.70, "signal_projected_min_exclusive": 0,
    "cohort": "all four models available; same date/hall/machine/seat",
    "outcome": "diff_coins > 0; missing/conflicting results remain pending",
}
_summary_cache = {}  # Small summaries only; never retain full 7-day input archives.


def replay_summary(result):
    return {k: v for k, v in result.items() if k not in {"folds", "samples"}}


def decode_replay_archive(payload):
    """Accept old uncompressed reports without rewriting immutable history."""
    stored = json.loads(payload)
    if stored.get("encoding") == "gzip-base64-v1":
        return json.loads(gzip.decompress(base64.b64decode(stored["archive"])))
    return stored


def subject_key(item):
    return (item["scope"], canonical_hall_name(item["hall_name"]),
            normalize_machine_key(item["machine_name"]), int(item.get("seat_number", 0)))


def _valid_prediction(value):
    try:
        return math.isfinite(float(value["projected"])) and 0 <= float(value["probability"]) <= 1
    except (KeyError, TypeError, ValueError):
        return False


def freeze_comparison(prediction):
    """Pure server-side baselines using only this batch's audited frozen inputs."""
    target = date.fromisoformat(prediction["visit_date"])
    cutoff = date.fromisoformat(prediction["input_cutoff_date"])
    if cutoff >= target:
        raise ValueError("benchmark input must precede target")
    history = defaultdict(list)
    for scope, name in (("machine", "machine_rows"), ("seat", "seat_rows")):
        rows, _ = audit_rows(prediction.get("frozen_inputs", {}).get(name, []), cutoff=cutoff, scope=scope)
        for row in rows:
            key = subject_key({**row, "scope": scope})
            history[key].append((date.fromisoformat(row["report_date"]),
                                 row["avg_diff_coins" if scope == "machine" else "diff_coins"]))
    samples, seen = [], set()
    for item in prediction.get("forecast_subjects", []):
        key = subject_key(item)
        if key in seen:
            raise ValueError("duplicate comparison subject")
        seen.add(key)
        points = sorted(history[key])
        models = {"current": {"projected": item["projected"], "probability": item["probability_pct"] / 100}}
        reasons = []
        if len(points) < SPEC["minimum_history_days"]:
            reasons.append("history_under_14_days")
        else:
            recent = [p for p in points if p[0] > cutoff - timedelta(days=14)]
            weekday = [p for p in points if p[0].weekday() == target.weekday()]
            fallback = len(weekday) < SPEC["weekday_minimum_days"]
            for name, group in (("mean", points), ("recent14", recent), ("weekday", points if fallback else weekday)):
                if not group:
                    reasons.append(f"{name}_unavailable")
                    continue
                values = [p[1] for p in group]
                models[name] = {"projected": sum(values) / len(values),
                                "probability": (sum(v > 0 for v in values) + 2) / (len(values) + 4),
                                "training_days": len(values), "last_input_date": group[-1][0].isoformat(),
                                "fallback": "mean" if name == "weekday" and fallback else None}
        if any(not _valid_prediction(value) for value in models.values()):
            reasons.append("invalid_prediction")
        samples.append({"scope": key[0], "hall_name": key[1], "machine_name": item["machine_name"],
                        "machine_key": key[2], "seat_number": key[3], "target_date": target.isoformat(),
                        "models": models, "comparable": not reasons and set(models) == set(MODELS),
                        "exclusion_reasons": reasons,
                        "live_recommended": bool(item["action"].startswith("狙う") and item["quality"]["analysis_eligible"])})
    return {"protocol": PROTOCOL, "spec": SPEC.copy(), "input_cutoff_date": cutoff.isoformat(), "samples": samples}


def _mean(values):
    return sum(values) / len(values) if values else None


def _rounded(value, digits=4):
    return round(value, digits) if value is not None else None


def _score(samples, name):
    answered = [s for s in samples if s.get("actual") is not None]
    signal = [s for s in samples if s["models"][name]["probability"] >= .70 and s["models"][name]["projected"] > 0]
    signal_answered = [s for s in signal if s.get("actual") is not None]
    days = defaultdict(list)
    for s in answered:
        days[s["target_date"]].append((s["models"][name]["probability"] - int(s["actual"] > 0)) ** 2)
    return {"model": name, "label": MODELS[name], "resolved": len(answered),
            "brier": _rounded(_mean([v for values in days.values() for v in values])),
            "daily_brier": _rounded(_mean([_mean(v) for v in days.values()])),
            "mae_coins": _rounded(_mean([abs(s["models"][name]["projected"] - s["actual"]) for s in answered]), 1),
            "direction_success_pct": _rounded(_mean([100 * ((s["models"][name]["projected"] > 0) == (s["actual"] > 0)) for s in answered]), 1),
            "signal_count": len(signal), "signal_resolved": len(signal_answered),
            "signal_pending": len(signal) - len(signal_answered),
            "signal_rate_pct": _rounded(100 * len(signal) / len(samples), 1) if samples else None,
            "signal_success_pct": _rounded(_mean([100 * (s["actual"] > 0) for s in signal_answered]), 1)}


def _paired(samples, baseline):
    """Equal-date loss differences, not IID confidence from correlated machines."""
    days = defaultdict(list)
    for s in samples:
        if s.get("actual") is None:
            continue
        y = int(s["actual"] > 0)
        days[s["target_date"]].append((s["models"]["current"]["probability"] - y) ** 2 -
                                       (s["models"][baseline]["probability"] - y) ** 2)
    differences = [_mean(v) for v in days.values()]
    return {"baseline": baseline, "days": len(days),
            "daily_brier_delta": _rounded(_mean(differences)),
            "better_days": sum(d < -1e-12 for d in differences),
            "worse_days": sum(d > 1e-12 for d in differences),
            "tied_days": sum(abs(d) <= 1e-12 for d in differences)}


def summarize(samples, *, mode):
    groups = defaultdict(list)
    for s in samples:
        groups[(s["scope"], s["version"])].append(s)
    cohorts = []
    for (scope, version), raw in sorted(groups.items()):
        common = [s for s in raw if s["comparable"] and set(s["models"]) == set(MODELS)
                  and all(_valid_prediction(v) for v in s["models"].values())]
        answered = [s for s in common if s.get("actual") is not None]
        breakdown = {}
        for dimension in ("hall_name", "machine_key"):
            partitions = defaultdict(list)
            for s in common:
                partitions[s[dimension]].append(s)
            breakdown[dimension] = [{"name": name if dimension == "hall_name" else values[0]["machine_name"],
                                     "predictions": len(values), "resolved": sum(s.get("actual") is not None for s in values),
                                     "days": len({s["target_date"] for s in values if s.get("actual") is not None}),
                                     "models": [_score(values, m) for m in MODELS]}
                                    for name, values in sorted(partitions.items())]
        reasons = defaultdict(int)
        for s in raw:
            for reason in s.get("exclusion_reasons", []):
                reasons[reason] += 1
        cohorts.append({"scope": scope, "version": version, "total": len(raw), "common": len(common),
                        "excluded": len(raw) - len(common), "exclusion_reasons": dict(reasons),
                        "resolved": len(answered), "pending": len(common) - len(answered),
                        "days": len({s["target_date"] for s in answered}),
                        "models": [_score(common, m) for m in MODELS],
                        "paired": [_paired(common, m) for m in MODELS if m != "current"],
                        "breakdown": breakdown})
    return {"protocol": PROTOCOL, "mode": mode, "spec": SPEC.copy(), "cohorts": cohorts,
            "notice": "差枚プラスの予測を比較。高設定的中率・本人勝率・利益保証ではありません。参考シグナルは着席推奨ではありません。",
            "method_notice": "全方式を計算できた同一対象だけで比較。欠損・矛盾は結果待ち。Brier・平均絶対誤差は小さい方が良好。日別Brierは各日を同じ重さで集計。",
            "selection_notice": "70%かつ予測差枚0枚超を全方式共通の参考シグナルとします。方式ごとに件数が違うためプラス率だけで優劣を決めません。自動採用なし。"}


def prospective_comparison(conn, *, region="shijonawate"):
    """Read-only. Never reconstruct missing baselines for pre-protocol batches."""
    samples, legacy = [], 0
    if conn is not None and conn.execute("SELECT 1 FROM sqlite_master WHERE name='prediction_batch'").fetchone():
        conn.row_factory = sqlite3.Row
        from hall.prediction_log import POLICY
        batches = conn.execute("SELECT * FROM prediction_batch WHERE region=? AND policy=? ORDER BY id", (region, POLICY)).fetchall()
        for b in batches:
            frozen = json.loads(b["payload"]).get("comparison")
            if not frozen or frozen.get("protocol") != PROTOCOL:
                legacy += 1
                continue
            actuals = {(r["scope"], r["hall_name"], r["machine_key"], r["seat_number"]): r["actual"] for r in conn.execute(
                "SELECT s.*,o.actual FROM prediction_subject s LEFT JOIN prediction_outcome o ON o.subject_id=s.id WHERE s.batch_id=?", (b["id"],))}
            for s in frozen["samples"]:
                samples.append({**s, "version": b["version"], "actual": actuals.get(subject_key(s))})
    return {**summarize(samples, mode="prospective"), "legacy_batches_excluded": legacy,
            "mode_notice": "比較方式も前日保存した予測だけを採点。比較値を保存していない旧版の日は後付けせず対象外。"}


def attach_actuals(comparison, actual_rows, *, version):
    """Exact match at the target date; never substitute a replacement machine."""
    samples = comparison["samples"]
    if not samples:
        return []
    target = samples[0]["target_date"]
    lookup, evidence = {}, {}
    for scope in ("machine", "seat"):
        raw = [r for r in actual_rows.get(scope, []) if r["report_date"] == target]
        rows, _ = audit_rows(raw, cutoff=date.fromisoformat(target), scope=scope)
        for r in rows:
            key = subject_key({**r, "scope": scope})
            lookup[key] = r["diff_coins" if scope == "seat" else "avg_diff_coins"]
            evidence[key] = r
    return [{**s, "version": version, "actual": lookup.get(subject_key(s)), "actual_evidence": evidence.get(subject_key(s))} for s in samples]


def run_replay(dates, build, actual_rows, *, version, progress=lambda done, total, day: None):
    """Sequential expanding-time replay of the real search, with fixed parameters.

    Previous-day 13:00 protocol: unfinished previous-day results are excluded.
    120-day lookback on every fold; no model selection based on test outcomes.
    Arrival times of historical sources are unknown: this is retrospective only.
    """
    if dates != sorted(set(dates)) or not dates:
        raise ValueError("ordered, distinct evaluation dates required")
    samples, folds = [], []
    for index, day in enumerate(dates):
        progress(index, len(dates), day)
        as_of = date.fromisoformat(day) - timedelta(days=1)
        prediction = build(day, 120, 20, "shijonawate", 70, include_inputs=True, as_of_date=as_of)
        if prediction.get("forecast_subjects"):
            if prediction["visit_date"] != day or prediction["input_cutoff_date"] >= as_of.isoformat():
                raise ValueError("replay input cutoff violation")
            for name in ("machine_rows", "seat_rows"):
                if any(r["report_date"] > prediction["input_cutoff_date"] for r in prediction.get("frozen_inputs", {}).get(name, [])):
                    raise ValueError("replay input contains future result")
            comparison = freeze_comparison(prediction)
            samples.extend(attach_actuals(comparison, actual_rows, version=version))
            folds.append({"date": day, "prediction": prediction, "comparison": comparison})
        else:
            folds.append({"date": day, "empty": True})
        progress(index + 1, len(dates), day)
    result = summarize(samples, mode="retrospective")
    result.update(dates=dates, samples=samples, folds=folds, version=version,
                  replay_policy="previous_day_13_JST_completed_days_only",
                  mode_notice="後から実施した過去検証です。当時の取得時刻や訂正前データは復元できず、本番の事前成績とは分離します。反復した同期間は未知期間の証明になりません。")
    return result


def store_replay(conn, result, *, created_at):
    from hall.prediction_log import dumps
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS prediction_benchmark_report(
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, version TEXT NOT NULL,
            protocol TEXT NOT NULL, payload_sha256 TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS benchmark_report_no_update BEFORE UPDATE ON prediction_benchmark_report
          BEGIN SELECT RAISE(ABORT,'benchmark report is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS benchmark_report_no_delete BEFORE DELETE ON prediction_benchmark_report
          BEGIN SELECT RAISE(ABORT,'benchmark report is immutable'); END;
    """)
    raw = dumps(result)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    # Overlapping 120-day inputs are highly compressible. Keep the audit trail,
    # but don't add tens of megabytes or reparse it on every progress poll.
    payload = dumps({"encoding": "gzip-base64-v1", "summary": replay_summary(result),
                     "archive": base64.b64encode(gzip.compress(raw.encode(), mtime=0)).decode("ascii")})
    with conn:
        cur = conn.execute("INSERT INTO prediction_benchmark_report(created_at,version,protocol,payload_sha256,payload) VALUES (?,?,?,?,?)",
                           (created_at, result["version"], PROTOCOL, digest, payload))
    return cur.lastrowid


def latest_replay(conn):
    if conn is None or not conn.execute("SELECT 1 FROM sqlite_master WHERE name='prediction_benchmark_report'").fetchone():
        return None
    row = conn.execute("SELECT id,created_at,payload_sha256 FROM prediction_benchmark_report WHERE protocol=? ORDER BY id DESC LIMIT 1", (PROTOCOL,)).fetchone()
    if not row:
        return None
    result = _summary_cache.get(row[2])
    if result is None:
        stored = json.loads(conn.execute("SELECT payload FROM prediction_benchmark_report WHERE id=?", (row[0],)).fetchone()[0])
        result = stored["summary"] if stored.get("encoding") == "gzip-base64-v1" else replay_summary(stored)
        if len(_summary_cache) >= 2:
            _summary_cache.clear()
        _summary_cache[row[2]] = result
    return {**result,
            "id": row[0], "created_at": row[1], "sha256": row[2]}
