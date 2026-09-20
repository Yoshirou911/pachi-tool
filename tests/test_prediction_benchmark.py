import copy
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from hall.prediction_benchmark import (
    PROTOCOL, attach_actuals, decode_replay_archive, freeze_comparison, latest_replay, prospective_comparison,
    run_replay, store_replay, summarize,
)
from hall.prediction_log import JST, POLICY, initialize, save_batch


def fixture_prediction():
    rows = [{"hall_name": "キコーナ四条畷店", "machine_name": "L北斗", "report_date": (date(2026, 8, 11) + timedelta(days=i)).isoformat(),
             "avg_diff_coins": 100 if i % 2 else -100, "unit_count": 5, "avg_games": 4000} for i in range(28)]
    item = {"scope": "machine", "hall_name": "キコーナ四條畷店", "machine_name": "L北斗", "seat_number": 0,
            "projected": 50, "probability_pct": 80, "action": "見送り", "quality": {"analysis_eligible": True}}
    return {"visit_date": "2026-09-09", "input_cutoff_date": "2026-09-07", "region": "shijonawate",
            "frozen_inputs": {"machine_rows": rows, "seat_rows": []}, "forecast_subjects": [item]}


def test_baselines_exact_days_probability_and_aliases():
    f = freeze_comparison(fixture_prediction())
    s = f["samples"][0]
    assert s["comparable"] and not s["live_recommended"]
    assert s["models"]["mean"]["training_days"] == 28
    assert s["models"]["mean"]["projected"] == 0
    assert s["models"]["mean"]["probability"] == .5
    assert s["models"]["recent14"]["training_days"] == 14
    assert s["models"]["weekday"]["training_days"] == 4
    assert s["models"]["weekday"]["fallback"] is None


def test_future_and_same_day_never_enter_baselines():
    p = fixture_prediction()
    original = freeze_comparison(p)
    for day in ("2026-09-08", "2026-09-09", "2026-09-10"):
        p["frozen_inputs"]["machine_rows"].append({**p["frozen_inputs"]["machine_rows"][0], "report_date": day, "avg_diff_coins": 99999})
    assert freeze_comparison(p) == original


def test_sparse_weekday_falls_back_and_missing_recent_excludes():
    p = fixture_prediction()
    p["frozen_inputs"]["machine_rows"] = p["frozen_inputs"]["machine_rows"][-14:]
    s = freeze_comparison(p)["samples"][0]
    assert s["models"]["weekday"]["fallback"] == "mean"
    assert s["models"]["weekday"]["projected"] == s["models"]["mean"]["projected"]
    p["input_cutoff_date"] = "2026-10-01"
    p["visit_date"] = "2026-10-02"
    s = freeze_comparison(p)["samples"][0]
    assert not s["comparable"] and "recent14_unavailable" in s["exclusion_reasons"]


def test_missing_duplicates_conflicts_do_not_inflate_history():
    p = fixture_prediction()
    rows = p["frozen_inputs"]["machine_rows"]
    rows[:] = rows[:13]
    rows.extend(copy.deepcopy(rows))
    assert not freeze_comparison(p)["samples"][0]["comparable"]
    rows.append({**rows[0], "report_date": "2026-09-07", "avg_diff_coins": None})
    assert not freeze_comparison(p)["samples"][0]["comparable"]
    rows[-1]["avg_diff_coins"] = 0
    assert freeze_comparison(p)["samples"][0]["comparable"]
    rows.append({**rows[-1], "avg_diff_coins": 500})
    assert not freeze_comparison(p)["samples"][0]["comparable"]


def test_exact_outcomes_zero_missing_replacement_and_conflict():
    p = fixture_prediction()
    p["forecast_subjects"].append({**p["forecast_subjects"][0], "scope": "seat", "seat_number": 12})
    comp = freeze_comparison(p)
    actual = {**p["frozen_inputs"]["machine_rows"][0], "report_date": "2026-09-09", "avg_diff_coins": 0}
    result = attach_actuals(comp, {"machine": [actual], "seat": [{**actual, "machine_name": "L東京喰種", "seat_number": 12, "games": 4000, "diff_coins": 100}]}, version="3.24")
    assert result[0]["actual"] == 0 and result[1]["actual"] is None
    assert attach_actuals(comp, {"machine": [actual, {**actual, "avg_diff_coins": -100}]}, version="3.24")[0]["actual"] is None


