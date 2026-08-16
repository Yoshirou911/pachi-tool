import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _catalog(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_mobile_catalog_matches_canonical_catalog():
    assert _catalog("mobile/catalog.json") == _catalog("data/opportunity_catalog.json")


def test_catalog_keys_are_unique_and_start_values_are_backed_by_curves():
    profiles = _catalog("data/opportunity_catalog.json")["profiles"]
    keys = [profile["catalog_key"] for profile in profiles]
    assert len(keys) == len(set(keys))

    for profile in profiles:
        start = profile["start_threshold"]
        matching_points = [point for point in profile["curve_points"] if point["value"] == start]
        assert matching_points, profile["catalog_key"]
        assert matching_points[0]["ev_yen"] == profile["expected_value_yen"]


def test_current_machine_batch_keeps_conditions_separate():
    profiles = _catalog("data/opportunity_catalog.json")["profiles"]
    by_key = {profile["catalog_key"]: profile for profile in profiles}

    expected = {
        "god-eater-resurrection-normal-equivalent-v1": (500, 1300, "normal"),
        "god-eater-resurrection-reset-equivalent-v1": (300, 1500, "reset_confirmed"),
        "kaguya-sama-big-after-equivalent-v1": (550, 1182, "normal"),
        "kaguya-sama-reg-after-equivalent-v1": (400, 1299, "normal"),
        "monster-hunter-rise-normal-equivalent-v1": (550, 1171, "normal"),
        "karakuri-circus-cz-equivalent-v1": (350, 1173, "normal"),
        "karakuri-circus-cz-56-cash-v1": (450, 1374, "normal"),
    }
    for key, (start, value, reset_status) in expected.items():
        assert by_key[key]["start_threshold"] == start
        assert by_key[key]["expected_value_yen"] == value
        assert by_key[key]["reset_status"] == reset_status

