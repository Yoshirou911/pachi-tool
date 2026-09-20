"""狙い台予測の時系列検証と、実戦可否の保守的な判定。"""
from __future__ import annotations

import math
from datetime import date
from statistics import median
from typing import Iterable, Mapping, Sequence


DailyPoint = tuple[date, float]

MODEL_POLICIES = {
    "balanced": {
        "label": "バランス型",
        "weights": {"base": 0.30, "recent": 0.30, "weekday": 0.28, "digit": 0.12},
    },
    "recent": {
        "label": "直近重視型",
        "weights": {"base": 0.25, "recent": 0.45, "weekday": 0.22, "digit": 0.08},
    },
    "weekday": {
        "label": "曜日重視型",
        "weights": {"base": 0.25, "recent": 0.22, "weekday": 0.45, "digit": 0.08},
    },
    "calendar": {
        "label": "日付傾向型",
        "weights": {"base": 0.28, "recent": 0.22, "weekday": 0.25, "digit": 0.25},
    },
}


def grade_policy() -> dict:
    """画面/APIで共有する高信頼ラベルの最低条件。"""
    return {
        "meaning": "勝率・高設定保証ではなく、推奨日の対象集計が平均差枚プラスだったかを先読みなしで検証したグレード",
        "success_definition": "推奨日に対象店舗・機種・台番号の集計平均差枚が0枚を超えた場合を成功とする",
        "confidence_interval_pct": 95,
        "90%級": {
            "recommended_days": 40,
            "success_pct": 90,
            "lower_bound_pct": 80,
            "recent_success_pct": 85,
            "quality_score": 90,
        },
        "80%級": {
            "recommended_days": 25,
            "success_pct": 82,
            "lower_bound_pct": 68,
            "recent_success_pct": 75,
            "quality_score": 80,
        },
        "70%実戦基準": {
            "recommended_days": 15,
            "success_pct": 70,
            "lower_bound_pct": 55,
            "recent_success_pct": 65,
            "quality_score": 70,
        },
    }


def _robust_mean(values: Sequence[float]) -> float:
    """極端な誤爆・欠損の影響を抑えた平均。少数時は通常平均を使う。"""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) < 8:
        return sum(ordered) / len(ordered)
    trim = max(1, math.floor(len(ordered) * 0.10))
    trimmed = ordered[trim:-trim]
    return sum(trimmed) / len(trimmed)