def _samples():
    s = freeze_comparison(fixture_prediction())["samples"][0]
    return [{**s, "version": "3.24", "actual": 0}, {**s, "version": "3.24", "target_date": "2026-09-10", "actual": 100}]


def test_paired_denominators_loss_missing_and_empty_signals():
    samples = _samples()
    samples.append({**samples[0], "actual": None})
    samples.append({**samples[0], "comparable": False, "exclusion_reasons": ["history_under_14_days"]})
    c = summarize(samples, mode="retrospective")["cohorts"][0]
    assert (c["total"], c["common"], c["resolved"], c["pending"], c["excluded"], c["days"]) == (4, 3, 2, 1, 1, 2)
    current, mean = c["models"][:2]
    assert current["brier"] == .34 and mean["brier"] == .25
    assert current["mae_coins"] == 50
    assert current["signal_count"] == 3 and current["signal_pending"] == 1 and current["signal_success_pct"] == 50
    assert mean["signal_success_pct"] is None and mean["signal_count"] == 0
    assert c["paired"][0]["daily_brier_delta"] == .09
    assert c["paired"][0]["better_days"] == 1 and c["paired"][0]["worse_days"] == 1
    assert all(m["resolved"] == 2 for m in c["models"])


def test_days_equally_weighted_and_versions_scopes_not_pooled():
    samples = _samples()
    samples.extend([dict(samples[0]) for _ in range(9)])
    result = summarize(samples, mode="retrospective")["cohorts"][0]
    assert result["models"][0]["daily_brier"] == .34
    assert result["models"][0]["brier"] != .34
    samples.extend([{**samples[0], "version": "old"}, {**samples[0], "scope": "seat"}])
    assert len(summarize(samples, mode="retrospective")["cohorts"]) == 3


def test_no_answer_not_zero_accuracy():
    samples = [{**s, "actual": None} for s in _samples()]
    c = summarize(samples, mode="prospective")["cohorts"][0]
    assert c["resolved"] == 0 and c["pending"] == 2
    assert all(m["brier"] is None and m["signal_success_pct"] is None for m in c["models"])


def test_comparison_frozen_with_payload_and_old_batch_not_backfilled():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    p = fixture_prediction()
    now = datetime(2026, 9, 8, 13, tzinfo=JST)
    assert save_batch(conn, p, version="3.24", started_at=now, now=now)["status"] == "saved"
    before = conn.execute("SELECT payload FROM prediction_batch").fetchone()[0]
    assert json.loads(before)["comparison"]["protocol"] == PROTOCOL
    p["forecast_subjects"][0]["probability_pct"] = 99
    assert save_batch(conn, p, version="3.24", started_at=now, now=now)["status"] == "already_saved"
    assert conn.execute("SELECT payload FROM prediction_batch").fetchone()[0] == before
    conn.execute("INSERT INTO prediction_batch(target_date,region,policy,version,started_at,saved_at,cutoff_date,payload,payload_sha256) VALUES (?,?,?,?,?,?,?,?,?)",
                 ("2026-09-08", "shijonawate", POLICY, "3.23", "old", "old", "2026-09-06", "{}", "old"))
    summary = prospective_comparison(conn)
    assert summary["legacy_batches_excluded"] == 1
    assert summary["cohorts"][0]["common"] == 1
    assert summary["cohorts"][0]["pending"] == 1
    assert conn.execute("SELECT payload FROM prediction_batch ORDER BY id DESC").fetchone()[0] == "{}"
    conn.close()


def test_read_only_empty_no_schema_mutation():
    conn = sqlite3.connect(":memory:")
    assert prospective_comparison(conn)["cohorts"] == []
    assert latest_replay(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    conn.close()


def test_replay_uses_real_pipeline_contract_and_separate_immutable_report():
    calls, progress = [], []
    def build(*args, **kwargs):
        calls.append((args, kwargs))
        return fixture_prediction()
    report = run_replay(["2026-09-09"], build, {}, version="3.24", progress=lambda *args: progress.append(args))
    assert calls == [(("2026-09-09", 120, 20, "shijonawate", 70), {"include_inputs": True, "as_of_date": date(2026, 9, 8)})]
    assert progress == [(0, 1, "2026-09-09"), (1, 1, "2026-09-09")]
    conn = sqlite3.connect(":memory:")
    store_replay(conn, report, created_at="2026-09-10")
    loaded = latest_replay(conn)
    assert loaded["mode"] == "retrospective" and "folds" not in loaded and "samples" not in loaded
    saved_payload = conn.execute("SELECT payload FROM prediction_benchmark_report").fetchone()[0]
    assert json.loads(saved_payload)["encoding"] == "gzip-base64-v1"
    assert decode_replay_archive(saved_payload)["folds"][0]["prediction"] == fixture_prediction()
    assert decode_replay_archive(json.dumps(report)) == report
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='prediction_batch'").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE prediction_benchmark_report SET version='future'")
    conn.close()


