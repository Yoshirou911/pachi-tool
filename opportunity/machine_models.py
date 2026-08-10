"""ハイエナ判定に使う、説明可能な機種別モデル。

期待値表の公開値と、そこから別途推定した消化時間を混ぜないための層。
duration は平均時間と閉店判定用の安全側時間を明示的に分ける。
machine_adjustment は公開根拠が確認できた条件だけを自動補正し、推測値は
`verified=False` として着席許可に使わない。
"""
from __future__ import annotations

from math import ceil


# 通常時の残り消化、前兆/CZ、AT/ボーナス、引き戻しを分けた平均モデル。
# expected_play_minutes は各項目の合計（端数切上げ）。safe_buffer_minutes は
# ロング継続・前兆の振れを閉店判定だけに加算する。
DURATION_MODELS: dict[str, dict] = {
    "hokuto-normal-equivalent-conservative-v1": dict(normal_games=310, normal_games_per_minute=13.5, cz_minutes=5, at_bonus_minutes=39, pullback_minutes=3, safe_buffer_minutes=28),
    "hokuto-normal-56-current-conservative-v1": dict(normal_games=255, normal_games_per_minute=13.5, cz_minutes=5, at_bonus_minutes=35, pullback_minutes=3, safe_buffer_minutes=28),
    "hokuto-reset-equivalent-conservative-v1": dict(normal_games=285, normal_games_per_minute=13.5, cz_minutes=5, at_bonus_minutes=42, pullback_minutes=4, safe_buffer_minutes=30),
    "monkey5-normal-equivalent-conservative-v1": dict(normal_games=285, normal_games_per_minute=13.2, cz_minutes=7, at_bonus_minutes=42, pullback_minutes=7, variable_speed_minutes=1, safe_buffer_minutes=35),
    "monkey5-normal-56-cash-conservative-v1": dict(normal_games=235, normal_games_per_minute=13.2, cz_minutes=7, at_bonus_minutes=39, pullback_minutes=6, variable_speed_minutes=1, safe_buffer_minutes=32),
    "tokyo-ghoul-cz-equivalent-safe-v1": dict(normal_games=160, normal_games_per_minute=13.2, cz_minutes=8, at_bonus_minutes=27, pullback_minutes=7, variable_speed_minutes=1, safe_buffer_minutes=30),
    "tokyo-ghoul-at-56-cash-safe-v1": dict(normal_games=340, normal_games_per_minute=13.2, cz_minutes=8, at_bonus_minutes=47, pullback_minutes=8, variable_speed_minutes=1, safe_buffer_minutes=40),
    "god-eater-resurrection-normal-equivalent-v1": dict(normal_games=278, normal_games_per_minute=13.3, cz_minutes=6, at_bonus_minutes=18, pullback_minutes=4, variable_speed_minutes=1, safe_buffer_minutes=28),
    "god-eater-resurrection-reset-equivalent-v1": dict(normal_games=207, normal_games_per_minute=13.3, cz_minutes=6, at_bonus_minutes=19, pullback_minutes=4, variable_speed_minutes=1, safe_buffer_minutes=26),
    "kaguya-sama-big-after-equivalent-v1": dict(normal_games=305, normal_games_per_minute=13.2, cz_minutes=6, at_bonus_minutes=37, pullback_minutes=7, variable_speed_minutes=1, safe_buffer_minutes=35),
    "kaguya-sama-reg-after-equivalent-v1": dict(normal_games=365, normal_games_per_minute=13.2, cz_minutes=6, at_bonus_minutes=40, pullback_minutes=8, variable_speed_minutes=1, safe_buffer_minutes=38),
    "monster-hunter-rise-normal-equivalent-v1": dict(normal_games=350, normal_games_per_minute=13.0, cz_minutes=10, at_bonus_minutes=53, pullback_minutes=9, variable_speed_minutes=1, safe_buffer_minutes=45),
    "karakuri-circus-cz-equivalent-v1": dict(normal_games=300, normal_games_per_minute=13.2, cz_minutes=9, at_bonus_minutes=27, pullback_minutes=5, variable_speed_minutes=2, safe_buffer_minutes=38),
    "karakuri-circus-cz-56-cash-v1": dict(normal_games=235, normal_games_per_minute=13.2, cz_minutes=9, at_bonus_minutes=26, pullback_minutes=5, variable_speed_minutes=2, safe_buffer_minutes=35),
    "tokyo-revengers-normal-equivalent-v1": dict(normal_games=330, normal_games_per_minute=13.0, cz_minutes=8, at_bonus_minutes=48, pullback_minutes=7, variable_speed_minutes=1, safe_buffer_minutes=40),
    "tokyo-revengers-reset-equivalent-v1": dict(normal_games=225, normal_games_per_minute=13.0, cz_minutes=8, at_bonus_minutes=46, pullback_minutes=7, variable_speed_minutes=1, safe_buffer_minutes=38),
    "bakemonogatari-normal-equivalent-v1": dict(normal_games=260, normal_games_per_minute=13.0, cz_minutes=7, at_bonus_minutes=41, pullback_minutes=6, variable_speed_minutes=1, safe_buffer_minutes=35),
    "bakemonogatari-reset-equivalent-v1": dict(normal_games=170, normal_games_per_minute=13.0, cz_minutes=7, at_bonus_minutes=38, pullback_minutes=6, variable_speed_minutes=1, safe_buffer_minutes=32),
    "million-god-kiseki-normal-equivalent-v1": dict(normal_games=520, normal_games_per_minute=13.0, cz_minutes=8, at_bonus_minutes=52, pullback_minutes=8, variable_speed_minutes=2, safe_buffer_minutes=50),
    "million-god-kiseki-normal-56-cash-v1": dict(normal_games=390, normal_games_per_minute=13.0, cz_minutes=8, at_bonus_minutes=52, pullback_minutes=8, variable_speed_minutes=2, safe_buffer_minutes=48),
    "valvrave1-bonus-gap-equivalent-v1": dict(normal_games=424, normal_games_per_minute=13.0, cz_minutes=9, at_bonus_minutes=42, pullback_minutes=6, variable_speed_minutes=2, safe_buffer_minutes=45),
    "valvrave1-pullback-66g-v1": dict(normal_games=33, normal_games_per_minute=13.0, cz_minutes=0, at_bonus_minutes=7, pullback_minutes=0, variable_speed_minutes=0, safe_buffer_minutes=12),
    "saint-seiya-kaiou-normal-equivalent-v1": dict(normal_games=282, normal_games_per_minute=13.0, cz_minutes=9, at_bonus_minutes=47, pullback_minutes=8, variable_speed_minutes=1, safe_buffer_minutes=45),
    "toloveru-normal-equivalent-v1": dict(normal_games=310, normal_games_per_minute=13.2, cz_minutes=7, at_bonus_minutes=38, pullback_minutes=8, variable_speed_minutes=1, safe_buffer_minutes=38),
    "toloveru-reset-equivalent-v1": dict(normal_games=255, normal_games_per_minute=13.2, cz_minutes=7, at_bonus_minutes=35, pullback_minutes=8, variable_speed_minutes=1, safe_buffer_minutes=35),
}


