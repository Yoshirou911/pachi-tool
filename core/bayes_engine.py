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
        laplace_alpha: float = 0.5,
    ) -> dict[str, float]:
        """事後分布 P(設定 | 観測) を返す。

        prior を渡すと事前分布を差し替えられる(店傾向の出力などを注入可能)。
        渡さなければ一様事前。

        laplace_alpha: ラプラス平滑化の疑似カウント (0.5 = Jeffreys prior)。
            N が小さい時に確率が 0/1 に張り付くのを防ぐ。
        """
        settings = self.profile.settings
        if prior is None:
            log_post = {s: 0.0 for s in settings}
        else:
            total = sum(prior.values())
            # ゼロ事前を防ぐため微小値でクランプ
            log_post = {
                s: log(max(prior.get(s, 0) / total, 1e-10))
                for s in settings
            }

        N = obs.total_games
        for name, k in obs.counts.items():
            el = self._elem_by_name.get(name)
            if el is None:
                continue
            if k < 0 or k > N:
                raise ValueError(f"要素 '{name}' の回数 {k} が 0..{N} の範囲外です")
            for s in settings:
                p = el.probabilities[s]
                # ラプラス平滑化: k → k+α, N-k → N-k+α
                k_s   = k + laplace_alpha
                nk_s  = (N - k) + laplace_alpha
                n_s   = N + 2 * laplace_alpha
                # 平滑化後の二項対数尤度
                log_lik = k_s * log(p) + nk_s * log(1.0 - p)
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

    def credible_interval(
        self, posterior: Mapping[str, float], prob: float = 0.90
    ) -> tuple[float, float]:
        """posterior の prob% 信用区間（設定値のCDF ベース）。

        例: (2.0, 5.0) → 「90%の確率で期待設定は2〜5の間」。
        設定値を離散値として扱い、累積確率でlo/hiをサンプリング。
        """
        tail = (1.0 - prob) / 2.0
        items = sorted(
            ((float(s), p) for s, p in posterior.items()),
            key=lambda x: x[0]
        )
        cumsum = 0.0
        lo = items[0][0]
        hi = items[-1][0]
        for setting_val, p in items:
            if cumsum < tail:
                lo = setting_val
            cumsum += p
            if cumsum >= 1.0 - tail and hi == items[-1][0]:
                hi = setting_val
        return lo, hi

    def element_discrimination_power(self) -> dict[str, float]:
        """各要素の「設定識別力」をlog-odds比で計算（大きいほど識別しやすい）。

        discrimination = log(max_p / min_p) across settings.
        これが大きい要素ほど設定間の差が大きく、精度に貢献する。
        """
        result = {}
        for el in self.profile.elements:
            ps = list(el.probabilities.values())
            if not ps:
                continue
            max_p = max(ps)
            min_p = min(ps)
            if min_p > 0:
                result[el.name] = log(max_p / min_p)
            else:
                result[el.name] = 0.0
        return result

    def find_correlated_elements(self, threshold: float = 0.95) -> list[tuple[str, str, float]]:
        """相関が強い要素ペアを検出する（同じ情報を重複計上していないかチェック）。

        各設定にわたる確率ベクトル間の相関係数が threshold を超えるペアを返す。
        例: [("BB確率", "合算確率", 0.98)] → 合算確率にBBが含まれているため高相関
        """
        import math as _math
        elements = list(self.profile.elements)
        correlated = []
        for i in range(len(elements)):
            for j in range(i + 1, len(elements)):
                a = elements[i]
                b = elements[j]
                settings = list(self.profile.settings)
                pa = [a.probabilities[s] for s in settings]
                pb = [b.probabilities[s] for s in settings]
                n = len(pa)
                if n < 2:
                    continue
                ma = sum(pa) / n
                mb = sum(pb) / n
                cov = sum((pa[k] - ma) * (pb[k] - mb) for k in range(n)) / n
                sa = _math.sqrt(sum((x - ma)**2 for x in pa) / n)
                sb = _math.sqrt(sum((x - mb)**2 for x in pb) / n)
                if sa > 0 and sb > 0:
                    r = cov / (sa * sb)
                    if abs(r) >= threshold:
                        correlated.append((a.name, b.name, round(r, 3)))
        return correlated


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
