"""
店傾向分析 + 事前分布生成モジュール。

履歴が少ないうちはベガスベガス大東店の分析データ（ハードコード）を先行利用し、
実戦セッションが溜まるにつれて履歴ベースに移行していく。

Dirichlet スムージングで過学習を防ぐ。
"""
from __future__ import annotations

import datetime
from collections import defaultdict
from typing import Optional

# ---------------------------------------------------------------------------
# ベガスベガス大東店 固有分析データ（min-repo.com 12/15〜6/25 期間集計）
# ---------------------------------------------------------------------------

DAITO_MACHINE_SCORES: dict[str, tuple[int, int]] = {
    # 機種名: (累計スコア, 出現回数)
    "ディスクアップULTRA": (45, 23),
    "ディスクアップ2":     (35, 17),
    "戦国乙女4":           (34, 14),
    "不二子BT":            (32, 14),
    "絆2天膳":             (28, 14),
    "攻殻機動隊":          (28, 12),
    "このすば":            (26, 12),
    "SBJ":                 (25, 11),
    "まどかマギカ":        (24,  9),
    "虚構推理L":           (15,  5),
}

# 曜日別平均スコア（0=月〜6=日）
DAITO_WEEKDAY_AVG: dict[int, float] = {
    0: 1.93,  # 月
    1: 1.67,  # 火
    2: 1.90,  # 水
    3: 1.58,  # 木
    4: 1.71,  # 金
    5: 1.87,  # 土
    6: 2.02,  # 日
}

# 特定日補正（日付の下1桁）
DAITO_DIGIT_ADJ: dict[str, float] = {
    "5": +0.03,   # 5のつく日：通常とほぼ同じ
    "8": -0.22,   # 8のつく日：通常より低い（期待外れ）
}

SMOOTHING = 3.0  # Dirichlet 疑似カウント（小 → 履歴依存、大 → 一様寄り）

_WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]


def compute_prior(
    hall_name: str,
    machine_name: str = "",
    weekday: Optional[int] = None,
    is_event_day: bool = False,
    day_of_month: Optional[int] = None,
    settings: Optional[list[str]] = None,
) -> dict[str, float]:
    """
    指定条件に合った事前分布を生成して返す。

    Args:
        hall_name:      ホール名（"ベガスベガス大東店" を含む場合は専用データを使用）
        machine_name:   機種名（機種スコアでシフト）
        weekday:        0=月 ... 6=日
        is_event_day:   イベント日フラグ
        day_of_month:   その日の「日」（1〜31）。特定日調整に使用。
        settings:       設定ラベルリスト（Noneの場合は["1"〜"6"]）

    Returns:
        {"1": 0.15, "2": 0.20, ...} のような正規化済み事前分布。
    """
    from records.models import list_sessions  # 循環import を避けるため遅延import

    if settings is None:
        settings = ["1", "2", "3", "4", "5", "6"]
    n = len(settings)

    # Step1: 履歴から実績集計
    sessions = list_sessions(hall_name=hall_name, machine_name=machine_name or None)
    accumulated: dict[str, float] = defaultdict(float)
    total_weight = 0.0

    for s in sessions:
        if s.posterior is None:
            continue
        try:
            d = datetime.date.fromisoformat(s.date)
            weight = 1.0
            if weekday is not None and d.weekday() == weekday:
                weight = 2.0  # 同曜日を重視
            if day_of_month is not None:
                digit = str(day_of_month % 10)
                if str(d.day % 10) == digit:
                    weight *= 1.5
        except ValueError:
            weight = 1.0

        for setting, prob in s.posterior.items():
            if setting in settings:
                accumulated[setting] += prob * weight
        total_weight += weight

    # Step2: 一様事前 + Dirichlet スムージング
    weights: dict[str, float] = {}
    total_acc = sum(accumulated.values())
    for s in settings:
        empirical = (accumulated.get(s, 0) / total_acc) if total_acc > 0 else (1.0 / n)
        weights[s] = empirical * total_weight + SMOOTHING / n

    # Step3: ベガスベガス大東店 固有調整
    if _is_daito(hall_name):
        weights = _apply_daito_adjustments(
            weights, settings, machine_name, weekday, is_event_day, day_of_month
        )

    # Step4: 正規化
    total = sum(weights.values())
    return {s: weights[s] / total for s in settings}


def _is_daito(hall_name: str) -> bool:
    return "大東" in hall_name or "ベガスベガス大東" in hall_name


