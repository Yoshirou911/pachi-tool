"""狙い台予測の時系列検証と、実戦可否の保守的な判定。"""
from __future__ import annotations

import math
from datetime import date
from typing import Iterable, Mapping, Sequence


DailyPoint = tuple[date, float]


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

    base_avg = sum(values) / len(values) if values else 0.0
    components = [(base_avg, 0.35)]
    if recent_values:
        components.append((sum(recent_values) / len(recent_values), 0.20))
    if weekday_values:
        components.append(
            (
                sum(weekday_values) / len(weekday_values),
                min(0.30, 0.10 * len(weekday_values)),
            )
        )
    if digit_values:
        components.append(
            (
                sum(digit_values) / len(digit_values),
                min(0.15, 0.075 * len(digit_values)),
            )
        )
    weight_total = sum(weight for _, weight in components) or 1.0
    projected = sum(value * weight for value, weight in components) / weight_total

    base_positive = sum(value > 0 for value in values) / len(values) * 100 if values else 0.0
    if weekday_values:
        weekday_positive = sum(value > 0 for value in weekday_values) / len(weekday_values) * 100
        positive_rate = base_positive * 0.6 + weekday_positive * 0.4
    else:
        positive_rate = base_positive
    return {
        "projected": round(projected),
        "base_avg": round(base_avg),
        "positive_rate": round(positive_rate),
        "sample_days": len(points),
        "weekday_days": len(weekday_values),
        "digit_days": len(digit_values),
        "recent_days": len(recent_values),
        "weekday_avg": round(sum(weekday_values) / len(weekday_values)) if weekday_values else None,
        "digit_avg": round(sum(digit_values) / len(digit_values)) if digit_values else None,
        "latest_date": points[-1][0].isoformat() if points else "",
    }


def _wilson_lower(successes: int, trials: int, z: float = 1.645) -> float:
    """成功率の片側90% Wilson下限。少数サンプルを過信しない。"""
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
        predicted_positive = predicted >= 50 and prediction["positive_rate"] >= 55
        actual_positive = actual > 0
        strong_signal = predicted >= 100 and prediction["positive_rate"] >= 60
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
    enough = test_days >= 20 and len(recommended) >= 5
    precision = recommended_successes / len(recommended) if recommended else 0.0
    direction = direction_successes / test_days if test_days else 0.0
    lower_bound = _wilson_lower(recommended_successes, len(recommended))
    strong_precision = strong_successes / len(strong) if strong else 0.0

    return {
        "status": "validated" if enough else "insufficient",
        "method": "walk_forward",
        "test_days": test_days,
        "train_min_days": min_train_days,
        "direction_accuracy_pct": round(direction * 100),
        "recommended_days": len(recommended),
        "recommendation_success_pct": round(precision * 100) if recommended else None,
        "recommendation_lower_bound_pct": round(lower_bound * 100) if recommended else None,
        "strong_signal_days": len(strong),
        "strong_signal_success_pct": round(strong_precision * 100) if strong else None,
        "mae_coins": round(mae) if mae is not None else None,
        "trust_level": (
            "90%級" if enough and precision >= 0.90 and lower_bound >= 0.70
            else "80%級" if enough and precision >= 0.80 and lower_bound >= 0.60
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
        return "要確認", "過去検証が20日・推奨5回に未達"

    precision = validation.get("recommendation_success_pct") or 0
    lower = validation.get("recommendation_lower_bound_pct") or 0
    recommended_days = validation.get("recommended_days") or 0
    direction = validation.get("direction_accuracy_pct") or 0
    if precision < 50 or direction < 45:
        return "見送り", f"過去検証の狙い時成功率{precision}%・方向的中{direction}%"
    if precision >= 90 and lower >= 70 and recommended_days >= 20:
        return "狙う・90%級", f"過去{recommended_days}回の狙い時成功率{precision}%"
    if precision >= 80 and lower >= 60 and recommended_days >= 15:
        return "狙う・80%級", f"過去{recommended_days}回の狙い時成功率{precision}%"
    if precision >= 65 and lower >= 50 and recommended_days >= 10:
        return "狙う", f"過去{recommended_days}回の狙い時成功率{precision}%"
    return "要確認", f"過去検証は{precision}%だが安全基準に未達"
