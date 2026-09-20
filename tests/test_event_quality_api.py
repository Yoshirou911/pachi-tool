import sqlite3
from datetime import date

from api.routers import events as events_router
from scraper import events as event_scraper


def _event_connection(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    event_scraper.init_event_db(conn)
    return conn


def test_event_quality_and_calendar_hide_quarantined_report_days(tmp_path, monkeypatch):
    database = tmp_path / "events.db"
    conn = _event_connection(database)
    conn.executemany(
        """
        INSERT INTO hall_event
          (hall_name,event_date,event_type,event_title,source,source_url)
        VALUES (?,?,?,?,?,?)
        """,
        [
            (
                "検証店", "2026-08-01", "通常イベント", "みんレポ記録日（8/1）",
                "minrepo", "https://min-repo.com/1",
            ),
            (
                "検証店", "2026-08-03", "新台入替", "新台入替",
                "pworld", "https://www.p-world.co.jp/test",
            ),
        ],
    )
    conn.commit()
    event_scraper._backfill_event_classification(conn)
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        events_router,
        "_get_event_conn",
        lambda: _event_connection(database),
    )
    monkeypatch.setattr(events_router, "_cache_get", lambda _key: None)
    monkeypatch.setattr(events_router, "_cache_set", lambda _key, _value: None)

    quality = events_router.get_event_quality(None)
    calendar = events_router.get_event_calendar("2026-08", None, False)
    full_calendar = events_router.get_event_calendar("2026-08", None, True)

    assert quality["total"] == 2
    assert quality["eligible"] == 1
    assert quality["excluded"] == 1
    assert quality["event_evidence"]["eligible"] == 0
    assert quality["day_summaries"]["records"] == 0
    assert list(calendar["events"]) == ["2026-08-03"]
    assert calendar["events"]["2026-08-03"][0]["source_trust"] == "A"
    assert set(full_calendar["events"]) == {"2026-08-01", "2026-08-03"}


def test_event_analysis_compares_same_hall_normal_days_and_lists_machines(tmp_path, monkeypatch):
    database = tmp_path / "analysis.db"
    conn = _event_connection(database)
    conn.execute(
        """
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT,
            avg_diff_coins REAL, unit_count INTEGER
        )
        """
    )
    event_dates = ["2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22", "2026-08-29"]
    normal_dates = ["2026-08-02", "2026-08-09", "2026-08-16", "2026-08-23", "2026-08-30"]
    for event_date in event_dates + ["2026-09-05"]:
        conn.execute(
            """INSERT INTO hall_event
                 (hall_name,event_date,event_type,event_title,source,source_url)
               VALUES (?,?,?,?,?,?)""",
            ("検証店", event_date, "通常イベント", "週末取材", "slomap", "https://slo-map.com/1"),
        )
    for report_date in event_dates:
        conn.execute(
            "INSERT INTO hall_day_machine VALUES (?,?,?,?,?)",
            ("検証店", report_date, "L北斗", 500, 10),
        )
    for report_date in normal_dates:
        conn.execute(
            "INSERT INTO hall_day_machine VALUES (?,?,?,?,?)",
            ("検証店", report_date, "L北斗", -100, 10),
        )
    event_scraper._backfill_event_classification(conn)
    conn.commit()
    conn.close()

    monkeypatch.setattr(events_router, "_get_event_conn", lambda: _event_connection(database))
    monkeypatch.setattr(events_router, "_get_reports_conn", lambda: _event_connection(database))

    result = events_router.get_event_analysis(
        "2026-09-01", "all", "検証店", 730, 31
    )

    assert result["summary"]["upcoming_count"] == 1
    assert result["upcoming"][0]["event_date"] == "2026-09-05"
    analysis = result["event_analysis"][0]
    assert analysis["matched_days"] == 5
    assert analysis["grade"] == "S"
    assert analysis["lift_vs_normal"] == 600
    assert analysis["strong_machines"][0]["machine_name"] == "L北斗"