DURATION_BASIS = (
    "公開されている天井・実質初当たり・純増を基礎に、通常時、前兆/CZ、"
    "AT/ボーナス、引き戻し、純増変化区間を分離した保守モデル。実戦時間で継続校正する。"
)


def duration_model_for(profile: dict) -> dict | None:
    model = profile.get("duration_model")
    if isinstance(model, dict) and model:
        return model
    return DURATION_MODELS.get(str(profile.get("catalog_key") or ""))


def estimate_duration(profile: dict, current_value=None) -> dict:
    """平均消化時間と閉店判定用の安全側時間を返す。"""
    model = duration_model_for(profile)
    if not model:
        minutes = profile.get("estimated_play_minutes")
        return {
            "estimated_play_minutes": int(minutes) if minutes else None,
            "safe_play_minutes": int(minutes) + 30 if minutes else 120,
            "confidence": "legacy" if minutes else "unknown",
            "breakdown": {},
            "basis": "固定値" if minutes else "未登録",
        }

    start = float(profile.get("start_threshold") or 0)
    try:
        extra = max(0.0, float(current_value) - start)
    except (TypeError, ValueError):
        extra = 0.0
    # 深いゲーム数ほど残り通常時を減らす。ただし平均残りの25%は残す。
    base_normal_games = max(0.0, float(model.get("normal_games", 0)))
    reduction_rate = max(0.0, float(model.get("remaining_reduction_per_input", 0.55)))
    normal_games = max(base_normal_games * 0.25, base_normal_games - extra * reduction_rate)
    normal_minutes = normal_games / max(1.0, float(model.get("normal_games_per_minute", 13.2)))
    breakdown = {
        "normal": ceil(normal_minutes),
        "cz_forecast": int(model.get("cz_minutes", 0)),
        "at_bonus": int(model.get("at_bonus_minutes", 0)),
        "pullback": int(model.get("pullback_minutes", 0)),
        "variable_speed": int(model.get("variable_speed_minutes", 0)),
    }
    mean_minutes = max(1, ceil(sum(breakdown.values())))
    safe_buffer = max(0, int(model.get("safe_buffer_minutes", 30)))
    return {
        "estimated_play_minutes": mean_minutes,
        "safe_play_minutes": mean_minutes + safe_buffer,
        "safe_buffer_minutes": safe_buffer,
        "confidence": str(model.get("confidence") or "modeled"),
        "breakdown": breakdown,
        "basis": str(model.get("basis") or DURATION_BASIS),
    }


