"""
設定変更検知モジュール。

「前半 N1G で観測 A、後半 N2G で観測 B」という2区間データを受け取り、
- 両区間を「同じ設定」から生成されたとする帰無仮説
- 「設定が途中で変わった」とする対立仮説

を尤度比で比較して「設定変更された可能性」を返す。

使い方:
    result = detect_setting_change(
        profile,
        obs_early  = Observation(3000, {"ベル": 411, "ボーナス合算": 9}),
        obs_late   = Observation(3000, {"ベル": 390, "ボーナス合算": 18}),
    )
    print(result.change_prob)   # 0.0〜1.0
    print(result.early_setting) # 前半推測設定（期待値）
    print(result.late_setting)  # 後半推測設定（期待値）
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from core.bayes_engine import MachineProfile, Observation, SettingEstimator


@dataclass
class ChangeDetectionResult:
    change_prob: float            # 設定変更確率 (0〜1)
    log_bf: float                 # ベイズ因子 log10(変更モデル / 固定モデル)
    early_posterior: dict[str, float]
    late_posterior: dict[str, float]
    combined_posterior: dict[str, float]
    early_setting: float          # 前半 期待設定値
    late_setting: float           # 後半 期待設定値
    combined_setting: float       # 全体 期待設定値
    verdict: str                  # "変更の可能性高" | "変更なし（可能性低）" | "判定不能"


def detect_setting_change(
    profile: MachineProfile,
    obs_early: Observation,
    obs_late: Observation,
    prior: dict[str, float] | None = None,
    change_prior: float = 0.10,   # 事前に設定変更があると思う確率
) -> ChangeDetectionResult:
    """
    2区間の観測から設定変更確率を推定する。

    Args:
        profile:      機種プロファイル
        obs_early:    前半の観測（朝〜中盤）
        obs_late:     後半の観測（中盤〜終盤）
        prior:        事前分布（Noneで一様）
        change_prior: 設定変更があると思う事前確率（デフォルト10%）
    """
    estimator = SettingEstimator(profile)
    settings = list(profile.settings)
    if not 0.0 < change_prior < 1.0:
        raise ValueError("設定変更の事前確率は0より大きく1未満で指定してください")

    # 前半・後半・合計それぞれの事後分布
    post_early = estimator.estimate(obs_early, prior=prior)
    post_late = estimator.estimate(obs_late, prior=prior)
    combined_games = obs_early.total_games + obs_late.total_games
    combined_counts = {
        k: obs_early.counts.get(k, 0) + obs_late.counts.get(k, 0)
        for k in set(list(obs_early.counts) + list(obs_late.counts))
    }
    post_combined = estimator.estimate(
        Observation(combined_games, combined_counts), prior=prior
    )

    # 尤度計算
    # H0: 全期間同一設定 s → L0(s) = P(early|s) * P(late|s)
    # H1: 前半 s1、後半 s2 → L1 = Σ_{s1} Σ_{s2} P(early|s1) * P(late|s2) * p(s1) * p(s2)
    # ただし s1 ≠ s2 のときのみ「変更」とする

    if prior:
        prior_values = {s: float(prior.get(s, 0.0)) for s in settings}
        if any(not math.isfinite(v) or v < 0 for v in prior_values.values()):
            raise ValueError("事前分布には0以上の有限値を指定してください")
        prior_total = sum(prior_values.values())
        if prior_total <= 0:
            raise ValueError("事前分布の合計は0より大きくしてください")
        normalized_prior = {s: prior_values[s] / prior_total for s in settings}
    else:
        normalized_prior = {s: 1.0 / len(settings) for s in settings}
    log_prior = {s: math.log(max(normalized_prior[s], 1e-12)) for s in settings}

    def log_likelihood(obs: Observation, setting: str) -> float:
        el_map = {el.name: el for el in profile.elements}
        ll = 0.0
        N = obs.total_games
        for name, k in obs.counts.items():
            el = el_map.get(name)
            if el is None:
                continue
            if k > N:
                raise ValueError(f"要素 '{name}' の回数 {k} が 0..{N} の範囲外です")
            p = el.probabilities[setting]
            ll += k * math.log(p) + (N - k) * math.log(1 - p)
        return ll

    # H0: 固定設定モデルの周辺尤度
    log_liks_h0 = []
    for s in settings:
        ll = log_prior[s] + log_likelihood(obs_early, s) + log_likelihood(obs_late, s)
        log_liks_h0.append(ll)
    log_marg_h0 = _logsumexp(log_liks_h0)

    # H1: 変更モデルの周辺尤度（s1 ≠ s2）
    log_liks_h1 = []
    for s1 in settings:
        for s2 in settings:
            if s1 == s2:
                continue
            ll = log_prior[s1] + log_prior[s2] + log_likelihood(obs_early, s1) + log_likelihood(obs_late, s2)
            log_liks_h1.append(ll)
    log_marg_h1 = _logsumexp(log_liks_h1) if log_liks_h1 else -1e10
    # H1 は「設定が変わった」ことを条件とするため、s1 != s2 の事前確率で正規化する。
    distinct_pair_prob = 1.0 - sum(p * p for p in normalized_prior.values())
    if distinct_pair_prob <= 0:
        raise ValueError("設定変更モデルを計算できる事前分布ではありません")
    log_marg_h1 -= math.log(distinct_pair_prob)

    # ベイズ因子 BF = P(data|H1) / P(data|H0)
    log_bf = (log_marg_h1 - log_marg_h0) / math.log(10)  # log10

    # 変更確率 P(H1|data) = BF * prior_odds / (1 + BF * prior_odds)
    prior_odds = change_prior / (1 - change_prior)
    log_bayes_odds = (log_marg_h1 - log_marg_h0) + math.log(prior_odds)
    if log_bayes_odds >= 0:
        change_prob = 1.0 / (1.0 + math.exp(-log_bayes_odds))
    else:
        odds = math.exp(log_bayes_odds)
        change_prob = odds / (1.0 + odds)
    change_prob = max(0.0, min(1.0, change_prob))

    if change_prob >= 0.60:
        verdict = "[!] 設定変更の可能性が高い"
    elif change_prob >= 0.35:
        verdict = "[?] 変更の可能性あり（要注意）"
    else:
        verdict = "[OK] 設定変更なし（可能性低）"

    return ChangeDetectionResult(
        change_prob=round(change_prob, 4),
        log_bf=round(log_bf, 3),
        early_posterior=post_early,
        late_posterior=post_late,
        combined_posterior=post_combined,
        early_setting=round(estimator.expected_setting(post_early), 2),
        late_setting=round(estimator.expected_setting(post_late), 2),
        combined_setting=round(estimator.expected_setting(post_combined), 2),
        verdict=verdict,
    )


def _logsumexp(log_vals: list[float]) -> float:
    if not log_vals:
        return -math.inf
    m = max(log_vals)
    return m + math.log(sum(math.exp(v - m) for v in log_vals))


if __name__ == "__main__":
    import json, sys
    from pathlib import Path
    ROOT = Path(__file__).parent.parent
    sys.path.insert(0, str(ROOT))

    data = json.loads((ROOT / "data/machines/ゴーゴージャグラー.json").read_text(encoding="utf-8-sig"))
    profile = MachineProfile.from_dict(data)

    # デモ: 前半は低設定挙動、後半は高設定挙動（変更を模擬）
    obs_early = Observation(3000, {"BB確率": 8, "RB確率": 7, "ブドウ確率": 432})
    obs_late  = Observation(3000, {"BB確率": 15, "RB確率": 18, "ブドウ確率": 460})

    result = detect_setting_change(profile, obs_early, obs_late)
    print(f"change_prob: {result.change_prob*100:.1f}%")
    print(f"verdict: {result.verdict}")
    print(f"early_setting: {result.early_setting:.2f}")
    print(f"late_setting: {result.late_setting:.2f}")
    print(f"log10(BF): {result.log_bf:.2f}")
