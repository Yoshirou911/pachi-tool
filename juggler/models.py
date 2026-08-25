"""北電子の公式ボーナス確率に基づくジャグラー営業中判定。"""
from __future__ import annotations

import math
from typing import Mapping


def _settings(bb: list[float], rb: list[float], payout: list[float]) -> dict[str, dict]:
    return {
        str(index + 1): {"bb": bb[index], "rb": rb[index], "payout": payout[index]}
        for index in range(6)
    }


JUGGLER_PROFILES: dict[str, dict] = {
    "my5": {
        "name": "マイジャグラーV",
        "aliases": ["マイジャグラーV", "マイジャグラーⅤ", "SマイジャグラーV"],
        "source_url": "https://www.kitadenshi.co.jp/slot/myjuggler5/",
        "settings": _settings(
            [273.1, 270.8, 266.4, 254.0, 240.1, 229.1],
            [409.6, 385.5, 336.1, 290.0, 268.6, 229.1],
            [97.0, 98.0, 99.9, 102.8, 105.3, 109.4],
        ),
    },
    "neo_im": {
        "name": "ネオアイムジャグラーEX",
        "aliases": ["ネオアイムジャグラーEX", "アイムジャグラーEX", "SアイムジャグラーEX"],
        "source_url": "https://www.kitadenshi.co.jp/slot/neoimjugglerex/",
        "settings": _settings(
            [273.1, 269.7, 269.7, 259.0, 259.0, 255.0],
            [439.8, 399.6, 331.0, 315.1, 255.0, 255.0],
            [97.0, 98.0, 99.5, 101.1, 103.3, 105.5],
        ),
    },
    "funky2": {
        "name": "ファンキージャグラー2",
        "aliases": ["ファンキージャグラー2", "Sファンキージャグラー2"],
        "source_url": "https://www.kitadenshi.co.jp/slot/funkyjuggler2/",
        "settings": _settings(
            [266.4, 259.0, 256.0, 249.2, 240.1, 219.9],
            [439.8, 407.1, 366.1, 322.8, 299.3, 262.1],
            [97.0, 98.5, 99.8, 102.0, 104.3, 109.0],
        ),
    },
    "gogo3": {
        "name": "ゴーゴージャグラー3",
        "aliases": ["ゴーゴージャグラー3", "Sゴーゴージャグラー3"],
        "source_url": "https://www.kitadenshi.co.jp/slot/gogojuggler3/",
        "settings": _settings(
            [259.0, 258.0, 257.0, 254.0, 247.3, 234.9],
            [354.2, 332.7, 306.2, 268.6, 247.3, 234.9],
            [97.2, 98.2, 99.4, 101.6, 103.8, 106.5],
        ),
    },
    "mister": {
        "name": "ミスタージャグラー",
        "aliases": ["ミスタージャグラー", "Sミスタージャグラー"],
        "source_url": "https://www.kitadenshi.co.jp/slot/mrjuggler/",
        "settings": _settings(
            [268.6, 267.5, 260.1, 249.2, 240.9, 237.4],
            [374.5, 354.2, 331.0, 291.3, 257.0, 237.4],
            [97.0, 98.0, 99.8, 102.7, 105.5, 107.3],
        ),
    },
    "happy3": {
        "name": "ハッピージャグラーV III",
        "aliases": [
            "ハッピージャグラーV III", "ハッピージャグラーVⅢ",
            "ハッピージャグラーV Ⅲ", "SハッピージャグラーV III",
        ],
        "source_url": "https://www.kitadenshi.co.jp/slot/happyjugglerv3/",
        "settings": _settings(
            [273.1, 270.8, 263.2, 254.0, 239.2, 226.0],
            [397.2, 362.1, 332.7, 300.6, 273.1, 256.0],
            [97.0, 98.1, 99.9, 102.9, 105.8, 108.4],
        ),
    },
    "girls_ss": {
        "name": "ジャグラーガールズSS",
        "aliases": ["ジャグラーガールズSS", "SジャグラーガールズSS"],
        "source_url": "https://www.kitadenshi.co.jp/slot/jugglergirlsss/",
        "settings": _settings(
            [273.1, 270.8, 260.1, 250.1, 243.6, 226.0],
            [381.0, 350.5, 316.6, 281.3, 270.8, 252.1],
            [97.0, 97.9, 99.9, 102.1, 104.0, 107.5],
        ),
    },
    "ultra_miracle": {
        "name": "ウルトラミラクルジャグラー",
        "aliases": ["ウルトラミラクルジャグラー", "Sウルトラミラクルジャグラー"],
        "source_url": "https://www.kitadenshi.co.jp/slot/ultramiraclejuggler/",
        "settings": _settings(
            [267.5, 261.1, 256.0, 242.7, 233.2, 216.3],
            [425.6, 402.1, 350.5, 322.8, 297.9, 277.7],
            [97.0, 98.1, 99.8, 102.1, 104.5, 108.1],
        ),
    },
}


def catalog() -> list[dict]:
    return [
        {
            "id": profile_id,
            "name": profile["name"],
            "source_url": profile["source_url"],
            "settings": profile["settings"],
        }
        for profile_id, profile in JUGGLER_PROFILES.items()
    ]