def apply_catalog_enrichment(profile: dict) -> dict:
    """カタログ行をDBへ保存する前に、モデルを自己記述可能にする。"""
    result = dict(profile)
    model = duration_model_for(result)
    if model:
        result["duration_model"] = dict(model)
        duration = estimate_duration(result, result.get("start_threshold"))
        result["estimated_play_minutes"] = duration["estimated_play_minutes"]
        result["safe_play_minutes"] = duration["safe_play_minutes"]
        result["duration_confidence"] = duration["confidence"]
        result["duration_basis"] = duration["basis"]
    return result


# 根拠が確認できた専用ロジック。差枚だけの汎用 -2400/+2400 判定は使わない。
def calculate_machine_adjustment(profile: dict, context: dict | None = None) -> dict:
    context = context or {}
    name = str(profile.get("machine_name") or "")
    key = str(profile.get("catalog_key") or "")
    start = float(profile.get("start_threshold") or 0)

    if "からくりサーカス" in name:
        misses = int(context.get("cz_misses") or 0)
        at_gap = float(context.get("at_gap") or 0)
        fate_ready = str(context.get("fate_progress") or "") == "ready"
        if misses >= 4:
            return {"active": True, "verified": True, "adjusted_start_threshold": 0,
                    "reason": "CZ4スルー後は次回CZがAT直撃へ書き換わる公開条件です",
                    "rule": "karakuri_cz_4_misses"}
        if at_gap >= 2500:
            return {"active": True, "verified": True, "adjusted_start_threshold": 0,
                    "reason": "AT間2500G到達はAT＋成功確定の激情ジャッジ条件です",
                    "rule": "karakuri_at_gap_2500"}
        if fate_ready:
            return {"active": True, "verified": True, "adjusted_start_threshold": start,
                    "reason": "運命の一劇権利あり。通常ボーダーは浅くせず、状態を保持して判定します",
                    "rule": "karakuri_fate_ready"}

    if "ゴッドイーター" in name:
        if bool(context.get("section_reset_confirmed")):
            return {"active": True, "verified": True, "adjusted_start_threshold": start,
                    "reason": "有利区間切断を確認済み。未公開の差枚ライン推測による浅追いはしません",
                    "rule": "god_eater_cut_confirmed"}

    if "かぐや様" in name:
        if bool(context.get("section_reset_confirmed")):
            return {"active": True, "verified": True, "adjusted_start_threshold": start,
                    "reason": "有利区間リセット確認済み。BIG後/REG後条件を優先します",
                    "rule": "kaguya_cut_confirmed"}

    if "ヴァルヴレイヴ" in name:
        if key == "valvrave1-pullback-66g-v1":
            return {"active": True, "verified": True, "adjusted_start_threshold": 0,
                    "reason": "ボーナス/AT終了後66G以内の引き戻し確認条件です",
                    "rule": "valvrave_66g_pullback"}
        cz_misses = int(context.get("cz_misses") or 0)
        if cz_misses >= 7:
            return {"active": True, "verified": True, "adjusted_start_threshold": 0,
                    "reason": "CZ7スルー後は次回CZ成功確定の公開条件です",
                    "rule": "valvrave_cz_7_misses"}
        decision_bonuses = int(context.get("decision_bonus_streak") or 0)
        if decision_bonuses >= 4:
            return {"active": True, "verified": True, "adjusted_start_threshold": 0,
                    "reason": "決戦ボーナス4連続後は次回革命ボーナス確定の公開条件です",
                    "rule": "valvrave_decision_bonus_4"}

    if "聖闘士星矢" in name:
        gb_misses = int(context.get("gb_misses") or 0)
        futsu = str(context.get("futsu_hint") or "")
        if gb_misses >= 8:
            return {"active": True, "verified": True, "adjusted_start_threshold": 0,
                    "reason": "GB8連続スルー後は次回GBでAT当選する公開条件です",
                    "rule": "seiya_gb_8_misses"}
        if futsu == "max":
            return {"active": True, "verified": True, "adjusted_start_threshold": 0,
                    "reason": "不屈MAX示唆を確認。次回GB突破条件として扱います",
                    "rule": "seiya_futsu_max"}

    difference = context.get("section_difference_coins")
    if difference not in (None, ""):
        return {"active": False, "verified": False,
                "reason": "この機種の強制切断差枚ラインを一次公開情報で確認できないため自動補正しません",
                "rule": "unverified_difference_only"}
    return {"active": False}