def _apply_daito_adjustments(
    w: dict[str, float],
    settings: list[str],
    machine_name: str,
    weekday: Optional[int],
    is_event_day: bool,
    day_of_month: Optional[int],
) -> dict[str, float]:
    w = dict(w)
    high = [s for s in settings if int(s) >= 4]
    low  = [s for s in settings if int(s) < 4]

    # 機種スコアによる調整
    if machine_name and machine_name in DAITO_MACHINE_SCORES:
        score, appearances = DAITO_MACHINE_SCORES[machine_name]
        # スコアの正規化: 最大45pt → 最大 0.25 のブースト
        boost = min(score / 180.0, 0.25)
        for s in high:
            w[s] *= (1.0 + boost)
        for s in low:
            w[s] *= max(0.5, 1.0 - boost * 0.4)

    # 曜日スコアによる調整（基準=1.80、上振れで高設定シフト）
    if weekday is not None:
        day_avg = DAITO_WEEKDAY_AVG.get(weekday, 1.80)
        day_delta = (day_avg - 1.80) * 0.4  # 最大±0.09 程度
        if day_delta > 0:
            for s in high: w[s] *= (1.0 + day_delta)
        elif day_delta < 0:
            for s in high: w[s] *= max(0.5, 1.0 + day_delta)

    # 特定日調整
    if day_of_month is not None:
        digit = str(day_of_month % 10)
        adj = DAITO_DIGIT_ADJ.get(digit, 0.0)
        if adj != 0:
            for s in high: w[s] *= (1.0 + adj)

    # イベント日は高設定率 +10% シフト
    if is_event_day:
        for s in high: w[s] *= 1.10

    return w


# ---------------------------------------------------------------------------
# 分析ユーティリティ
# ---------------------------------------------------------------------------

def day_rating(hall_name: str, weekday: int) -> dict:
    """指定曜日の「高設定が出やすさ」を返す（0〜1スケール + テキスト）。"""
    if _is_daito(hall_name):
        avg = DAITO_WEEKDAY_AVG.get(weekday, 1.80)
        all_scores = list(DAITO_WEEKDAY_AVG.values())
        mn, mx = min(all_scores), max(all_scores)
        score_0_1 = (avg - mn) / (mx - mn) if mx > mn else 0.5
        return {
            "weekday": _WEEKDAY_NAMES[weekday],
            "avg_score": avg,
            "normalized": round(score_0_1, 3),
            "rank": sorted(DAITO_WEEKDAY_AVG, key=lambda d: -DAITO_WEEKDAY_AVG[d]).index(weekday) + 1,
            "source": "daito_analysis",
        }
    # 履歴ベース
    from records.models import list_sessions
    sessions = list_sessions(hall_name=hall_name)
    probs: list[float] = []
    for s in sessions:
        if s.posterior is None:
            continue
        try:
            d = datetime.date.fromisoformat(s.date)
            if d.weekday() == weekday:
                hp = sum(p for k, p in s.posterior.items() if int(k) >= 4)
                probs.append(hp)
        except ValueError:
            pass
    avg_hp = sum(probs) / len(probs) if probs else 0.5
    return {
        "weekday": _WEEKDAY_NAMES[weekday],
        "avg_high_prob": round(avg_hp, 3),
        "sample_count": len(probs),
        "source": "history",
    }


def machine_ranking(hall_name: str) -> list[dict]:
    """機種別スコアランキングを返す。"""
    if _is_daito(hall_name):
        return [
            {
                "machine": name,
                "score": score,
                "appearances": apps,
                "avg_score": round(score / apps, 2) if apps else 0,
            }
            for name, (score, apps) in sorted(
                DAITO_MACHINE_SCORES.items(), key=lambda x: -x[1][0]
            )
        ]
    # 履歴ベース（高設定確率の平均）
    from records.models import list_sessions
    sessions = list_sessions(hall_name=hall_name)
    machine_data: dict[str, list[float]] = defaultdict(list)
    for s in sessions:
        if s.posterior:
            hp = sum(p for k, p in s.posterior.items() if int(k) >= 4)
            machine_data[s.machine_name].append(hp)
    return [
        {
            "machine": m,
            "avg_high_prob": round(sum(v) / len(v), 3),
            "appearances": len(v),
        }
        for m, v in sorted(machine_data.items(), key=lambda x: -sum(x[1]) / len(x[1]))
    ]