def test_event_analysis_reconstructs_only_explicit_recurring_date_patterns(tmp_path, monkeypatch):
    database = tmp_path / "recurring.db"
    conn = _event_connection(database)
    conn.execute(
        """
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT,
            avg_diff_coins REAL, unit_count INTEGER
        )
        """
    )
    conn.execute(
        """INSERT INTO hall_event
             (hall_name,event_date,event_type,event_title,source,source_url)
           VALUES (?,?,?,?,?,?)""",
        ("検証店", "2026-09-03", "通常イベント", "旧イベ(3のつく日)",
         "slomap", "https://slo-map.com/1"),
    )
    for report_date, value in [
        ("2026-07-03", 400), ("2026-07-13", 500), ("2026-07-23", 600),
        ("2026-07-04", -100), ("2026-07-14", -100), ("2026-07-24", -100),
    ]:
        conn.execute(
            "INSERT INTO hall_day_machine VALUES (?,?,?,?,?)",
            ("検証店", report_date, "L北斗", value, 10),
        )
    event_scraper._backfill_event_classification(conn)
    conn.commit()
    conn.close()

    monkeypatch.setattr(events_router, "_get_event_conn", lambda: _event_connection(database))
    monkeypatch.setattr(events_router, "_get_reports_conn", lambda: _event_connection(database))
    result = events_router.get_event_analysis(
        "2026-09-02", "all", "検証店", 365, 31
    )
    analysis = result["event_analysis"][0]
    assert analysis["matched_days"] == 3
    assert analysis["event_avg_diff"] == 500
    assert analysis["normal_avg_diff"] == -100
    assert analysis["inferred_history_days"] > 20
    assert "3のつく日" in analysis["history_basis"]


def test_non_recurring_event_title_is_not_inferred():
    dates, basis = events_router._recurring_event_dates(
        "スロパチ来店取材", date(2026, 1, 1), date(2026, 8, 31)
    )
    assert dates == set()
    assert basis is None


def test_event_walk_forward_does_not_use_future_results():
    event_dates = [f"2026-0{month}-03" for month in range(1, 9)]
    hall_day_avg = {
        ("検証店", event_date): 300 for event_date in event_dates
    }
    for month in range(1, 9):
        for day in (1, 2, 4, 5):
            hall_day_avg[("検証店", f"2026-{month:02d}-{day:02d}")] = -100
    result = events_router._event_walk_forward(
        "検証店", event_dates, hall_day_avg, set(event_dates)
    )
    assert result["recommended_days"] == 5
    assert result["hits"] == 5
    assert result["success_pct"] == 100
    assert result["status"] == "検証済み"
    assert result["recent_answers"][0]["event_date"] == "2026-08-03"


def test_event_quality_gate_blocks_machine_subset_and_stale_data():
    gate = events_router._event_quality_gate(
        matched_days=12,
        normal_days=50,
        direct_event_days=0,
        eligible_evidence_records=0,
        latest_matched_date="2025-12-01",
        source_trust="C",
        backtest={"recommended_days": 8, "lower_bound_pct": 60},
        reference=date(2026, 9, 1),
    )
    assert gate["passed"] is False
    assert "店舗全体の結果が3日未満" in gate["blockers"]
    assert "実績が90日より古い" in gate["blockers"]


def test_event_quality_gate_and_baseline_allow_only_verified_evidence():
    gate = events_router._event_quality_gate(
        matched_days=8,
        normal_days=40,
        direct_event_days=8,
        eligible_evidence_records=6,
        latest_matched_date="2026-08-25",
        source_trust="C",
        backtest={"recommended_days": 6, "lower_bound_pct": 58},
        reference=date(2026, 9, 1),
    )
    forecast = events_router._event_baseline_forecast(
        "A", 180, 67, gate, {"recommended_days": 6, "lower_bound_pct": 58}
    )
    assert gate["passed"] is True
    assert gate["quality_score"] >= 70
    assert forecast["decision"] == "実戦候補"
    assert forecast["is_actionable"] is True


def test_event_baseline_caps_unverified_signal_below_actionable_range():
    gate = {
        "passed": False,
        "quality_score": 65,
    }
    forecast = events_router._event_baseline_forecast(
        "S", 900, 100, gate, {"recommended_days": 10, "lower_bound_pct": 80}
    )
    assert forecast["score"] == 49
    assert forecast["decision"] == "参考止まり"


