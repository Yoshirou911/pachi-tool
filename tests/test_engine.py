"""
bayes_engine / value/ev 単体テスト。
実行: python -m pytest tests/test_engine.py -v
"""
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.bayes_engine import CountElement, MachineProfile, Observation, SettingEstimator
from core.setting_change import detect_setting_change
from value.ev import EVResult, compute_ev


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------

DEMO_DATA = {
    "machine_name": "テスト機",
    "settings": ["1", "2", "3", "4", "5", "6"],
    "machine_kw": {
        "1": 0.970, "2": 0.985, "3": 0.999,
        "4": 1.010, "5": 1.040, "6": 1.080,
    },
    "elements": [
        {
            "name": "ベル",
            "p": {"1": 1/7.3, "2": 1/7.28, "3": 1/7.20,
                  "4": 1/7.18, "5": 1/7.05, "6": 1/6.95},
        },
        {
            "name": "ボーナス合算",
            "one_over": {"1": 273.1, "2": 264.3, "3": 254.0,
                         "4": 240.1, "5": 220.5, "6": 199.3},
        },
    ],
}

@pytest.fixture
def demo_profile():
    return MachineProfile.from_dict(DEMO_DATA)

@pytest.fixture
def estimator(demo_profile):
    return SettingEstimator(demo_profile)


# ---------------------------------------------------------------------------
# MachineProfile
# ---------------------------------------------------------------------------

class TestMachineProfile:
    def test_from_dict_p(self):
        profile = MachineProfile.from_dict(DEMO_DATA)
        assert profile.machine_name == "テスト機"
        assert list(profile.settings) == ["1","2","3","4","5","6"]
        assert len(profile.elements) == 2

    def test_from_dict_one_over(self):
        profile = MachineProfile.from_dict(DEMO_DATA)
        bonus = next(e for e in profile.elements if e.name == "ボーナス合算")
        assert abs(bonus.probabilities["1"] - 1/273.1) < 1e-10
        assert abs(bonus.probabilities["6"] - 1/199.3) < 1e-10

    def test_missing_setting_raises(self):
        bad_data = {
            "machine_name": "bad",
            "settings": ["1","2","3"],
            "elements": [
                {"name": "ベル", "p": {"1": 0.1, "2": 0.09}},  # "3" missing
            ],
        }
        with pytest.raises(ValueError, match="設定"):
            MachineProfile.from_dict(bad_data)

    def test_probability_range_validation(self):
        with pytest.raises(ValueError):
            CountElement("test", {"1": 0.0, "2": 0.5})  # p=0.0 is invalid

    def test_partial_settings_normalized(self):
        # 設定3非公開の機種
        data = {
            "machine_name": "partial",
            "settings": ["1","2","4","5","6"],
            "elements": [
                {"name": "AT合算", "p": {"1":0.003,"2":0.0031,"4":0.0035,"5":0.004,"6":0.0045}},
            ],
        }
        profile = MachineProfile.from_dict(data)
        assert "3" not in profile.settings
        assert len(profile.settings) == 5

    def test_bonus_combined_probability_is_recalculated(self):
        data = {
            "machine_name": "合算補正テスト",
            "settings": ["1", "6"],
            "elements": [
                {"name": "BB確率", "p": {"1": 0.01, "6": 0.02}},
                {"name": "RB確率", "p": {"1": 0.02, "6": 0.03}},
                {"name": "合算確率", "p": {"1": 0.001, "6": 0.002}},
            ],
        }
        profile = MachineProfile.from_dict(data)
        combined = next(e for e in profile.elements if e.name == "合算確率")
        assert combined.probabilities == {"1": 0.03, "6": 0.05}


# ---------------------------------------------------------------------------
# SettingEstimator
# ---------------------------------------------------------------------------

