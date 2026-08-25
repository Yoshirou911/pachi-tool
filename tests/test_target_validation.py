from datetime import date, timedelta

from hall.target_validation import (
    date_weighted_estimate,
    decide_action,
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


def test_walk_forward_backtest_rejects_bad_direction_accuracy():
    # 長いプラス傾向の後にマイナスへ反転。過去平均だけを信じる予測を見送りにする。
    validation = walk_forward_backtest(_points([500] * 30 + [-700] * 30))

    assert validation["status"] == "validated"
    assert validation["recommendation_success_pct"] < 50
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
