from datetime import date, timedelta

from hall.target_validation import (
    activity_filter_summary,
    build_daily_points,
    compare_prediction_models,
    date_weighted_estimate,
    decide_action,
    grade_policy,
    walk_forward_backtest,
)


def _points(values, start=date(2026, 1, 1)):
    return [(start + timedelta(days=index), value) for index, value in enumerate(values)]


def test_walk_forward_backtest_can_certify_consistent_signal():
    validation = walk_forward_backtest(_points([350] * 70))

    assert validation["status"] == "validated"
    assert validation["direction_accuracy_pct"] == 100
    assert validation["recommendation_success_pct"] == 100
    assert validation["trust_level"] == "90%級"
    action, reason = decide_action(300, 1, validation)
    assert action == "狙う・90%級"
    assert "成功率100%" in reason


def test_policy_exposes_seventy_percent_practical_gate():
    assert grade_policy()["70%実戦基準"] == {
        "recommended_days": 15,
        "success_pct": 70,
        "lower_bound_pct": 55,
        "recent_success_pct": 65,
        "quality_score": 70,
    }


def test_walk_forward_backtest_rejects_bad_direction_accuracy():
    # 長いプラス傾向の後にマイナスへ反転。過去平均だけを信じる予測を見送りにする。
    validation = walk_forward_backtest(_points([500] * 30 + [-700] * 30))

    assert validation["status"] == "validated"
    assert validation["strong_signal_success_pct"] < 50
    action, _ = decide_action(200, 1, validation)
    assert action == "見送り"


def test_insufficient_backtest_never_claims_high_trust():
    validation = walk_forward_backtest(_points([500] * 15))

    assert validation["status"] == "insufficient"
    assert validation["trust_level"] == "データ不足"
    action, _ = decide_action(500, 1, validation)
    assert action == "要確認"


def test_estimate_does_not_peek_at_target_day_or_future():
    target = date(2026, 2, 1)
    history = _points([100] * 31, start=date(2026, 1, 1))
    leaked = history + [(target, -9999), (target + timedelta(days=1), -9999)]

    clean_estimate = date_weighted_estimate(history, target)
    leaked_estimate = date_weighted_estimate(leaked, target)

    assert leaked_estimate == clean_estimate


def test_recent_instability_blocks_90_grade_even_when_long_term_is_strong():
    values = [350] * 70 + [-800 if index in {1, 6, 11, 16} else 350 for index in range(20)]
    validation = walk_forward_backtest(_points(values))

    assert validation["recommendation_success_pct"] >= 90
    assert validation["recent_recommendation_success_pct"] < 85
    assert validation["trust_level"] != "90%級"
    assert validation["confidence_interval_pct"] == 95
    assert validation["quality_score"] <= 100


def test_estimate_exposes_downside_and_risk_adjusted_forecast():
    target = date(2026, 3, 1)
    history = _points([400, 500, -1200, 350, 450] * 8)

    estimate = date_weighted_estimate(history, target)

    assert estimate["risk_adjusted_projected"] < estimate["projected"]
    assert estimate["downside_q25_coins"] <= 350
    assert 0 <= estimate["severe_loss_rate_pct"] <= 100
    assert isinstance(estimate["recommendation_ready"], bool)


def test_action_rejects_disagreeing_or_risky_current_signal():
    validation = walk_forward_backtest(_points([350] * 70))

    action, reason = decide_action(
        300,
        1,
        validation,
        70,
        {"signal_agreement_pct": 50, "risk_adjusted_projected": 250},
    )
    assert action == "見送り"
    assert "根拠一致" in reason

    action, reason = decide_action(
        300,
        1,
        validation,
        70,
        {"signal_agreement_pct": 80, "risk_adjusted_projected": 40},
    )
    assert action == "見送り"
    assert "安全側推定" in reason


def test_backtest_reports_skipped_days_and_recommended_downside():
    validation = walk_forward_backtest(_points([350] * 55 + [-900, 350] * 10))

    assert validation["skipped_days"] + validation["recommended_days"] == validation["test_days"]
    assert 0 <= validation["recommendation_rate_pct"] <= 100
    assert "recommended_avg_actual_coins" in validation
    assert "recommended_downside_q25_coins" in validation


def test_low_activity_rows_are_excluded_and_mid_activity_is_reduced():
    rows = [
        {"report_date": "2026-01-01", "avg_diff_coins": 3000, "unit_count": 10, "avg_games": 500},
        {"report_date": "2026-01-02", "avg_diff_coins": 400, "unit_count": 10, "avg_games": 1800},
        {"report_date": "2026-01-03", "avg_diff_coins": 200, "unit_count": 10, "avg_games": 4000},
    ]

    points = build_daily_points(rows)
    summary = activity_filter_summary(rows)

    assert [point[0].isoformat() for point in points] == ["2026-01-02", "2026-01-03"]
    assert summary["excluded_low_activity_rows"] == 1
    assert summary["reduced_weight_rows"] == 1


def test_model_selection_and_event_adjustment_never_read_target_or_future():
    target = date(2026, 4, 1)
    history = _points([350, -100, 450, 200, -50] * 18)
    event_dates = {history[index][0] for index in (5, 15, 25, 35, 45)} | {target}
    leaked = history + [(target, -9999), (target + timedelta(days=1), 9999)]

    clean = compare_prediction_models(history, target, event_dates=event_dates)
    dirty = compare_prediction_models(leaked, target, event_dates=event_dates)
    estimate = date_weighted_estimate(history, target, event_dates=event_dates)

    assert dirty == clean
    assert estimate["event_day"] is True
    assert estimate["historic_event_days"] == 5
    assert "event_adjustment_coins" in estimate


def test_auto_model_backtest_records_only_prior_selected_models():
    validation = walk_forward_backtest(
        _points([300, -100, 500, 250, -50] * 18), model="auto", max_test_days=30
    )

    assert validation["model"] == "auto"
    assert sum(validation["selected_model_counts"].values()) == validation["test_days"]
    assert set(validation["selected_model_counts"]).issubset({"balanced", "recent", "weekday", "calendar"})
