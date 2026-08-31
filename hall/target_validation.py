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
        "meaning": "勝率保証ではなく、先読みなし過去検証を通過した予測グレード",
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
    )
    strong_ready = (
        len(points) >= 30
        and risk_adjusted >= 150
        and positive_rate >= 60
        and signal_agreement >= 70
        and severe_loss_rate < 35
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
            trial_model = active_auto_model
            selected_model_counts[trial_model] = selected_model_counts.get(trial_model, 0) + 1
        prediction = date_weighted_estimate(
            training, test_date, model=trial_model, event_dates=event_dates
        )
        predicted = float(prediction["projected"])
        predicted_positive = bool(prediction["recommendation_ready"])
        actual_positive = actual > 0
        strong_signal = bool(prediction["strong_signal_ready"])
        direction_prediction = predicted >= 0
        predicted_probability = max(0.05, min(0.95, prediction["positive_rate"] / 100))
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
                "predicted_probability": round(predicted_probability, 3),
                "signal_agreement_pct": prediction["signal_agreement_pct"],
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
        sample_factor = min(1.0, recommended / 12)
        if recommended:
            score = (
                lower * 0.38
                + precision * 0.24
                + recent * 0.16
                + direction * 0.10
                + max(0, min(100, 50 + avg_actual / 10)) * 0.12
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
        })
    candidates.sort(
        key=lambda item: (item["score"], item["recommended_days"], item["direction_accuracy_pct"]),
        reverse=True,
    )
    winner = candidates[0] if candidates else {
        "model": "balanced", "label": MODEL_POLICIES["balanced"]["label"], "score": 0
    }
    return {
        "selected_model": winner["model"],
        "selected_label": winner["label"],
        "selection_score": winner["score"],
        "models": candidates,
        "selection_notice": "対象日より前のウォークフォワード成績だけで選択",
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
        risk_adjusted = int(
            prediction_diagnostics.get("risk_adjusted_projected", projected) or 0
        )
        severe_loss_rate = int(prediction_diagnostics.get("severe_loss_rate_pct") or 0)
        if agreement < 65:
            return "見送り", f"長期・直近・曜日の根拠一致が{agreement}%で安全基準未満"
        if risk_adjusted < 80:
            return "見送り", f"ブレを差し引いた安全側推定が{risk_adjusted:+,}枚"
        if severe_loss_rate >= 35:
            return "要確認", f"過去の大幅マイナス日が{severe_loss_rate}%あり下振れ注意"
    if validation.get("status") != "validated":
        return "要確認", "過去検証が30日・推奨10回に未達"

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