class TestSettingEstimator:
    def test_uniform_prior_no_obs(self, estimator, demo_profile):
        obs = Observation(total_games=0, counts={})
        posterior = estimator.estimate(obs)
        # 観測なし → 一様分布
        for s in demo_profile.settings:
            assert abs(posterior[s] - 1/6) < 1e-6

    def test_posterior_sums_to_one(self, estimator):
        obs = Observation(3000, {"ベル": 415, "ボーナス合算": 11})
        posterior = estimator.estimate(obs)
        assert abs(sum(posterior.values()) - 1.0) < 1e-9

    def test_high_setting_obs_biases_posterior(self, estimator):
        # 高設定挙動: ボーナス多め
        obs = Observation(6000, {"ボーナス合算": 35})  # 1/171 ≈ 設定6水準
        posterior = estimator.estimate(obs)
        high_prob = sum(posterior[s] for s in ["4","5","6"])
        assert high_prob > 0.7, f"高設定確率が低すぎる: {high_prob:.2f}"

    def test_low_setting_obs_biases_posterior(self, estimator):
        # 低設定挙動: ボーナス少なめ
        obs = Observation(6000, {"ボーナス合算": 12})  # 1/500 = 低設定以下
        posterior = estimator.estimate(obs)
        low_prob = sum(posterior[s] for s in ["1","2"])
        assert low_prob > 0.5, f"低設定確率が低すぎる: {low_prob:.2f}"

    def test_custom_prior_shifts_posterior(self, estimator, demo_profile):
        obs = Observation(1000, {})  # 観測なし
        # 設定6に偏った事前分布
        biased_prior = {s: 0.01 for s in demo_profile.settings}
        biased_prior["6"] = 0.95
        posterior = estimator.estimate(obs, prior=biased_prior)
        assert posterior["6"] > 0.9

    def test_expected_setting(self, estimator, demo_profile):
        # 一様分布の期待設定値 = 3.5
        uniform = {s: 1/6 for s in demo_profile.settings}
        assert abs(estimator.expected_setting(uniform) - 3.5) < 1e-6

    def test_high_setting_prob(self, estimator, demo_profile):
        # 設定5・6のみ確率 → 高設定率 = 1.0
        posterior = {s: 0.0 for s in demo_profile.settings}
        posterior["5"] = 0.5
        posterior["6"] = 0.5
        assert estimator.high_setting_prob(posterior) == 1.0

    def test_unknown_element_ignored(self, estimator):
        obs = Observation(1000, {"存在しない要素": 100, "ベル": 140})
        # エラーなく動く
        posterior = estimator.estimate(obs)
        assert abs(sum(posterior.values()) - 1.0) < 1e-9

    def test_count_out_of_range_raises(self, estimator):
        with pytest.raises(ValueError):
            estimator.estimate(Observation(1000, {"ベル": 1001}))  # count > total_games

    def test_invalid_observation_and_prior_raise(self, estimator):
        with pytest.raises(ValueError, match="総ゲーム数"):
            Observation(-1, {})
        with pytest.raises(ValueError, match="事前分布"):
            estimator.estimate(Observation(100, {}), prior={"1": 0, "2": 0})


class TestSettingChange:
    def test_zero_count_is_valid_evidence(self):
        profile = MachineProfile.from_dict({
            "machine_name": "二設定テスト機",
            "settings": ["1", "6"],
            "elements": [{"name": "当選", "p": {"1": 0.01, "6": 0.10}}],
        })
        result = detect_setting_change(
            profile,
            Observation(100, {"当選": 0}),
            Observation(100, {"当選": 10}),
            change_prior=0.10,
        )
        assert result.change_prob > 0.5

    def test_change_prior_must_be_probability(self, demo_profile):
        with pytest.raises(ValueError, match="事前確率"):
            detect_setting_change(
                demo_profile, Observation(100, {}), Observation(100, {}), change_prior=1.0
            )


# ---------------------------------------------------------------------------
# value/ev
# ---------------------------------------------------------------------------

