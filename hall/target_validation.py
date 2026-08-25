"""狙い台予測の時系列検証と、実戦可否の保守的な判定。"""
from __future__ import annotations

import math
from datetime import date
from statistics import median
from typing import Iterable, Mapping, Sequence


DailyPoint = tuple[date, float]


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


def _smoothed_positive_rate(values: Sequence[float]) -> float:
    """少数データが0%/100%へ張り付かないようBeta(2,2)で縮小する。"""
    wins = sum(float(value) > 0 for value in values)
    return (wins + 2) / (len(values) + 4) * 100


def build_daily_points(rows: Iterable[Mapping]) -> list[DailyPoint]:
    """機種行を設置台数で加重し、店舗/機種の日別平均にまとめる。"""
    daily: dict[str, list[tuple[float, int]]] = {}
    for row in rows:
        unit_count = max(1, int(row["unit_count"] or 1))
        daily.setdefault(str(row["report_date"]), []).append(
            (float(row["avg_diff_coins"]), unit_count)
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
    source: Sequence[DailyPoint] | Iterable[Mapping], target_date: date
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
    recent_values = [value for _, value in points[-min(7, len(points)):]]

    base_avg = _robust_mean(values)
    components = [("全期間", base_avg, 0.30)]
    if recent_values:
        recent_weights = list(range(1, len(recent_values) + 1))
        recent_avg = sum(
            value * weight for value, weight in zip(recent_values, recent_weights)
        ) / sum(recent_weights)
        components.append(("直近", recent_avg, 0.25))
    if weekday_values:
        components.append(
            (
                "曜日",
                _robust_mean(weekday_values),
                min(0.30, 0.10 * len(weekday_values)),
            )
        )
    if digit_values:
        components.append(
            (
                "日付末尾",
                _robust_mean(digit_values),
                min(0.15, 0.075 * len(digit_values)),
            )
        )
    weight_total = sum(weight for _, _, weight in components) or 1.0
    projected = sum(value * weight for _, value, weight in components) / weight_total
    projected_positive = projected >= 0
    agreement_weight = sum(
        weight for _, value, weight in components if (value >= 0) == projected_positive
    )
    signal_agreement = agreement_weight / weight_total * 100

    base_positive = _smoothed_positive_rate(values)
    if weekday_values:
        weekday_positive = _smoothed_positive_rate(weekday_values)
        recent_positive = _smoothed_positive_rate(recent_values)
        positive_rate = base_positive * 0.45 + weekday_positive * 0.35 + recent_positive * 0.20
    else:
        recent_positive = _smoothed_positive_rate(recent_values) if recent_values else base_positive
        positive_rate = base_positive * 0.70 + recent_positive * 0.30
    return {
        "projected": round(projected),
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
        "volatility_coins": round(_median_absolute_deviation(values)),
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
) -> dict:
    """各日を当時までのデータだけで予測し、先読みなしで成績を測る。"""
    source_list = list(source)
    if source_list and isinstance(source_list[0], tuple):
        points = sorted(source_list, key=lambda item: item[0])
    else:
        points = build_daily_points(source_list)

    trials = []
    start_index = max(min_train_days, len(points) - max_test_days)
    for index in range(start_index, len(points)):
        test_date, actual = points[index]
        training = points[:index]
        prediction = date_weighted_estimate(training, test_date)
        predicted = float(prediction["projected"])
        predicted_positive = (
            predicted >= 50
            and prediction["positive_rate"] >= 55
            and prediction["signal_agreement_pct"] >= 60
        )
        actual_positive = actual > 0
        strong_signal = (
            predicted >= 100
            and prediction["positive_rate"] >= 60
            and prediction["signal_agreement_pct"] >= 70
        )
        predicted_probability = max(0.05, min(0.95, prediction["positive_rate"] / 100))
        trials.append(
            {
                "date": test_date.isoformat(),
                "predicted": round(predicted),
                "actual": round(actual),
                "direction_correct": predicted_positive == actual_positive,
                "recommended": predicted_positive,
                "recommended_success": predicted_positive and actual_positive,
                "strong_signal": strong_signal,
                "strong_signal_success": strong_signal and actual_positive,
                "predicted_probability": round(predicted_probability, 3),
                "signal_agreement_pct": prediction["signal_agreement_pct"],
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

    return {
        "status": "validated" if enough else "insufficient",
        "method": "walk_forward",
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
        "mae_coins": round(mae) if mae is not None else None,
        "brier_score": round(brier_score, 3) if brier_score is not None else None,
        "quality_score": quality_score,
        "trust_level": (
            "90%級" if grade90
            else "80%級" if grade80
            else "検証済み" if enough
            else "データ不足"
        ),
        "recent_trials": trials[-10:],
    }


def decide_action(
    projected: int,
    stale_days: int,
    validation: Mapping,
    positive_rate: int | None = None,
) -> tuple[str, str]:
    """実測検証を満たさない候補を、保守的に見送りへ落とす。"""
    if stale_days > 30:
        return "見送り", f"最終データから{stale_days}日経過"
    if projected < 50:
        return "見送り", "指定日の推定差枚が安全基準の+50枚未満"
    if positive_rate is not None and positive_rate < 55:
        return "見送り", "指定日の推定プラス率が安全基準の55%未満"
    if validation.get("status") != "validated":
        return "要確認", "過去検証が30日・推奨10回に未達"

    precision = validation.get("recommendation_success_pct") or 0
    lower = validation.get("recommendation_lower_bound_pct") or 0
    recommended_days = validation.get("recommended_days") or 0
    direction = validation.get("direction_accuracy_pct") or 0
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
