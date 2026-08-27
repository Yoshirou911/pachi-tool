"""設定推測・期待値エンドポイント"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.deps import (
    HALL_REPORTS_DB,
    MACHINES_DIR,
    WEB_DIR,
    _cache_get,
    _cache_invalidate_prefix,
    _cache_set,
    _get_event_conn,
    _get_machine_path,
    _get_reports_conn,
    logger,
)
from core.bayes_engine import MachineProfile, Observation, SettingEstimator
from core.setting_change import detect_setting_change
from hall.prior import compute_prior
from value.ev import compute_ev

router = APIRouter()

class EstimateRequest(BaseModel):
    machine_name: str
    games_total: int = Field(default=0, ge=0)
    started_from: int = Field(default=0, ge=0)  # 宵越しなど引き継ぎG数。実観測G数 = games_total - started_from
    element_counts: dict[str, int] = Field(default_factory=dict)
    element_trials: dict[str, int] = Field(default_factory=dict)
    prior: Optional[dict[str, float]] = None
    hall_name: str = ""
    weekday: Optional[int] = Field(default=None, ge=0, le=6)
    is_event_day: bool = False
    day_of_month: Optional[int] = Field(default=None, ge=1, le=31)
    min_setting: Optional[int] = Field(default=None, ge=1, le=6)  # 確定演出による下限設定 (e.g. 4 → 設4以上確定)
    seat_number: Optional[int] = Field(default=None, ge=1)  # 台番（同台の過去セッションを事前に反映）

class EstimateResponse(BaseModel):
    posterior: dict[str, float]
    expected_setting: float
    high_setting_prob: float
    ev: float
    ev_pct: float
    should_retreat: bool
    retreat_reason: str
    kw_source: str
    settings: list[str]
    confidence: float
    confidence_label: str
    observed_rates: dict[str, float]  # 実測出現率: {element_name: rate}
    element_analysis: list[dict]      # [{name, observed, theoretical_by_setting, direction}]
    sample_warning: Optional[str] = None   # 少サンプル警告
    recommended_games: Optional[int] = None  # 推奨最低G数
    credible_interval: Optional[list[float]] = None  # 90%信用区間 [lo, hi]
    element_powers: Optional[dict[str, float]] = None  # 各要素の識別力
    correlated_elements: Optional[list[list]] = None  # 相関の強い要素ペア
    sample_adequacy_pct: int = 0
    prediction_grade: str = "判定材料不足"
    confidence_scope: str = "後験分布の集中度（的中率ではありません）"
    observed_games: int = 0
    high_setting_probabilities: dict[str, float] = Field(default_factory=dict)
    action: str = "情報不足"
    action_reason: str = ""
    next_review_games: Optional[int] = None
    input_notice: str = ""
    profile_verified: bool = False
    profile_source_url: str = ""

class ChangeDetectRequest(BaseModel):
    machine_name: str
    early_games: int = Field(ge=1)
    late_games: int = Field(ge=1)
    early_counts: dict[str, int] = Field(default_factory=dict)
    late_counts: dict[str, int] = Field(default_factory=dict)
    prior: Optional[dict[str, float]] = None
    change_prior: float = Field(default=0.10, gt=0, lt=1)


@router.post("/api/estimate", response_model=EstimateResponse, tags=["estimate"])
def estimate(req: EstimateRequest) -> EstimateResponse:
    """
    観測カウントから設定推測を実行する。

    hall_name を渡すと、店傾向データを事前分布に自動反映。
    prior を明示した場合はそちらを優先。
    """
    path = _get_machine_path(req.machine_name)
    if not path.exists():
        raise HTTPException(404, f"機種データが見つかりません: {req.machine_name}")

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        profile = MachineProfile.from_dict(data)
    except Exception as e:
        raise HTTPException(422, f"機種データ読み込みエラー: {e}")

    # 事前分布: 明示 > 店傾向学習 > 一様
    prior = req.prior
    if prior is None and req.hall_name:
        try:
            prior = compute_prior(
                hall_name=req.hall_name,
                machine_name=req.machine_name,
                weekday=req.weekday,
                is_event_day=req.is_event_day,
                day_of_month=req.day_of_month,
                settings=list(profile.settings),
                seat_number=req.seat_number,
            )
        except Exception:
            prior = None

    # 宵越し補正: 実観測G数 = 総G数 − 引き継ぎG数
    if req.started_from > req.games_total:
        raise HTTPException(422, "引き継ぎG数は総ゲーム数以下で指定してください")
    observed_games = req.games_total - req.started_from
    obs = Observation(
        total_games=observed_games,
        counts=req.element_counts,
        trials=req.element_trials,
    )
    estimator = SettingEstimator(profile)

    try:
        posterior = estimator.estimate(obs, prior=prior)
    except ValueError as e:
        raise HTTPException(422, str(e))

    # 確定演出による下限設定制約 (e.g. min_setting=4 → 設1,2,3を0にして再正規化)
    if req.min_setting is not None:
        filtered = {s: p for s, p in posterior.items() if int(s) >= req.min_setting}
        total = sum(filtered.values())
        if total > 1e-12:
            posterior = {s: p / total for s, p in filtered.items()}
        # else: 設定が全て除外された場合は制約を無視（データ不整合時の安全弁）。
        # posterior はフィルタ前の値のまま = estimator.estimate(obs, prior=prior) と同一なので
        # 再計算せずそのまま使う。

    ev_result = compute_ev(posterior, machine_name=req.machine_name)

    # 推測信頼度: 一様分布からの KL ダイバージェンス的な集中度
    import math as _math
    n = len(posterior)
    uniform_entropy = _math.log(n)
    posterior_entropy = -sum(p * _math.log(max(p, 1e-12)) for p in posterior.values())
    # 1=一様(信頼度0)、0=デルタ関数(信頼度1)
    confidence = max(0.0, 1.0 - posterior_entropy / uniform_entropy) if uniform_entropy > 0 else 0.0
    if confidence >= 0.75:
        confidence_label = "非常に高"
    elif confidence >= 0.50:
        confidence_label = "高"
    elif confidence >= 0.25:
        confidence_label = "中"
    else:
        confidence_label = "低"

    # 要素別実測値 vs 理論値分析
    observed_rates: dict[str, float] = {}
    element_analysis = []
    if observed_games > 0:
        for el in profile.elements:
            cnt = req.element_counts.get(el.name, 0)
            trials = req.element_trials.get(el.name, observed_games)
            obs_rate = cnt / trials if trials > 0 else 0.0
            observed_rates[el.name] = round(obs_rate, 6)
            theory = {sv: el.probabilities.get(sv, 0.0) for sv in profile.settings}
            closest_s = min(theory, key=lambda sv: abs(theory[sv] - obs_rate))
            avg_theory = sum(theory[sv] * posterior.get(sv, 0.0) for sv in profile.settings)
            direction = "up" if obs_rate > avg_theory else "down"
            element_analysis.append({
                "name": el.name,
                "count": cnt,
                "trials": trials,
                "observed": round(obs_rate, 6),
                "observed_per_n": round(1 / obs_rate, 1) if obs_rate > 0 else None,
                "theoretical": {sv: round(v, 6) for sv, v in theory.items()},
                "closest_setting": closest_s,
                "direction": direction,
            })

    # サンプル量: 入力した要素のうち、最も観測が育った要素を基準にする。
    # 未入力の超低確率要素（リーチ目等）が他の十分な観測を無効化しないようにする。
    sample_warning = None
    recommended_games = None
    sample_adequacy = 0
    if profile.elements:
        supplied = [el for el in profile.elements if el.name in req.element_counts]
        checked = supplied or list(profile.elements)
        adequacies = []
        game_based_needs = []
        for el in checked:
            max_p = max(el.probabilities.get(sv, 0.01) for sv in profile.settings)
            needed = int(30 / max_p) if max_p > 0 else 10000
            trials = req.element_trials.get(el.name, observed_games)
            adequacies.append(trials / max(1, needed))
            if el.name not in req.element_trials:
                game_based_needs.append(needed)
        sample_adequacy = min(100, round(max(adequacies, default=0) * 100))
        recommended_games = min(game_based_needs) if game_based_needs else None
        if sample_adequacy < 50:
            sample_warning = f"サンプル不足（充足{sample_adequacy}%）"
        elif sample_adequacy < 100:
            sample_warning = f"サンプル充足{sample_adequacy}% — まだ推測のブレが残ります"

    # 信用区間・識別力・相関チェック
    ci_lo, ci_hi = estimator.credible_interval(posterior, prob=0.90)
    powers = {k: round(v, 3) for k, v in estimator.element_discrimination_power().items()}
    correlated = [[a, b, r] for a, b, r in estimator.find_correlated_elements(threshold=0.95)]
    peak_probability = max(posterior.values(), default=0.0)
    # これは理論値が正しい前提での統計モデル内グレード。実戦的中率とは分ける。
    prediction_grade = (
        "統計モデル90%級" if sample_adequacy >= 100 and peak_probability >= 0.90
        and confidence >= 0.75 and not correlated
        else "統計モデル80%級" if sample_adequacy >= 80 and peak_probability >= 0.80
        and confidence >= 0.50 and not correlated
        else "判定材料不足"
    )

    high4 = sum(p for s, p in posterior.items() if int(s) >= 4)
    high5 = sum(p for s, p in posterior.items() if int(s) >= 5)
    setting6 = posterior.get("6", 0.0)
    has_observation = any(name in req.element_counts for name in (el.name for el in profile.elements))
    review_points = sorted({1000, 2000, 3000, 4000, 5000, int(recommended_games or 0)})
    next_review_games = next((point for point in review_points if point > observed_games), None)
    remaining_text = (
        f"あと{next_review_games - observed_games:,}Gで再判定" if next_review_games else "現在のデータで再確認"
    )
    profile_verified = bool(data.get("verified_for_live_setting") and data.get("source_url"))
    if not profile_verified:
        action = "情報不足"
        action_reason = "この機種の設定差データは出典照合が未完了のため、続行・撤退判断には使えません。"
    elif not has_observation or observed_games < 500:
        action = "情報不足"
        action_reason = "設定差のある実戦項目が不足しています。差枚や一時的な当たりだけでは判定しません。"
    elif sample_adequacy < 35:
        action = "様子見"
        action_reason = f"まだブレが大きい段階です。{remaining_text}を目安に、設定差の大きい項目を追加してください。"
    elif high4 >= 0.65 and ev_result.ev >= 1.0:
        action = "続行候補"
        action_reason = f"設定4以上{high4*100:.0f}%・推定機械割{ev_result.ev_pct:.1f}%です。確定ではないため示唆と店内状況も確認してください。"
    elif high4 <= 0.20 and ev_result.ev < 0.99:
        action = "撤退候補"
        action_reason = f"設定4以上{high4*100:.0f}%・推定機械割{ev_result.ev_pct:.1f}%です。少数試行なら即断せず、代替台の有無も含めて判断してください。"
    else:
        action = "様子見"
        action_reason = f"高設定・低設定のどちらにも十分寄っていません。{remaining_text}が次の確認目安です。"

    return EstimateResponse(
        posterior=posterior,
        expected_setting=estimator.expected_setting(posterior),
        high_setting_prob=estimator.high_setting_prob(posterior),
        ev=ev_result.ev,
        ev_pct=ev_result.ev_pct,
        should_retreat=ev_result.should_retreat,
        retreat_reason=ev_result.retreat_reason,
        kw_source=ev_result.kw_source,
        settings=list(profile.settings),
        confidence=round(confidence, 3),
        confidence_label=confidence_label,
        observed_rates=observed_rates,
        element_analysis=element_analysis,
        sample_warning=sample_warning,
        recommended_games=recommended_games,
        credible_interval=[ci_lo, ci_hi],
        element_powers=powers,
        correlated_elements=correlated if correlated else None,
        sample_adequacy_pct=sample_adequacy,
        prediction_grade=prediction_grade,
        observed_games=observed_games,
        high_setting_probabilities={
            "setting4_or_higher": round(high4, 4),
            "setting5_or_higher": round(high5, 4),
            "setting6": round(setting6, 4),
        },
        action=action,
        action_reason=action_reason,
        next_review_games=next_review_games,
        input_notice="差枚・現在の勝ち負けは設定判別要素にしていません。割合系の項目は出現回数と試行回数の両方が必要です。",
        profile_verified=profile_verified,
        profile_source_url=str(data.get("source_url") or ""),
    )


@router.post("/api/setting_change", tags=["estimate"])
def setting_change(req: ChangeDetectRequest) -> dict:
    """前半/後半の2区間カウントから設定変更確率を推定する。"""
    path = _get_machine_path(req.machine_name)
    if not path.exists():
        raise HTTPException(404, f"機種データが見つかりません: {req.machine_name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        profile = MachineProfile.from_dict(data)
    except Exception as e:
        raise HTTPException(422, str(e))

    obs_early = Observation(req.early_games, req.early_counts)
    obs_late  = Observation(req.late_games, req.late_counts)
    try:
        result = detect_setting_change(
            profile, obs_early, obs_late,
            prior=req.prior, change_prior=req.change_prior,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {
        "change_prob": result.change_prob,
        "log_bf": result.log_bf,
        "verdict": result.verdict,
        "early_setting": result.early_setting,
        "late_setting": result.late_setting,
        "combined_setting": result.combined_setting,
        "early_posterior": result.early_posterior,
        "late_posterior": result.late_posterior,
        "combined_posterior": result.combined_posterior,
    }