class TestComputeEV:
    def test_ev_uniform_posterior(self):
        posterior = {"1":1/6,"2":1/6,"3":1/6,"4":1/6,"5":1/6,"6":1/6}
        kw = {"1":0.970,"2":0.985,"3":0.999,"4":1.010,"5":1.040,"6":1.080}
        result = compute_ev(posterior, machine_kw=kw)
        expected = sum(kw[s] / 6 for s in kw)
        assert abs(result.ev - expected) < 1e-6

    def test_retreat_when_ev_low(self):
        # 設定1・2に偏ったポステリア → EV低い → 撤退推奨
        posterior = {"1":0.8,"2":0.15,"3":0.03,"4":0.01,"5":0.01,"6":0.0}
        result = compute_ev(posterior)
        assert result.should_retreat

    def test_no_retreat_when_ev_high(self):
        # 設定5・6に偏った → EV高い → 撤退不要
        posterior = {"4":0.1,"5":0.45,"6":0.45}
        kw = {"4":1.010,"5":1.040,"6":1.080}
        result = compute_ev(posterior, machine_kw=kw)
        assert not result.should_retreat
        assert result.ev > 1.0

    def test_ev_pct_matches_ev(self):
        posterior = {"1":0.5,"6":0.5}
        kw = {"1":0.97,"6":1.08}
        result = compute_ev(posterior, machine_kw=kw)
        assert abs(result.ev_pct - result.ev * 100) < 1e-6

    def test_posterior_normalization(self):
        # 正規化前でも正しく動く
        posterior = {"1":10,"2":20,"6":30}
        result = compute_ev(posterior)
        assert abs(sum(result.posterior.values()) - 1.0) < 1e-9

    def test_loads_kw_from_machine_json(self):
        # スマスロ北斗の拳の machine_kw を自動ロード
        posterior = {"1":0.2,"2":0.2,"4":0.2,"5":0.2,"6":0.2}
        result = compute_ev(posterior, machine_name="スマスロ北斗の拳")
        assert result.kw_source == "machine_data"
        assert result.ev > 0.9  # 妥当な範囲

    def test_fallback_kw_when_machine_unknown(self):
        posterior = {"1":0.5,"6":0.5}
        result = compute_ev(posterior, machine_name="存在しない機種")
        assert result.kw_source == "default"


# ---------------------------------------------------------------------------
# 実機種 JSON 読み込みテスト
# ---------------------------------------------------------------------------

class TestRealMachineData:
    def _load_json(self, name: str) -> dict:
        path = ROOT / "data" / "machines" / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def test_juggler_profiles_are_removed(self):
        machines_dir = ROOT / "data" / "machines"
        assert not list(machines_dir.glob("*ジャグラー*.json"))

    def test_smartslot_hokuto_loadable(self):
        data = self._load_json("スマスロ北斗の拳")
        profile = MachineProfile.from_dict(data)
        assert len(profile.elements) == 5
        assert "1" in profile.settings and "6" in profile.settings

    def test_kabaneri_partial_settings(self):
        data = self._load_json("パチスロ甲鉄城のカバネリ")
        profile = MachineProfile.from_dict(data)
        assert "3" not in profile.settings  # 設定3非公開

    def test_hokuto_no_ken_partial_settings(self):
        data = self._load_json("スマスロ北斗の拳")
        profile = MachineProfile.from_dict(data)
        assert "3" not in profile.settings

    def test_kabaneri_smart_normalized(self):
        data = self._load_json("スマスロカバネリ")
        profile = MachineProfile.from_dict(data)
        # settings は intersection = ["1","2","6"] のはず
        assert set(profile.settings) == {"1", "2", "6"}

    def test_all_machines_estimable(self):
        machines_dir = ROOT / "data" / "machines"
        for path in machines_dir.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            try:
                profile = MachineProfile.from_dict(data)
                est = SettingEstimator(profile)
                obs = Observation(1000, {})
                posterior = est.estimate(obs)
                assert abs(sum(posterior.values()) - 1.0) < 1e-9, f"{path.name}: posterior sum != 1"
            except Exception as e:
                pytest.fail(f"{path.name}: {e}")