def assess_juggler(profile_id: str, games: int, bb_count: int, rb_count: int) -> dict:
    """BB/RBを多項分布として扱い、設定別の相対尤度を返す。"""
    if profile_id not in JUGGLER_PROFILES:
        raise KeyError(profile_id)
    if games <= 0 or bb_count < 0 or rb_count < 0 or bb_count + rb_count > games:
        raise ValueError("ゲーム数とBIG・REG回数の組み合わせが不正です")

    profile = JUGGLER_PROFILES[profile_id]
    log_likelihoods: dict[str, float] = {}
    other_count = games - bb_count - rb_count
    for setting, spec in profile["settings"].items():
        p_bb = 1.0 / spec["bb"]
        p_rb = 1.0 / spec["rb"]
        p_other = max(1e-12, 1.0 - p_bb - p_rb)
        log_likelihoods[setting] = (
            bb_count * math.log(p_bb)
            + rb_count * math.log(p_rb)
            + other_count * math.log(p_other)
        )

    peak = max(log_likelihoods.values())
    weights = {setting: math.exp(value - peak) for setting, value in log_likelihoods.items()}
    total = sum(weights.values()) or 1.0
    probabilities = {setting: weights[setting] / total for setting in weights}
    best_setting = max(probabilities, key=probabilities.get)
    high_probability = sum(probabilities[str(setting)] for setting in (4, 5, 6))
    low_probability = sum(probabilities[str(setting)] for setting in (1, 2, 3))
    high_low_ratio = high_probability / max(1e-12, low_probability)
    best_probability = probabilities[best_setting]

    sample_factor = min(1.0, games / 6000)
    reference_reliability = round(best_probability * sample_factor * 100)
    prediction_grade = (
        "90%級" if games >= 6000 and high_probability >= 0.90 and high_low_ratio >= 6
        else "80%級" if games >= 5000 and high_probability >= 0.82 and high_low_ratio >= 3
        else "低設定90%級" if games >= 6000 and high_probability <= 0.10 and high_low_ratio <= 1 / 6
        else "低設定80%級" if games >= 5000 and high_probability <= 0.18 and high_low_ratio <= 1 / 3
        else "判定材料不足"
    )
    if games < 2000:
        confidence = "データ不足"
        action = "判定保留"
        reason = "2000G未満はブレが大きいため、設定判断に使いません"
    elif games < 4000:
        confidence = "低"
        action = "様子見"
        reason = "4000G未満のため、REGと合算が良くても続行確定にはしません"
    elif prediction_grade in {"90%級", "80%級"}:
        confidence = "高" if prediction_grade == "90%級" else "中"
        action = f"続行候補・{prediction_grade}"
        reason = (
            f"設定4以上の相対確率{round(high_probability * 100)}%・"
            f"高設定側の尤度比{high_low_ratio:.1f}倍"
        )
    elif prediction_grade in {"低設定90%級", "低設定80%級"}:
        confidence = "高" if prediction_grade == "低設定90%級" else "中"
        action = f"見送り候補・{prediction_grade.replace('低設定', '')}"
        reason = (
            f"設定4以上の相対確率{round(high_probability * 100)}%・"
            f"高設定側の尤度比{high_low_ratio:.2f}倍"
        )
    elif high_probability >= 0.75 and games >= 5000:
        confidence = "中"
        action = "続行候補"
        reason = f"高設定寄りですが90%級・80%級の厳格条件には未達です"
    elif high_probability <= 0.25 and games >= 5000:
        confidence = "中"
        action = "見送り候補"
        reason = f"低設定寄りですが90%級・80%級の厳格条件には未達です"
    else:
        confidence = "高" if games >= 6000 else "中"
        action = "様子見"
        reason = "高設定・低設定のどちらにも十分寄っていません"

    combined_count = bb_count + rb_count
    return {
        "profile_id": profile_id,
        "machine_name": profile["name"],
        "games": games,
        "bb_count": bb_count,
        "rb_count": rb_count,
        "bb_denominator": round(games / bb_count, 1) if bb_count else None,
        "rb_denominator": round(games / rb_count, 1) if rb_count else None,
        "combined_denominator": round(games / combined_count, 1) if combined_count else None,
        "setting_probabilities_pct": {
            setting: round(probability * 100, 1)
            for setting, probability in probabilities.items()
        },
        "best_setting": int(best_setting),
        "best_setting_probability_pct": round(best_probability * 100),
        "high_setting_probability_pct": round(high_probability * 100),
        "high_low_likelihood_ratio": round(high_low_ratio, 2),
        "prediction_grade": prediction_grade,
        "grade_scope": "入力したBIG・REGの統計モデル内",
        "reference_reliability_pct": reference_reliability,
        "confidence": confidence,
        "action": action,
        "reason": reason,
        "source_url": profile["source_url"],
        "notice": "BIG・REGのみを設定1〜6同率の前提で比較した相対尤度です。90%級は統計モデル内の判定で、実際の設定や勝利確率を保証しません。店舗傾向と当日の示唆も併用してください。",
    }


def profile_for_machine(machine_name: str) -> str | None:
    compact = (machine_name or "").replace(" ", "").replace("　", "")
    for profile_id, profile in JUGGLER_PROFILES.items():
        if any(alias.replace(" ", "").replace("　", "") in compact for alias in profile["aliases"]):
            return profile_id
    return None
