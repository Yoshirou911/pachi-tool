"""
設定推測ベイズ推定エンジン
============================

パチスロの設定推測を、複数の設定差要素を同時に取り込んで
ベイズ推定で行うコアエンジン。ツール全体の「精度の心臓部」。

設計方針
--------
- 機種ごとの理論値は外部データ(JSON)として差し込む。エンジンは機種非依存。
- すべての設定差要素を「毎ゲーム抽選で確率 p_s で出現する事象」として統一的に扱う。
  小役(共通ベル等)も、ボーナス確率(1/x)も、AT初当たりも、この形に落とせる。
- 尤度は二項分布。設定間比較では組合せ係数 C(N,k) が共通なので
  対数尤度 k*log(p) + (N-k)*log(1-p) のみで比較でき、数値的にも安定。
- 事前分布は差し替え可能(通常は一様。店傾向分析の出力を事前に流し込める設計)。

これにより「サンプルが増えるほど事後分布が尖る = 精度が上がる」挙動になる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import log
from typing import Mapping, Sequence


# --------------------------------------------------------------------------
# データ構造
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CountElement:
    """設定差のある「カウント要素」1つ分の定義。

    例: 共通ベル、チェリー、ボーナス確率、AT初当たり、特定演出の出現など。

    probabilities: 設定ラベル -> その設定での1ゲームあたり出現確率。
        値は確率(0〜1)。1/x 形式しか手元に無い場合は from_one_over() を使う。
    """
    name: str
    probabilities: Mapping[str, float]

    def __post_init__(self) -> None:
        for label, p in self.probabilities.items():
            if not (0.0 < p < 1.0):
                raise ValueError(
                    f"要素 '{self.name}' 設定 '{label}' の確率 {p} が (0,1) の範囲外です"
                )


@dataclass(frozen=True)
class MachineProfile:
    """1機種分の設定差データ。"""
    machine_name: str
    settings: Sequence[str]            # 例: ["1", "2", "3", "4", "5", "6"]
    elements: Sequence[CountElement]

    def __post_init__(self) -> None:
        for el in self.elements:
            missing = set(self.settings) - set(el.probabilities.keys())
            if missing:
                raise ValueError(
                    f"要素 '{el.name}' に設定 {sorted(missing)} の確率がありません"
                )

    @classmethod
    def from_dict(cls, data: Mapping) -> "MachineProfile":
        """JSON/dict から構築。

        確率は "p"(直接確率) または "one_over"(1/x の x)で指定可能。
        例:
        {
          "machine_name": "サンプル機",
          "settings": ["1","2","3","4","5","6"],
          "elements": [
            {"name": "共通ベル", "one_over": {"1": 7.30, "6": 7.10}},
            {"name": "ボーナス合算", "one_over": {"1": 273.1, "6": 199.3}}
          ]
        }
        """
        settings = [str(s) for s in data["settings"]]
        elements = []
        for el in data["elements"]:
            if "p" in el:
                probs = {str(k): float(v) for k, v in el["p"].items()}
            elif "one_over" in el:
                probs = {str(k): 1.0 / float(v) for k, v in el["one_over"].items()}
            else:
                raise ValueError(f"要素 '{el.get('name')}' に p か one_over が必要です")
            elements.append(CountElement(name=el["name"], probabilities=probs))
        return cls(machine_name=data["machine_name"], settings=settings, elements=elements)


@dataclass
class Observation:
    """実戦で観測したデータ。

    total_games: そのデータ区間の総ゲーム数。
    counts: 要素名 -> 観測回数。
        記録できなかった要素は省略してよい(その要素は尤度に寄与しない)。
    """
    total_games: int
    counts: Mapping[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 推定エンジン
# --------------------------------------------------------------------------

class SettingEstimator:
    """機種プロファイルを受け取り、観測データから設定の事後分布を返す。"""

    def __init__(self, profile: MachineProfile):
        self.profile = profile
        self._elem_by_name = {el.name: el for el in profile.elements}

    def estimate(
        self,
        obs: Observation,
        prior: Mapping[str, float] | None = None,
    ) -> dict[str, float]:
        """事後分布 P(設定 | 観測) を返す。

        prior を渡すと事前分布を差し替えられる(店傾向の出力などを注入可能)。
        渡さなければ一様事前。
        """
        settings = self.profile.settings
        if prior is None:
            log_post = {s: 0.0 for s in settings}           # 一様事前 = log(1/n) 定数は無視可
        else:
            total = sum(prior.values())
            log_post = {s: log(prior[s] / total) for s in settings}

        N = obs.total_games
        for name, k in obs.counts.items():
            el = self._elem_by_name.get(name)
            if el is None:
                continue  # 未知の要素名は無視
            if k < 0 or k > N:
                raise ValueError(f"要素 '{name}' の回数 {k} が 0..{N} の範囲外です")
            for s in settings:
                p = el.probabilities[s]
                # 二項対数尤度(組合せ係数は設定間で共通なので省略)
                log_lik = k * log(p)
                if N - k > 0:
                    log_lik += (N - k) * log(1.0 - p)
                log_post[s] += log_lik

        return self._normalize(log_post)

    @staticmethod
    def _normalize(log_post: Mapping[str, float]) -> dict[str, float]:
        """対数事後を正規化して確率に戻す(log-sum-exp で安定化)。"""
        m = max(log_post.values())
        exps = {s: pow(2.718281828459045, lp - m) for s, lp in log_post.items()}
        z = sum(exps.values())
        return {s: v / z for s, v in exps.items()}

    # ------- 便利メソッド ----------------------------------------------------

    def expected_setting(self, posterior: Mapping[str, float]) -> float:
        """事後分布の期待設定値(設定ラベルが数値の場合)。"""
        return sum(float(s) * p for s, p in posterior.items())

    def high_setting_prob(
        self, posterior: Mapping[str, float], threshold: int = 4
    ) -> float:
        """設定 threshold 以上の合計確率(高設定期待度)。"""
        return sum(p for s, p in posterior.items() if float(s) >= threshold)


# --------------------------------------------------------------------------
# デモ(直接実行で動作確認)
# --------------------------------------------------------------------------

if __name__ == "__main__":
    # ※ 数値は説明用の架空機種。実機では公表/解析された正確な理論値に差し替える。
    demo = {
        "machine_name": "デモ機(架空)",
        "settings": ["1", "2", "3", "4", "5", "6"],
        "elements": [
            {"name": "共通ベル",
             "one_over": {"1": 7.30, "2": 7.28, "3": 7.20, "4": 7.18, "5": 7.05, "6": 6.95}},
            {"name": "ボーナス合算",
             "one_over": {"1": 273.1, "2": 264.3, "3": 254.0, "4": 240.1, "5": 220.5, "6": 199.3}},
            {"name": "AT初当たり",
             "one_over": {"1": 410.0, "2": 395.0, "3": 372.0, "4": 350.0, "5": 320.0, "6": 290.0}},
        ],
    }
    profile = MachineProfile.from_dict(demo)
    est = SettingEstimator(profile)

    print(f"=== {profile.machine_name} 設定推測デモ ===\n")

    for label, obs in {
        "少サンプル(2000G)": Observation(2000, {"共通ベル": 285, "ボーナス合算": 9, "AT初当たり": 6}),
        "中サンプル(6000G)": Observation(6000, {"共通ベル": 858, "ボーナス合算": 28, "AT初当たり": 19}),
        "大サンプル(12000G・高設定挙動)": Observation(
            12000, {"共通ベル": 1726, "ボーナス合算": 60, "AT初当たり": 41}),
    }.items():
        post = est.estimate(obs)
        print(f"[{label}]  総G={obs.total_games}")
        for s in profile.settings:
            bar = "#" * round(post[s] * 40)
            print(f"  設定{s}: {post[s]*100:5.1f}%  {bar}")
        print(f"  期待設定値: {est.expected_setting(post):.2f}  /  "
              f"設定4以上: {est.high_setting_prob(post)*100:.1f}%\n")