def test_replay_rejects_wrong_target_and_future_cutoff():
    with pytest.raises(ValueError):
        run_replay(["2026-09-10"], lambda *a, **k: fixture_prediction(), {}, version="x")
    p = fixture_prediction()
    p["input_cutoff_date"] = p["visit_date"]
    with pytest.raises(ValueError):
        run_replay([p["visit_date"]], lambda *a, **k: p, {}, version="x")
    p["input_cutoff_date"] = "2026-09-08"  # not finished at previous-day 13:00
    with pytest.raises(ValueError):
        run_replay([p["visit_date"]], lambda *a, **k: p, {}, version="x")


def test_replay_rejects_future_rows_even_with_valid_cutoff():
    p = fixture_prediction()
    p["frozen_inputs"]["machine_rows"].append({**p["frozen_inputs"]["machine_rows"][0], "report_date": "2026-09-08"})
    with pytest.raises(ValueError, match="future result"):
        run_replay([p["visit_date"]], lambda *a, **k: p, {}, version="x")


def test_reject_duplicate_subject_instead_of_double_counting():
    p = fixture_prediction()
    p["forecast_subjects"].append(copy.deepcopy(p["forecast_subjects"][0]))
    with pytest.raises(ValueError, match="duplicate comparison subject"):
        freeze_comparison(p)


def test_search_as_of_uses_exact_120_calendar_days(tmp_path, monkeypatch):
    from api.routers import hall
    path = tmp_path / "range.db"
    def connect():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c
    c = connect()
    c.execute("CREATE TABLE hall_day_machine(hall_name TEXT,report_date TEXT,machine_name TEXT,avg_diff_coins REAL,win_rate_pct REAL,unit_count INTEGER,source_url TEXT,avg_games REAL)")
    cutoff = date(2026, 9, 7)
    start = cutoff - timedelta(days=119)
    for day in (start - timedelta(days=1), start, cutoff, cutoff + timedelta(days=1)):
        c.execute("INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?,?,?)", ("キコーナ四條畷店", day.isoformat(), "L北斗", 100, 50, 5, "source", 4000))
    c.commit()
    c.close()
    monkeypatch.setattr(hall, "_get_reports_conn", connect)
    result = hall._build_target_search("2026-09-09", 120, 20, "shijonawate", 70, include_inputs=True, as_of_date=date(2026, 9, 8))
    assert result["input_cutoff_date"] == "2026-09-07"
    assert [r["report_date"] for r in result["frozen_inputs"]["machine_rows"]] == [start.isoformat(), cutoff.isoformat()]


def test_future_as_of_rejected_before_opening_database(monkeypatch):
    from api.routers import hall
    from hall.prediction_log import jst_today
    monkeypatch.setattr(hall, "_get_reports_conn", lambda: pytest.fail("opened database before validation"))
    with pytest.raises(ValueError):
        hall._build_target_search("2026-09-09", 120, 20, "shijonawate", 70, as_of_date=jst_today() + timedelta(days=1))


def test_archive_compression_checksum_and_summary_cache():
    import hashlib
    from hall.prediction_log import dumps
    from hall.prediction_benchmark import _summary_cache
    report = {"version": "3.24", "cohorts": [], "folds": ["audited-input" * 10000], "samples": []}
    conn = sqlite3.connect(":memory:")
    store_replay(conn, report, created_at="test")
    digest, payload = conn.execute("SELECT payload_sha256,payload FROM prediction_benchmark_report").fetchone()
    assert digest == hashlib.sha256(dumps(decode_replay_archive(payload)).encode()).hexdigest()
    assert len(payload) < len(dumps(report)) / 10
    _summary_cache.clear()
    queries = []
    conn.set_trace_callback(queries.append)
    assert latest_replay(conn)["version"] == "3.24"
    first_count = sum("SELECT payload FROM" in sql for sql in queries)
    latest_replay(conn)
    assert first_count == 1 and sum("SELECT payload FROM" in sql for sql in queries) == 1
    assert "folds" not in _summary_cache[digest]
    conn.close()
