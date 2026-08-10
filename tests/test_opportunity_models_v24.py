import json
from pathlib import Path

from opportunity.machine_models import (
    DURATION_MODELS,
    calculate_machine_adjustment,
    estimate_duration,
)


ROOT = Path(__file__).resolve().parents[1]


def _profiles():
    return json.loads((ROOT / "data" / "opportunity_catalog.json").read_text(encoding="utf-8"))["profiles"]


def test_all_original_missing_duration_profiles_now_have_explainable_models():
    original_missing = {
        "hokuto-normal-equivalent-conservative-v1", "hokuto-normal-56-current-conservative-v1",
        "hokuto-reset-equivalent-conservative-v1", "monkey5-normal-equivalent-conservative-v1",
        "monkey5-normal-56-cash-conservative-v1", "tokyo-ghoul-cz-equivalent-safe-v1",
        "tokyo-ghoul-at-56-cash-safe-v1", "god-eater-resurrection-normal-equivalent-v1",
        "god-eater-resurrection-reset-equivalent-v1", "kaguya-sama-big-after-equivalent-v1",
        "kaguya-sama-reg-after-equivalent-v1", "monster-hunter-rise-normal-equivalent-v1",
        "karakuri-circus-cz-equivalent-v1", "karakuri-circus-cz-56-cash-v1",
    }
    profiles = {item["catalog_key"]: item for item in _profiles()}
    assert original_missing <= DURATION_MODELS.keys()
    for key in original_missing:
        duration = estimate_duration(profiles[key], profiles[key]["start_threshold"])
        assert duration["estimated_play_minutes"] > 0
        assert duration["safe_play_minutes"] > duration["estimated_play_minutes"]
        assert set(duration["breakdown"]) == {"normal", "cz_forecast", "at_bonus", "pullback", "variable_speed"}


def test_deeper_start_reduces_mean_duration_but_keeps_safe_buffer():
    profile = next(item for item in _profiles() if item["catalog_key"] == "god-eater-resurrection-normal-equivalent-v1")
    at_border = estimate_duration(profile, 500)
    deeper = estimate_duration(profile, 800)
    assert deeper["estimated_play_minutes"] < at_border["estimated_play_minutes"]
    assert deeper["safe_play_minutes"] > deeper["estimated_play_minutes"]


def test_machine_specific_rules_never_use_unverified_difference_as_permission():
    kaguya = {"catalog_key": "kaguya-sama-big-after-equivalent-v1", "machine_name": "スマスロ かぐや様は告らせたい", "start_threshold": 550}
    difference_only = calculate_machine_adjustment(kaguya, {"section_difference_coins": 2300})
    assert difference_only["active"] is False
    assert difference_only["verified"] is False
    confirmed = calculate_machine_adjustment(kaguya, {"section_reset_confirmed": True})
    assert confirmed["active"] is True
    assert confirmed["adjusted_start_threshold"] == 550


def test_machine_specific_ceiling_and_through_rules():
    karakuri = {"machine_name": "スマスロ からくりサーカス", "start_threshold": 350}
    assert calculate_machine_adjustment(karakuri, {"cz_misses": 4})["adjusted_start_threshold"] == 0
    valvrave = {"catalog_key": "valvrave1-bonus-gap-equivalent-v1", "machine_name": "スマスロ 革命機ヴァルヴレイヴ", "start_threshold": 700}
    assert calculate_machine_adjustment(valvrave, {"decision_bonus_streak": 4})["rule"] == "valvrave_decision_bonus_4"
    seiya = {"machine_name": "スマスロ 聖闘士星矢 海皇覚醒 CUSTOM EDITION", "start_threshold": 500}
    assert calculate_machine_adjustment(seiya, {"gb_misses": 8})["verified"] is True