def _median_absolute_deviation(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    centre = median(values)
    return float(median(abs(float(value) - centre) for value in values))


def _percentile(values: Sequence[float], percentile: float) -> float:
    """外部ライブラリなしで線形補間パーセンタイルを返す。"""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _smoothed_positive_rate(values: Sequence[float]) -> float:
    """少数データが0%/100%へ張り付かないようBeta(2,2)で縮小する。"""
    wins = sum(float(value) > 0 for value in values)
    return (wins + 2) / (len(values) + 4) * 100


def _calibrate_probability_from_trials(raw_probability: float, trials: Sequence[Mapping]) -> dict:
    """過去に完了した近い確率帯だけで、その時点の確率を安全側へ補正する。"""
    raw = max(0.05, min(0.95, float(raw_probability)))
    nearby = [
        item for item in trials
        if abs(float(item.get("raw_predicted_probability", item.get("predicted_probability", 0.5))) - raw) <= 0.15
    ]
    if not nearby:
        return {"probability": raw, "sample": 0, "empirical_rate": None, "adjustment": 0.0, "applied": False}
    successes = sum(float(item.get("actual", 0)) > 0 for item in nearby)
    # Beta事前分布を現在の生確率に置き、少数結果への過剰適応を防ぐ。
    prior_strength = 10.0
    posterior = (raw * prior_strength + successes) / (prior_strength + len(nearby))
    reliability = len(nearby) / (len(nearby) + 12.0)
    calibrated = raw * (1 - reliability) + posterior * reliability
    # 過去の完了済み予測で補正後の方が悪ければ、以後は生確率へ戻す。
    if len(trials) >= 10:
        raw_brier = sum(
            (float(item.get("raw_predicted_probability", item["predicted_probability"])) - (1.0 if float(item["actual"]) > 0 else 0.0)) ** 2
            for item in trials
        ) / len(trials)
        calibrated_brier = sum(
            (float(item["predicted_probability"]) - (1.0 if float(item["actual"]) > 0 else 0.0)) ** 2
            for item in trials
        ) / len(trials)
        if calibrated_brier > raw_brier:
            return {
                "probability": raw, "sample": len(nearby),
                "empirical_rate": successes / len(nearby), "adjustment": 0.0,
                "applied": False,
            }
    return {
        "probability": max(0.05, min(0.95, calibrated)),
        "sample": len(nearby),
        "empirical_rate": successes / len(nearby),
        "adjustment": calibrated - raw,
        "applied": True,
    }


def _mapping_get(row: Mapping, key: str, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _activity_weight(row: Mapping) -> float:
    """平均回転数がある取得元だけ、低稼働を除外・減量する。"""
    raw_games = _mapping_get(row, "avg_games")
    if raw_games is None:
        return 1.0
    games = float(raw_games or 0)
    if games < 800:
        return 0.0
    if games >= 3500:
        return 1.0
    return 0.35 + (games - 800) / 2700 * 0.65


def activity_filter_summary(rows: Iterable[Mapping]) -> dict:
    source = list(rows)
    known = [row for row in source if _mapping_get(row, "avg_games") is not None]
    excluded = [row for row in known if _activity_weight(row) == 0]
    reduced = [row for row in known if 0 < _activity_weight(row) < 1]
    games = [float(_mapping_get(row, "avg_games") or 0) for row in known]
    return {
        "total_rows": len(source),
        "games_known_rows": len(known),
        "excluded_low_activity_rows": len(excluded),
        "reduced_weight_rows": len(reduced),
        "avg_games": round(sum(games) / len(games)) if games else None,
        "minimum_games": 800,
        "full_weight_games": 3500,
    }


def build_daily_points(rows: Iterable[Mapping]) -> list[DailyPoint]:
    """機種行を設置台数で加重し、店舗/機種の日別平均にまとめる。"""
    daily: dict[str, list[tuple[float, float]]] = {}
    for row in rows:
        activity_weight = _activity_weight(row)
        if activity_weight <= 0:
            continue
        unit_count = max(1, int(row["unit_count"] or 1))
        daily.setdefault(str(row["report_date"]), []).append(
            (float(row["avg_diff_coins"]), unit_count * activity_weight)
        )
    points = [
        (
            date.fromisoformat(day),
            sum(value * units for value, units in values)
            / sum(units for _, units in values),
        )
        for day, values in daily.items()
        if values
    ]
    points.sort(key=lambda item: item[0])
    return points


def date_weighted_estimate(
    source: Sequence[DailyPoint] | Iterable[Mapping],
    target_date: date,
    *,
    model: str = "balanced",
    event_dates: set[date] | None = None,
) -> dict:
    """全体・直近・曜日・日付末尾から、対象日の差枚を縮小推定する。"""
    source_list = list(source)
    if source_list and isinstance(source_list[0], tuple):
        points = sorted(source_list, key=lambda item: item[0])
    else:
        points = build_daily_points(source_list)

    # 対象日以降を混ぜない。過去日の再計算でも答えを先読みしないための境界。
    points = [(day, value) for day, value in points if day < target_date]
    values = [value for _, value in points]
    weekday_values = [value for day, value in points if day.weekday() == target_date.weekday()]
    digit_values = [value for day, value in points if day.day % 10 == target_date.day % 10]
    recent_values = [value for _, value in points[-min(14, len(points)):]]
    previous_values = [value for _, value in points[-44:-14]] if len(points) > 14 else []

    policy = MODEL_POLICIES.get(model, MODEL_POLICIES["balanced"])
    model = model if model in MODEL_POLICIES else "balanced"
    weights = policy["weights"]
    base_avg = _robust_mean(values)
    components = [("全期間", base_avg, weights["base"])]
    if recent_values:
        recent_weights = list(range(1, len(recent_values) + 1))
        recent_avg = sum(
            value * weight for value, weight in zip(recent_values, recent_weights)
        ) / sum(recent_weights)
        recent_confidence = min(1.0, len(recent_values) / 14)
        components.append(("直近14日", recent_avg, weights["recent"] * recent_confidence))
    # 少数の曜日・末尾実績を強く効かせると偶然を学習するため、4回未満は重みを半減する。
    if weekday_values:
        components.append(
            (
                "曜日",
                _robust_mean(weekday_values),
                weights["weekday"] * min(1.0, len(weekday_values) / 8),
            )
        )
    if digit_values:
        components.append(
            (
                "日付末尾",
                _robust_mean(digit_values),
                weights["digit"] * min(1.0, len(digit_values) / 8),
            )
        )
    weight_total = sum(weight for _, _, weight in components) or 1.0
    projected = sum(value * weight for _, value, weight in components) / weight_total
    projected_positive = projected >= 0
    agreement_weight = sum(
        weight for _, value, weight in components if (value >= 0) == projected_positive
    )
    signal_agreement = agreement_weight / weight_total * 100

    rate_components = [(base_positive := _smoothed_positive_rate(values), weights["base"])]
    if recent_values:
        rate_components.append(
            (_smoothed_positive_rate(recent_values), weights["recent"] * min(1.0, len(recent_values) / 14))
        )
    if weekday_values:
        rate_components.append(
            (_smoothed_positive_rate(weekday_values), weights["weekday"] * min(1.0, len(weekday_values) / 8))
        )
    if digit_values:
        rate_components.append(
            (_smoothed_positive_rate(digit_values), weights["digit"] * min(1.0, len(digit_values) / 8))
        )
    rate_weight_total = sum(weight for _, weight in rate_components) or 1.0
    positive_rate = sum(value * weight for value, weight in rate_components) / rate_weight_total
    volatility = _median_absolute_deviation(values)
    downside_q25 = _percentile(values, 0.25)
    severe_loss_line = -max(500.0, volatility * 2)
    severe_loss_rate = (
        sum(value <= severe_loss_line for value in values) / len(values) * 100
        if values else 0.0
    )
    recent_avg = _robust_mean(recent_values) if recent_values else base_avg
    previous_avg = _robust_mean(previous_values) if previous_values else base_avg
    regime_gap = abs(recent_avg - previous_avg)
    regime_scale = max(250.0, _median_absolute_deviation(values) * 1.5)
    regime_stability = max(0.0, min(100.0, 100.0 - regime_gap / regime_scale * 40.0))
    regime_shift = (
        len(recent_values) >= 7
        and len(previous_values) >= 14
        and regime_stability < 55
    )
    # 推定値から日々のブレと下側分布を少し差し引き、着席判断を安全側へ寄せる。
    event_dates = event_dates or set()
    historic_event_values = [value for day, value in points if day in event_dates]
    event_adjustment = 0.0
    event_avg = None
    if target_date in event_dates and len(historic_event_values) >= 3:
        event_avg = _robust_mean(historic_event_values)
        event_lift = event_avg - base_avg
        event_confidence = min(0.60, len(historic_event_values) / (len(historic_event_values) + 8))
        event_adjustment = max(-300.0, min(300.0, event_lift * event_confidence))
        projected += event_adjustment
    risk_adjusted = projected - volatility * 0.20 - max(0.0, -downside_q25) * 0.05
    recommendation_ready = (
        len(points) >= 21
        and risk_adjusted >= 80
        and positive_rate >= 58
        and signal_agreement >= 65
        and not regime_shift
    )
    strong_ready = (
        len(points) >= 30
        and risk_adjusted >= 150
        and positive_rate >= 60
        and signal_agreement >= 70
        and severe_loss_rate < 35
        and not regime_shift
    )
    return {
        "projected": round(projected),
        "risk_adjusted_projected": round(risk_adjusted),
        "base_avg": round(base_avg),
        "positive_rate": round(positive_rate),
        "sample_days": len(points),
        "weekday_days": len(weekday_values),
        "digit_days": len(digit_values),
        "recent_days": len(recent_values),
        "weekday_avg": round(_robust_mean(weekday_values)) if weekday_values else None,
        "digit_avg": round(_robust_mean(digit_values)) if digit_values else None,
        "latest_date": points[-1][0].isoformat() if points else "",
        "signal_agreement_pct": round(signal_agreement),
        "volatility_coins": round(volatility),
        "downside_q25_coins": round(downside_q25),
        "severe_loss_rate_pct": round(severe_loss_rate),
        "recent_avg_coins": round(recent_avg),
        "previous_avg_coins": round(previous_avg),
        "regime_stability_pct": round(regime_stability),
        "regime_shift_detected": regime_shift,
        "recommendation_ready": recommendation_ready,
        "strong_signal_ready": strong_ready,
        "event_day": target_date in event_dates,
        "historic_event_days": len(historic_event_values),
        "historic_event_avg_coins": round(event_avg) if event_avg is not None else None,
        "event_adjustment_coins": round(event_adjustment),
        "model": model,
        "model_label": policy["label"],
        "components": [
            {"name": name, "estimate": round(value), "weight_pct": round(weight / weight_total * 100)}
            for name, value, weight in components
        ],
    }


def _wilson_lower(successes: int, trials: int, z: float = 1.96) -> float:
    """成功率の95% Wilson下限。少数サンプルを過信しない。"""
    if trials <= 0:
        return 0.0
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = p + z * z / (2 * trials)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials)
    return max(0.0, (centre - margin) / denominator)


def walk_forward_backtest(
    source: Sequence[DailyPoint] | Iterable[Mapping],
    *,
    min_train_days: int = 21,
    max_test_days: int = 90,
    model: str = "balanced",
    event_dates: set[date] | None = None,
) -> dict:
    """各日を当時までのデータだけで予測し、先読みなしで成績を測る。"""
    source_list = list(source)
    if source_list and isinstance(source_list[0], tuple):
        points = sorted(source_list, key=lambda item: item[0])
    else:
        points = build_daily_points(source_list)

    trials = []
    selected_model_counts: dict[str, int] = {}
    start_index = max(min_train_days, len(points) - max_test_days)
    active_auto_model: str | None = None
    active_auto_weights: dict[str, float] | None = None
    auto_review_interval_days = 7
    for trial_number, index in enumerate(range(start_index, len(points))):
        test_date, actual = points[index]
        training = points[:index]
        trial_model = model
        if model == "auto":
            # 日々の偶然のブレに追従し過ぎないよう、方式の再選定は週1回。
            # 再選定時にも当日以降は使わないため、先読みは発生しない。
            if active_auto_model is None or trial_number % auto_review_interval_days == 0:
                selection = compare_prediction_models(
                    training,
                    test_date,
                    max_test_days=45,
                    event_dates=event_dates,
                )
                active_auto_model = selection["selected_model"]
                active_auto_weights = selection["ensemble_weights"]
            trial_model = active_auto_model
            selected_model_counts[trial_model] = selected_model_counts.get(trial_model, 0) + 1
        prediction = (
            weighted_ensemble_estimate(
                training,
                test_date,
                model_weights=active_auto_weights,
                event_dates=event_dates,
            )
            if model == "auto"
            else date_weighted_estimate(
                training, test_date, model=trial_model, event_dates=event_dates
            )
        )
        predicted = float(prediction["projected"])
        raw_probability = max(0.05, min(0.95, prediction["positive_rate"] / 100))
        calibration = _calibrate_probability_from_trials(raw_probability, trials)
        predicted_probability = calibration["probability"]
        consensus = int(prediction.get("model_consensus_pct", 100))
        predicted_positive = (
            prediction["sample_days"] >= 21
            and prediction["risk_adjusted_projected"] >= 80
            and predicted_probability >= 0.58
            and prediction["signal_agreement_pct"] >= 65
            and consensus >= 75
            and not prediction.get("regime_shift_detected")
        )
        actual_positive = actual > 0
        strong_signal = (
            prediction["sample_days"] >= 30
            and prediction["risk_adjusted_projected"] >= 150
            and predicted_probability >= 0.60
            and prediction["signal_agreement_pct"] >= 70
            and consensus >= 80
            and prediction["severe_loss_rate_pct"] < 35
            and not prediction.get("regime_shift_detected")
        )
        direction_prediction = predicted >= 0
        trials.append(
            {
                "date": test_date.isoformat(),
                "predicted": round(predicted),
                "actual": round(actual),
                "direction_correct": direction_prediction == actual_positive,
                "recommended": predicted_positive,
                "recommended_success": predicted_positive and actual_positive,
                "strong_signal": strong_signal,
                "strong_signal_success": strong_signal and actual_positive,
                "raw_predicted_probability": round(raw_probability, 3),
                "predicted_probability": round(predicted_probability, 3),
                "calibration_sample": calibration["sample"],
                "calibration_adjustment_pct": round(calibration["adjustment"] * 100, 1),
                "calibration_applied": calibration["applied"],
                "signal_agreement_pct": prediction["signal_agreement_pct"],
                "model_consensus_pct": prediction.get("model_consensus_pct", 100),
                "risk_adjusted_predicted": prediction["risk_adjusted_projected"],
                "downside_q25_coins": prediction["downside_q25_coins"],
                "model": trial_model,
            }
        )

    test_days = len(trials)
    direction_successes = sum(item["direction_correct"] for item in trials)
    recommended = [item for item in trials if item["recommended"]]
    recommended_successes = sum(item["recommended_success"] for item in recommended)
    strong = [item for item in trials if item["strong_signal"]]
    strong_successes = sum(item["strong_signal_success"] for item in strong)
    mae = (
        sum(abs(item["predicted"] - item["actual"]) for item in trials) / test_days
        if test_days
        else None
    )
    recent_recommended = recommended[-20:]
    recent_successes = sum(item["recommended_success"] for item in recent_recommended)
    recent_precision = recent_successes / len(recent_recommended) if recent_recommended else 0.0
    enough = test_days >= 30 and len(recommended) >= 10
    precision = recommended_successes / len(recommended) if recommended else 0.0
    direction = direction_successes / test_days if test_days else 0.0
    lower_bound = _wilson_lower(recommended_successes, len(recommended))
    recent_lower_bound = _wilson_lower(recent_successes, len(recent_recommended))
    strong_precision = strong_successes / len(strong) if strong else 0.0
    recommended_actuals = [float(item["actual"]) for item in recommended]
    recommended_avg_actual = (
        sum(recommended_actuals) / len(recommended_actuals) if recommended_actuals else None
    )
    recommended_downside_q25 = (
        _percentile(recommended_actuals, 0.25) if recommended_actuals else None
    )
    brier_score = (
        sum(
            (item["predicted_probability"] - (1.0 if item["actual"] > 0 else 0.0)) ** 2
            for item in trials
        ) / test_days
        if test_days else None
    )
    raw_brier_score = (
        sum(
            (item["raw_predicted_probability"] - (1.0 if item["actual"] > 0 else 0.0)) ** 2
            for item in trials
        ) / test_days
        if test_days else None
    )
    calibration_bins = []
    for lower, upper in ((0.0, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.01)):
        members = [
            item for item in trials
            if lower <= item["raw_predicted_probability"] < upper
        ]
        if not members:
            continue
        successes = sum(item["actual"] > 0 for item in members)
        calibration_bins.append({
            "lower_pct": round(lower * 100),
            "upper_pct": min(100, round(upper * 100)),
            "count": len(members),
            "successes": successes,
            "raw_avg_pct": round(sum(item["raw_predicted_probability"] for item in members) / len(members) * 100),
            "calibrated_avg_pct": round(sum(item["predicted_probability"] for item in members) / len(members) * 100),
            "actual_pct": round(successes / len(members) * 100),
        })
    residuals = [float(item["actual"] - item["predicted"]) for item in trials]
    sample_score = min(100.0, len(recommended) / 40 * 100)
    quality_score = round(
        precision * 30
        + lower_bound * 25
        + recent_precision * 20
        + direction * 15
        + sample_score * 0.10
    ) if enough else round(min(49, test_days / 30 * 25 + len(recommended) / 10 * 24))
    grade90 = (
        enough and len(recommended) >= 40 and precision >= 0.90
        and lower_bound >= 0.80 and recent_precision >= 0.85 and quality_score >= 90
    )
    grade80 = (
        enough and len(recommended) >= 25 and precision >= 0.82
        and lower_bound >= 0.68 and recent_precision >= 0.75 and quality_score >= 80
    )
    grade70 = (
        enough and len(recommended) >= 15 and precision >= 0.70
        and lower_bound >= 0.55 and recent_precision >= 0.65 and quality_score >= 70
    )

    return {
        "status": "validated" if enough else "insufficient",
        "success_definition": "推奨日の対象集計平均差枚が0枚を超えた割合（高設定的中率・本人勝率ではない）",
        "method": "walk_forward",
        "model": model,
        "model_label": (
            "店舗別モデル自動競争"
            if model == "auto"
            else MODEL_POLICIES.get(model, MODEL_POLICIES["balanced"])["label"]
        ),
        "selected_model_counts": selected_model_counts,
        "auto_review_interval_days": auto_review_interval_days if model == "auto" else None,
        "test_days": test_days,
        "train_min_days": min_train_days,
        "direction_accuracy_pct": round(direction * 100),
        "recommended_days": len(recommended),
        "recommendation_success_pct": round(precision * 100) if recommended else None,
        "recommendation_lower_bound_pct": round(lower_bound * 100) if recommended else None,
        "confidence_interval_pct": 95,
        "recent_recommended_days": len(recent_recommended),
        "recent_recommendation_success_pct": round(recent_precision * 100) if recent_recommended else None,
        "recent_recommendation_lower_bound_pct": round(recent_lower_bound * 100) if recent_recommended else None,
        "strong_signal_days": len(strong),
        "strong_signal_success_pct": round(strong_precision * 100) if strong else None,
        "skipped_days": test_days - len(recommended),
        "recommendation_rate_pct": round(len(recommended) / test_days * 100) if test_days else 0,
        "recommended_avg_actual_coins": round(recommended_avg_actual) if recommended_avg_actual is not None else None,
        "recommended_downside_q25_coins": round(recommended_downside_q25) if recommended_downside_q25 is not None else None,
        "mae_coins": round(mae) if mae is not None else None,
        "brier_score": round(brier_score, 3) if brier_score is not None else None,
        "raw_brier_score": round(raw_brier_score, 3) if raw_brier_score is not None else None,
        "calibration_bins": calibration_bins,
        "calibration_method": "過去時点までの近接確率帯＋Beta縮小",
        "residual_q10_coins": round(_percentile(residuals, 0.10)) if residuals else None,
        "residual_q90_coins": round(_percentile(residuals, 0.90)) if residuals else None,
        "quality_score": quality_score,
        "trust_level": (
            "90%級" if grade90
            else "80%級" if grade80
            else "70%実戦基準" if grade70
            else "検証済み" if enough
            else "データ不足"
        ),
        "recent_trials": trials[-10:],
    }


def weighted_ensemble_estimate(
    source: Sequence[DailyPoint] | Iterable[Mapping],
    target_date: date,
    *,
    model_weights: Mapping[str, float] | None = None,
    event_dates: set[date] | None = None,
) -> dict:
    """検証成績で複数方式を合成し、単一モデルの偶然の1位に依存しない。"""
    requested = {
        model: max(0.0, float(weight))
        for model, weight in (model_weights or {"balanced": 1.0}).items()
        if model in MODEL_POLICIES and float(weight) > 0
    }
    if not requested:
        requested = {"balanced": 1.0}
    total = sum(requested.values()) or 1.0
    weights = {model: weight / total for model, weight in requested.items()}
    estimates = {
        model: date_weighted_estimate(source, target_date, model=model, event_dates=event_dates)
        for model in weights
    }

    def blended(key: str, default: float = 0.0) -> float:
        return sum(float(estimates[model].get(key) or default) * weight for model, weight in weights.items())

    anchor = estimates.get("balanced") or next(iter(estimates.values()))
    projected = blended("projected")
    positive_rate = blended("positive_rate")
    risk_adjusted = blended("risk_adjusted_projected")
    direction_positive = projected >= 0
    consensus = sum(
        weight for model, weight in weights.items()
        if (float(estimates[model]["projected"]) >= 0) == direction_positive
    ) * 100
    component_agreement = blended("signal_agreement_pct")
    signal_agreement = min(consensus, component_agreement)
    sample_days = int(anchor["sample_days"])
    severe_loss_rate = blended("severe_loss_rate_pct")
    regime_stability = blended("regime_stability_pct", 100.0)
    regime_shift = any(
        estimates[model].get("regime_shift_detected") and weight >= 0.20
        for model, weight in weights.items()
    ) or regime_stability < 55
    recommendation_ready = (
        sample_days >= 21
        and risk_adjusted >= 80
        and positive_rate >= 58
        and signal_agreement >= 65
        and consensus >= 75
        and not regime_shift
    )
    strong_ready = (
        sample_days >= 30
        and risk_adjusted >= 150
        and positive_rate >= 60
        and signal_agreement >= 70
        and consensus >= 80
        and severe_loss_rate < 35
        and not regime_shift
    )
    result = dict(anchor)
    result.update({
        "projected": round(projected),
        "risk_adjusted_projected": round(risk_adjusted),
        "base_avg": round(blended("base_avg")),
        "positive_rate": round(positive_rate),
        "signal_agreement_pct": round(signal_agreement),
        "model_consensus_pct": round(consensus),
        "volatility_coins": round(blended("volatility_coins")),
        "downside_q25_coins": round(blended("downside_q25_coins")),
        "severe_loss_rate_pct": round(severe_loss_rate),
        "recent_avg_coins": round(blended("recent_avg_coins")),
        "previous_avg_coins": round(blended("previous_avg_coins")),
        "regime_stability_pct": round(regime_stability),
        "regime_shift_detected": regime_shift,
        "event_adjustment_coins": round(blended("event_adjustment_coins")),
        "recommendation_ready": recommendation_ready,
        "strong_signal_ready": strong_ready,
        "model": "ensemble",
        "model_label": "検証加重アンサンブル",
        "model_weights": {model: round(weight * 100) for model, weight in weights.items()},
        "components": [
            {
                "name": estimates[model]["model_label"],
                "estimate": estimates[model]["projected"],
                "weight_pct": round(weight * 100),
            }
            for model, weight in sorted(weights.items(), key=lambda item: item[1], reverse=True)
        ],
    })
    return result


def finalize_prediction_estimate(estimate: Mapping, validation: Mapping) -> dict:
    """先読みなし検証で確率を補正し、現在予測の区間と最終ゲートを確定する。"""
    result = dict(estimate)
    raw_pct = float(estimate.get("positive_rate") or 0)
    matched_bin = next((
        item for item in validation.get("calibration_bins", [])
        if float(item["lower_pct"]) <= raw_pct < float(item["upper_pct"])
    ), None)
    calibrated_pct = raw_pct
    brier = validation.get("brier_score")
    raw_brier = validation.get("raw_brier_score")
    calibration_improved = (
        brier is not None and raw_brier is not None
        and float(brier) <= float(raw_brier)
    )
    calibration_applied = bool(
        matched_bin and int(matched_bin.get("count") or 0) >= 5 and calibration_improved
    )
    if calibration_applied:
        count = int(matched_bin["count"])
        successes = int(matched_bin["successes"])
        prior_strength = 10.0
        posterior_pct = (raw_pct * prior_strength + successes * 100) / (prior_strength + count)
        reliability = count / (count + 12.0)
        calibrated_pct = raw_pct * (1 - reliability) + posterior_pct * reliability
    calibrated_pct = max(5.0, min(95.0, calibrated_pct))

    residual_low = validation.get("residual_q10_coins")
    residual_high = validation.get("residual_q90_coins")
    projected = int(estimate.get("projected") or 0)
    consensus = int(estimate.get("model_consensus_pct", 100))
    regime_shift = bool(estimate.get("regime_shift_detected"))
    recommendation_ready = (
        int(estimate.get("sample_days") or 0) >= 21
        and int(estimate.get("risk_adjusted_projected") or 0) >= 80
        and calibrated_pct >= 58
        and int(estimate.get("signal_agreement_pct") or 0) >= 65
        and consensus >= 75
        and not regime_shift
    )
    strong_ready = (
        int(estimate.get("sample_days") or 0) >= 30
        and int(estimate.get("risk_adjusted_projected") or 0) >= 150
        and calibrated_pct >= 60
        and int(estimate.get("signal_agreement_pct") or 0) >= 70
        and consensus >= 80
        and int(estimate.get("severe_loss_rate_pct") or 0) < 35
        and not regime_shift
    )
    result.update({
        "raw_positive_rate_pct": round(raw_pct),
        "positive_rate": round(calibrated_pct),
        "calibrated_positive_rate_pct": round(calibrated_pct),
        "probability_calibration": {
            "status": (
                "補正済み" if calibration_applied
                else "改善なし・生確率" if matched_bin and matched_bin.get("count", 0) >= 5
                else "標本不足・生確率"
            ),
            "applied": calibration_applied,
            "sample": int(matched_bin.get("count") or 0) if matched_bin else 0,
            "raw_pct": round(raw_pct),
            "calibrated_pct": round(calibrated_pct),
            "actual_pct": matched_bin.get("actual_pct") if matched_bin else None,
            "brier_score": brier,
            "raw_brier_score": raw_brier,
        },
        "forecast_interval_coins": {
            "level_pct": 80,
            "low": projected + int(residual_low) if residual_low is not None else None,
            "high": projected + int(residual_high) if residual_high is not None else None,
        },
        "recommendation_ready": recommendation_ready,
        "strong_signal_ready": strong_ready,
    })
    return result


def compare_prediction_models(
    source: Sequence[DailyPoint] | Iterable[Mapping],
    target_date: date,
    *,
    max_test_days: int = 60,
    event_dates: set[date] | None = None,
) -> dict:
    """対象日より前だけを使い、店舗に合う予測方式を選ぶ。"""
    source_list = list(source)
    if source_list and isinstance(source_list[0], tuple):
        points = sorted(source_list, key=lambda item: item[0])
    else:
        points = build_daily_points(source_list)
    points = [(day, value) for day, value in points if day < target_date]

    candidates = []
    for model, policy in MODEL_POLICIES.items():
        validation = walk_forward_backtest(
            points,
            max_test_days=max_test_days,
            model=model,
            event_dates=event_dates,
        )
        recommended = validation["recommended_days"]
        precision = validation["recommendation_success_pct"] or 0
        lower = validation["recommendation_lower_bound_pct"] or 0
        recent = validation["recent_recommendation_success_pct"] or 0
        avg_actual = validation["recommended_avg_actual_coins"] or 0
        direction = validation["direction_accuracy_pct"] or 0
        brier = validation.get("brier_score")
        calibration_score = max(0, min(100, (0.35 - float(brier)) / 0.20 * 100)) if brier is not None else 0
        sample_factor = min(1.0, recommended / 12)
        if recommended:
            score = (
                lower * 0.34
                + precision * 0.22
                + recent * 0.14
                + direction * 0.10
                + max(0, min(100, 50 + avg_actual / 10)) * 0.10
                + calibration_score * 0.10
            ) * sample_factor
        else:
            score = direction * 0.20
        candidates.append({
            "model": model,
            "label": policy["label"],
            "score": round(score),
            "recommended_days": recommended,
            "success_pct": validation["recommendation_success_pct"],
            "lower_bound_pct": validation["recommendation_lower_bound_pct"],
            "recent_success_pct": validation["recent_recommendation_success_pct"],
            "avg_actual_coins": validation["recommended_avg_actual_coins"],
            "direction_accuracy_pct": direction,
            "brier_score": brier,
            "calibration_score": round(calibration_score),
        })
    candidates.sort(
        key=lambda item: (item["score"], item["recommended_days"], item["direction_accuracy_pct"]),
        reverse=True,
    )
    winner = candidates[0] if candidates else {
        "model": "balanced", "label": MODEL_POLICIES["balanced"]["label"], "score": 0
    }
    # 1位だけへ全賭けせず、対象日より前の成績上位を滑らかに合成する。
    # 検証量が足りない間は、最も中立なバランス型だけを使う。
    if not candidates or max(item["recommended_days"] for item in candidates) < 3:
        ensemble_weights = {"balanced": 1.0}
    else:
        top = candidates[:3]
        floor_score = min(item["score"] for item in top)
        raw_weights = {
            item["model"]: max(5.0, item["score"] - floor_score + 5.0)
            for item in top
        }
        # 安定性の基準としてバランス型へ小さな事前重みを残す。
        raw_weights["balanced"] = raw_weights.get("balanced", 0.0) + 5.0
        weight_total = sum(raw_weights.values()) or 1.0
        ensemble_weights = {
            model: round(weight / weight_total, 4)
            for model, weight in raw_weights.items()
        }
    return {
        "selected_model": winner["model"],
        "selected_label": winner["label"],
        "selection_score": winner["score"],
        "prediction_method": "weighted_ensemble",
        "ensemble_weights": ensemble_weights,
        "models": candidates,
        "selection_notice": "対象日より前のウォークフォワード成績だけで重み付けし、複数方式を合成",
    }


def decide_action(
    projected: int,
    stale_days: int,
    validation: Mapping,
    positive_rate: int | None = None,
    prediction_diagnostics: Mapping | None = None,
) -> tuple[str, str]:
    """実測検証を満たさない候補を、保守的に見送りへ落とす。"""
    if stale_days > 30:
        return "見送り", f"最終データから{stale_days}日経過"
    if projected < 50:
        return "見送り", "指定日の推定差枚が最低基準の+50枚未満"
    if positive_rate is not None and positive_rate < 58:
        return "見送り", "指定日の推定プラス率が安全基準の58%未満"
    if prediction_diagnostics:
        agreement = int(prediction_diagnostics.get("signal_agreement_pct") or 0)
        consensus_value = prediction_diagnostics.get("model_consensus_pct")
        model_consensus = 100 if consensus_value is None else int(consensus_value)
        risk_adjusted = int(
            prediction_diagnostics.get("risk_adjusted_projected", projected) or 0
        )
        severe_loss_rate = int(prediction_diagnostics.get("severe_loss_rate_pct") or 0)
        if prediction_diagnostics.get("regime_shift_detected"):
            stability = int(prediction_diagnostics.get("regime_stability_pct") or 0)
            return "要確認", f"直近傾向が過去相場から変化中（安定度{stability}%）"
        if agreement < 65:
            return "見送り", f"長期・直近・曜日の根拠一致が{agreement}%で安全基準未満"
        if model_consensus < 75:
            return "見送り", f"予測モデル同士の一致度が{model_consensus}%で安全基準未満"
        if risk_adjusted < 80:
            return "見送り", f"ブレを差し引いた安全側推定が{risk_adjusted:+,}枚"
        if severe_loss_rate >= 35:
            return "要確認", f"過去の大幅マイナス日が{severe_loss_rate}%あり下振れ注意"
    if validation.get("status") != "validated":
        return "要確認", "過去検証が30日・推奨10回に未達"
    if validation.get("brier_score") is not None and float(validation["brier_score"]) >= 0.30:
        return "要確認", f"確率予測の誤差が大きい（Brier {validation['brier_score']}）"

    precision = validation.get("recommendation_success_pct") or 0
    lower = validation.get("recommendation_lower_bound_pct") or 0
    recommended_days = validation.get("recommended_days") or 0
    direction = validation.get("direction_accuracy_pct") or 0
    strong_days = validation.get("strong_signal_days") or 0
    strong_success = validation.get("strong_signal_success_pct") or 0
    if strong_days >= 3 and strong_success < 45:
        return "見送り", f"強い予測が過去{strong_days}回中{strong_success}%しか成功していない"
    if precision < 50 or direction < 45:
        return "見送り", f"過去検証の狙い時成功率{precision}%・方向的中{direction}%"
    recent = validation.get("recent_recommendation_success_pct") or 0
    quality = validation.get("quality_score") or 0
    if precision >= 90 and lower >= 80 and recommended_days >= 40 and recent >= 85 and quality >= 90:
        return "狙う・90%級", f"過去{recommended_days}回の狙い時成功率{precision}%"
    if precision >= 82 and lower >= 68 and recommended_days >= 25 and recent >= 75 and quality >= 80:
        return "狙う・80%級", f"過去{recommended_days}回の狙い時成功率{precision}%"
    if precision >= 70 and lower >= 55 and recommended_days >= 15 and recent >= 65 and quality >= 70:
        return "狙う", f"過去{recommended_days}回の狙い時成功率{precision}%"
    return "要確認", f"過去検証{precision}%・95%下限{lower}%・品質{quality}点で安全基準に未達"