def test_event_analysis_prefers_direct_hall_summary_over_machine_subset(tmp_path, monkeypatch):
    database = tmp_path / "direct-summary.db"
    conn = _event_connection(database)
    conn.execute(
        """CREATE TABLE hall_day_machine (
             hall_name TEXT, report_date TEXT, machine_name TEXT,
             avg_diff_coins REAL, unit_count INTEGER
           )"""
    )
    event_dates = ["2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22", "2026-08-29"]
    normal_dates = ["2026-08-02", "2026-08-09", "2026-08-16", "2026-08-23", "2026-08-30"]
    for event_date in event_dates:
        conn.execute(
            "INSERT INTO hall_event (hall_name,event_date,event_title,source) VALUES (?,?,?,?)",
            ("検証店", event_date, "週末取材", "slomap"),
        )
    for report_date in event_dates + normal_dates:
        conn.execute(
            "INSERT INTO hall_day_machine VALUES (?,?,?,?,?)",
            ("検証店", report_date, "L一部機種", 10, 2),
        )
        direct_value = 500 if report_date in event_dates else -100
        conn.execute(
            """INSERT INTO hall_source_day_summary
                 (source,hall_name,report_date,avg_diff_coins,total_diff_coins,unit_count,
                  evidence_scope,source_trust,analysis_eligible,quality_reason,source_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ("slomap", "検証店", report_date, direct_value, direct_value * 100, 100,
             "hall_all_reported_units", "C", 1, "test", "https://example.com"),
        )
    event_scraper._backfill_event_classification(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(events_router, "_get_event_conn", lambda: _event_connection(database))
    monkeypatch.setattr(events_router, "_get_reports_conn", lambda: _event_connection(database))

    result = events_router.get_event_analysis(
        "2026-09-01", "all", "検証店", 365, 31
    )
    analysis = result["event_analysis"][0]
    assert analysis["event_avg_diff"] == 500
    assert analysis["normal_avg_diff"] == -100
    assert analysis["lift_vs_normal"] == 600
    assert analysis["performance_basis"] == "店舗全体の日別差枚を優先"


def test_event_model_comparison_keeps_ai_out_until_fair_backtest():
    result = events_router._event_model_comparison({
        "event_analysis": [
            {"backtest": {"evaluated_days": 18, "recommended_days": 10, "hits": 8}},
            {"backtest": {"evaluated_days": 31, "recommended_days": 20, "hits": 15}},
        ],
    })
    assert result["baseline"]["recommended_trials"] == 30
    assert result["baseline"]["hits"] == 23
    assert result["baseline"]["success_pct"] == 77
    assert result["candidate"]["recommended_trials"] == 0
    assert result["candidate"]["changed_decisions"] == 0
    assert result["promotion_gate"]["passed"] is False
    assert "まだ判定を出していません" in result["promotion_gate"]["blockers"][0]


def test_event_model_evaluation_snapshot_is_saved(tmp_path, monkeypatch):
    database = tmp_path / "evaluation.db"
    monkeypatch.setattr(events_router, "_get_event_conn", lambda: _event_connection(database))
    comparison = events_router._event_model_comparison({
        "event_analysis": [
            {"backtest": {"evaluated_days": 8, "recommended_days": 5, "hits": 4}},
        ],
    })
    events_router._save_event_model_evaluation(
        comparison, region="shijonawate", reference_date="2026-09-01"
    )
    conn = _event_connection(database)
    row = conn.execute("SELECT * FROM event_model_evaluation").fetchone()
    conn.close()
    assert row["model_name"] == "説明可能ベースライン v1"
    assert row["recommended_trials"] == 5
    assert row["hits"] == 4
    assert row["promotion_status"] == "現行モデルを維持"


def test_production_prediction_is_frozen_then_resolved_from_later_result(tmp_path, monkeypatch):
    database = tmp_path / "production-audit.db"
    conn = _event_connection(database)
    conn.execute(
        """CREATE TABLE hall_day_machine (
             hall_name TEXT, report_date TEXT, machine_name TEXT,
             avg_diff_coins REAL, unit_count INTEGER
           )"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(events_router, "_get_event_conn", lambda: _event_connection(database))
    monkeypatch.setattr(events_router, "_get_reports_conn", lambda: _event_connection(database))
    analysis = {
        "upcoming": [{
            "event_date": "2026-09-03", "hall_name": "検証店", "event_name": "3のつく日",
            "baseline_forecast": {
                "model": "説明可能ベースライン v1", "decision": "実戦候補", "score": 81,
            },
            "quality_gate": {"passed": True},
        }],
    }
    assert events_router._record_event_predictions(
        analysis, region="shijonawate", prediction_date=date(2026, 9, 2)
    ) == 1
    # 同じ対象日の判定は、後から計算値が変わっても初回値を上書きしない。
    analysis["upcoming"][0]["baseline_forecast"]["decision"] = "参考止まり"
    assert events_router._record_event_predictions(
        analysis, region="shijonawate", prediction_date=date(2026, 9, 2)
    ) == 0
    conn = _event_connection(database)
    conn.execute(
        "INSERT INTO hall_day_machine VALUES (?,?,?,?,?)",
        ("検証店", "2026-09-03", "L北斗", 250, 10),
    )
    conn.commit()
    conn.close()
    assert events_router._resolve_event_prediction_outcomes(date(2026, 9, 4)) == 1
    audit = events_router._event_production_audit("shijonawate")
    assert audit["recommended_resolved"] == 1
    assert audit["hits"] == 1
    assert audit["success_pct"] == 100
