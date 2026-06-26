"""
pachi-tool FastAPI バックエンド。

起動:
    uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date
from pathlib import Path
from typing import Optional

import csv
import io

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from config import HALL_REPORTS_DB, MACHINES_DIR as _MACHINES_DIR
except ImportError:
    HALL_REPORTS_DB = Path(__file__).parent.parent / "data" / "hall_reports.db"
    _MACHINES_DIR = None
_scrape_status: dict[str, str] = {}  # hall_name → "idle"|"running"|"done"|"error"

from core.bayes_engine import MachineProfile, Observation, SettingEstimator
from core.setting_change import detect_setting_change
from hall.prior import (
    DAITO_MACHINE_SCORES,
    DAITO_WEEKDAY_AVG,
    compute_prior,
    day_rating,
    machine_ranking,
)
from records.models import (
    Session,
    delete_session,
    get_session,
    init_db,
    list_halls,
    list_sessions,
    save_session,
    session_to_dict,
    update_session,
)
from value.ev import compute_ev

MACHINES_DIR = _MACHINES_DIR or Path(__file__).parent.parent / "data" / "machines"
WEB_DIR = Path(__file__).parent.parent / "web"

# ---------------------------------------------------------------------------
app = FastAPI(title="pachi-tool", version="0.2.0", docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class EstimateRequest(BaseModel):
    machine_name: str
    games_total: int = 0
    started_from: int = 0  # 宵越しなど引き継ぎG数。実観測G数 = games_total - started_from
    element_counts: dict[str, int] = Field(default_factory=dict)
    prior: Optional[dict[str, float]] = None
    hall_name: str = ""
    weekday: Optional[int] = None
    is_event_day: bool = False
    day_of_month: Optional[int] = None
    min_setting: Optional[int] = None  # 確定演出による下限設定 (e.g. 4 → 設4以上確定)
    seat_number: Optional[int] = None  # 台番（同台の過去セッションを事前に反映）


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


class SessionCreate(BaseModel):
    date: str = Field(default_factory=lambda: date.today().isoformat())
    machine_name: str
    hall_name: str = ""
    seat_number: Optional[int] = None
    is_corner: bool = False
    games_total: int = 0
    investment: int = 0
    returns: int = 0
    diff_coins: int = 0
    is_event_day: bool = False
    started_from: int = 0
    element_counts: dict[str, int] = Field(default_factory=dict)
    posterior: Optional[dict[str, float]] = None
    notes: str = ""


class SessionUpdate(BaseModel):
    games_total: Optional[int] = None
    investment: Optional[int] = None
    returns: Optional[int] = None
    diff_coins: Optional[int] = None
    element_counts: Optional[dict[str, int]] = None
    posterior: Optional[dict[str, float]] = None
    notes: Optional[str] = None
    seat_number: Optional[int] = None
    is_corner: Optional[bool] = None
    is_event_day: Optional[bool] = None


# ---------------------------------------------------------------------------
# Machines
# ---------------------------------------------------------------------------

@app.get("/api/machines", tags=["machines"])
def list_machines() -> list[str]:
    """保存済み機種の一覧を返す。"""
    return sorted(
        p.stem for p in MACHINES_DIR.glob("*.json")
        if p.stem and p.stem != ""
    )


@app.get("/api/machines/{machine_name}", tags=["machines"])
def get_machine(machine_name: str) -> dict:
    """機種データ（確率テーブル・機械割）を返す。"""
    path = MACHINES_DIR / f"{machine_name}.json"
    if not path.exists():
        raise HTTPException(404, f"機種が見つかりません: {machine_name}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------

@app.post("/api/estimate", response_model=EstimateResponse, tags=["estimate"])
def estimate(req: EstimateRequest) -> EstimateResponse:
    """
    観測カウントから設定推測を実行する。

    hall_name を渡すと、店傾向データを事前分布に自動反映。
    prior を明示した場合はそちらを優先。
    """
    path = MACHINES_DIR / f"{req.machine_name}.json"
    if not path.exists():
        raise HTTPException(404, f"機種データが見つかりません: {req.machine_name}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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
    observed_games = max(0, req.games_total - req.started_from)
    obs = Observation(total_games=observed_games, counts=req.element_counts)
    estimator = SettingEstimator(profile)

    try:
        posterior = estimator.estimate(obs, prior=prior)
    except ValueError as e:
        raise HTTPException(422, str(e))

    # 確定演出による下限設定制約 (e.g. min_setting=4 → 設1,2,3を0にして再正規化)
    if req.min_setting is not None:
        min_s = str(req.min_setting)
        filtered = {s: p for s, p in posterior.items() if int(s) >= req.min_setting}
        total = sum(filtered.values())
        if total > 1e-12:
            posterior = {s: p / total for s, p in filtered.items()}
        # 設定が全て除外された場合は制約を無視（データ不整合時の安全弁）
        else:
            posterior = estimator.estimate(obs, prior=prior)

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
            obs_rate = cnt / observed_games
            observed_rates[el.name] = round(obs_rate, 6)
            theory = {sv: el.probabilities.get(sv, 0.0) for sv in profile.settings}
            closest_s = min(theory, key=lambda sv: abs(theory[sv] - obs_rate))
            avg_theory = sum(theory[sv] * posterior.get(sv, 0.0) for sv in profile.settings)
            direction = "up" if obs_rate > avg_theory else "down"
            element_analysis.append({
                "name": el.name,
                "observed": round(obs_rate, 6),
                "observed_per_n": round(1 / obs_rate, 1) if obs_rate > 0 else None,
                "theoretical": {sv: round(v, 6) for sv, v in theory.items()},
                "closest_setting": closest_s,
                "direction": direction,
            })

    # ゲーム数不足警告: 最低でも「最も出にくい要素の期待出現数 >= 30」が信頼できるライン
    sample_warning = None
    recommended_games = None
    if profile.elements:
        # 各要素の最高設定確率で期待出現数30回に必要なG数を計算
        max_needed = 0
        for el in profile.elements:
            max_p = max(el.probabilities.get(sv, 0.01) for sv in profile.settings)
            needed = int(30 / max_p) if max_p > 0 else 10000
            max_needed = max(max_needed, needed)
        recommended_games = max_needed
        if observed_games < max_needed:
            pct = int(observed_games / max_needed * 100)
            sample_warning = f"現在{observed_games}G（推奨{max_needed}Gの{pct}%）— サンプル不足のため推測精度が低い可能性があります"
        elif observed_games < max_needed * 0.5:
            sample_warning = f"サンプル不足（{observed_games}G / 推奨{max_needed}G）"

    # 信用区間・識別力・相関チェック
    ci_lo, ci_hi = estimator.credible_interval(posterior, prob=0.90)
    powers = {k: round(v, 3) for k, v in estimator.element_discrimination_power().items()}
    correlated = [[a, b, r] for a, b, r in estimator.find_correlated_elements(threshold=0.95)]

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
    )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@app.post("/api/sessions", tags=["sessions"])
def create_session(body: SessionCreate) -> dict:
    s = Session(
        date=body.date,
        machine_name=body.machine_name,
        hall_name=body.hall_name,
        seat_number=body.seat_number,
        is_corner=body.is_corner,
        games_total=body.games_total,
        investment=body.investment,
        returns=body.returns,
        diff_coins=body.diff_coins,
        is_event_day=body.is_event_day,
        started_from=body.started_from,
        posterior=body.posterior,
        element_counts=body.element_counts,
        notes=body.notes,
    )
    sid = save_session(s)
    return {"id": sid, "message": "保存しました"}


@app.get("/api/sessions", tags=["sessions"])
def get_sessions(
    hall_name: Optional[str] = Query(None),
    machine_name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
) -> list[dict]:
    sessions = list_sessions(
        hall_name=hall_name,
        machine_name=machine_name,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return [session_to_dict(s) for s in sessions]


@app.get("/api/sessions/export", tags=["sessions"])
def export_sessions_csv_route(
    hall_name: Optional[str] = Query(None),
    machine_name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
) -> StreamingResponse:
    """セッション履歴をCSVでエクスポートする。"""
    sessions = list_sessions(
        hall_name=hall_name,
        machine_name=machine_name,
        date_from=date_from,
        date_to=date_to,
        limit=5000,
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "date", "hall_name", "machine_name", "seat_number", "is_corner",
        "games_total", "investment", "returns", "diff_yen", "diff_coins",
        "is_event_day", "started_from", "expected_setting", "high_setting_prob",
        "notes",
    ])
    for s in sessions:
        exp_setting = ""
        high_prob = ""
        if s.posterior:
            exp_setting = f"{sum(int(k)*v for k,v in s.posterior.items()):.2f}"
            high_prob = f"{sum(v for k,v in s.posterior.items() if int(k)>=4)*100:.1f}%"
        writer.writerow([
            s.id, s.date, s.hall_name, s.machine_name,
            s.seat_number or "", int(s.is_corner),
            s.games_total, s.investment, s.returns, s.diff_yen, s.diff_coins,
            int(s.is_event_day), s.started_from,
            exp_setting, high_prob, s.notes,
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sessions.csv"},
    )


class CsvImportBody(BaseModel):
    csv_text: str  # UTF-8 CSVテキスト（BOM可）

@app.post("/api/sessions/import_csv", tags=["sessions"])
def import_sessions_csv(body: CsvImportBody) -> dict:
    """CSVテキストからセッションを一括インポートする。"""
    text = body.csv_text.lstrip("﻿").strip()  # BOM除去
    reader = csv.DictReader(io.StringIO(text))
    imported, skipped = 0, 0
    for row in reader:
        try:
            machine = row.get("machine_name", "").strip()
            if not machine:
                skipped += 1
                continue
            inv = int(row.get("investment") or 0)
            ret = int(row.get("returns") or 0)
            s = Session(
                date=row.get("date", date.today().isoformat()),
                machine_name=machine,
                hall_name=row.get("hall_name", ""),
                seat_number=int(row["seat_number"]) if row.get("seat_number") else None,
                is_corner=row.get("is_corner", "0") in ("1", "True", "true"),
                games_total=int(row.get("games_total") or 0),
                investment=inv,
                returns=ret,
                diff_coins=int(row.get("diff_coins") or 0),
                is_event_day=row.get("is_event_day", "0") in ("1", "True", "true"),
                started_from=int(row.get("started_from") or 0),
                notes=row.get("notes", ""),
            )
            save_session(s)
            imported += 1
        except Exception:
            skipped += 1
    return {"imported": imported, "skipped": skipped}


@app.get("/api/sessions/{session_id}", tags=["sessions"])
def get_session_endpoint(session_id: int) -> dict:
    s = get_session(session_id)
    if not s:
        raise HTTPException(404, "セッションが見つかりません")
    return session_to_dict(s)


@app.put("/api/sessions/{session_id}", tags=["sessions"])
def update_session_endpoint(session_id: int, body: SessionUpdate) -> dict:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "更新内容がありません")
    update_session(session_id, **updates)
    s = get_session(session_id)
    if not s:
        raise HTTPException(404, "セッションが見つかりません")
    return session_to_dict(s)


@app.delete("/api/sessions/{session_id}", tags=["sessions"])
def delete_session_endpoint(session_id: int) -> dict:
    if not delete_session(session_id):
        raise HTTPException(404, "セッションが見つかりません")
    return {"deleted": True}


@app.get("/api/halls", tags=["sessions"])
def get_halls() -> list[str]:
    return list_halls()


# ---------------------------------------------------------------------------
# Hall analysis
# ---------------------------------------------------------------------------

@app.get("/api/hall/prior", tags=["hall"])
def get_prior(
    hall_name: str = Query(...),
    machine_name: str = Query(""),
    weekday: Optional[int] = Query(None),
    is_event_day: bool = Query(False),
    day_of_month: Optional[int] = Query(None),
) -> dict[str, float]:
    """指定条件の事前分布を返す。"""
    return compute_prior(
        hall_name=hall_name,
        machine_name=machine_name,
        weekday=weekday,
        is_event_day=is_event_day,
        day_of_month=day_of_month,
    )


@app.get("/api/hall/daito", tags=["hall"])
def get_daito_analysis() -> dict:
    """ベガスベガス大東店の分析データ（機種スコア・曜日・特定日）を返す。"""
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    return {
        "machine_scores": [
            {"machine": k, "score": v[0], "appearances": v[1],
             "avg": round(v[0] / v[1], 2)}
            for k, v in sorted(DAITO_MACHINE_SCORES.items(), key=lambda x: -x[1][0])
        ],
        "weekday_scores": [
            {"day": weekday_names[d], "day_index": d, "avg_score": s}
            for d, s in sorted(DAITO_WEEKDAY_AVG.items())
        ],
        "special_days": {
            "5のつく日": {"avg_score": 1.87, "sample_days": 19, "vs_normal": +0.03},
            "8のつく日": {"avg_score": 1.62, "sample_days": 18, "vs_normal": -0.22},
            "通常日":    {"avg_score": 1.84, "sample_days": 124, "vs_normal": 0.0},
        },
    }


@app.get("/api/hall/day_rating", tags=["hall"])
def get_day_rating(
    hall_name: str = Query(...),
    weekday: int = Query(..., ge=0, le=6),
) -> dict:
    return day_rating(hall_name, weekday)


@app.get("/api/hall/machine_ranking", tags=["hall"])
def get_machine_ranking(hall_name: str = Query(...)) -> list[dict]:
    return machine_ranking(hall_name)


@app.get("/api/hall/weekday_machine_stats", tags=["hall"])
def get_weekday_machine_stats(
    hall_name: str = Query(...),
    days: int = Query(90),
) -> list[dict]:
    """曜日×機種のクロス集計（どの曜日にどの機種が強いか）"""
    conn = _get_reports_conn()
    if not conn:
        return []
    rows = conn.execute(
        """SELECT strftime('%w', report_date) as dow,
                  machine_name,
                  COUNT(*) as cnt,
                  ROUND(AVG(diff_coins)) as avg_diff,
                  ROUND(AVG(CASE WHEN diff_coins > 0 THEN 1.0 ELSE 0.0 END)*100) as win_rate
           FROM hall_day_seat
           WHERE hall_name=? AND machine_name NOT LIKE '末尾%'
             AND machine_name != '_NODATA_' AND machine_name NOT LIKE '%データ%'
             AND (bb_prob IS NOT NULL OR ev_pct IS NOT NULL)
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY dow, machine_name
           HAVING cnt >= 3
           ORDER BY dow, avg_diff DESC""",
        (hall_name, days)
    ).fetchall()
    conn.close()
    dow_map = {"0":"日","1":"月","2":"火","3":"水","4":"木","5":"金","6":"土"}
    return [
        {"weekday": dow_map.get(r[0], r[0]), "machine_name": r[1],
         "count": r[2], "avg_diff": r[3] or 0, "win_rate": r[4] or 0}
        for r in rows
    ]


@app.get("/api/hall/today_machine_ranking", tags=["hall"])
def get_today_machine_ranking(
    hall_name: str = Query(...),
    days: int = Query(120),
) -> list[dict]:
    """
    本日の曜日に絞った機種別成績ランキング。
    過去の同曜日データのみで集計し、「今日どの機種が強いか」を提示する。
    """
    import datetime
    today_dow = datetime.date.today().weekday()  # 0=月 … 6=日
    sqlite_dow = str((today_dow + 1) % 7)        # SQLite は 0=日 … 6=土

    conn = _get_reports_conn()
    if not conn:
        return []
    rows = conn.execute(
        """SELECT machine_name,
                  COUNT(*) as cnt,
                  ROUND(AVG(diff_coins)) as avg_diff,
                  ROUND(AVG(CASE WHEN diff_coins > 0 THEN 1.0 ELSE 0.0 END)*100) as win_rate,
                  MAX(report_date) as last_date,
                  COUNT(DISTINCT seat_number) as unit_cnt
           FROM hall_day_seat
           WHERE hall_name=? AND strftime('%w', report_date)=?
             AND machine_name NOT LIKE '末尾%'
             AND machine_name != '_NODATA_'
             AND machine_name NOT LIKE '%データ%'
             AND (bb_prob IS NOT NULL OR ev_pct IS NOT NULL)
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY machine_name
           HAVING cnt >= 3
           ORDER BY avg_diff DESC
           LIMIT 10""",
        (hall_name, sqlite_dow, days)
    ).fetchall()
    conn.close()

    dow_ja = ["月","火","水","木","金","土","日"]
    return [
        {
            "machine_name": r[0],
            "count": r[1],
            "avg_diff": int(r[2] or 0),
            "win_rate": float(r[3] or 0),
            "last_date": r[4],
            "unit_cnt": r[5],
            "weekday_ja": dow_ja[today_dow],
        }
        for r in rows
    ]


@app.get("/api/hall/stats", tags=["hall"])
def get_hall_stats(
    hall_name: str = Query(...),
    machine_name: Optional[str] = Query(None),
) -> dict:
    """収支サマリーと機種別成績を返す。"""
    sessions = list_sessions(hall_name=hall_name, machine_name=machine_name, limit=500)
    if not sessions:
        return {"total_sessions": 0}

    total_inv = sum(s.investment for s in sessions)
    total_ret = sum(s.returns for s in sessions)
    total_games = sum(s.games_total for s in sessions)
    wins = sum(1 for s in sessions if s.diff_yen > 0)

    machine_stats: dict[str, dict] = {}
    for s in sessions:
        m = s.machine_name
        if m not in machine_stats:
            machine_stats[m] = {"count": 0, "total_diff_yen": 0, "total_games": 0, "wins": 0}
        machine_stats[m]["count"] += 1
        machine_stats[m]["total_diff_yen"] += s.diff_yen
        machine_stats[m]["total_games"] += s.games_total
        if s.diff_yen > 0:
            machine_stats[m]["wins"] += 1

    return {
        "total_sessions": len(sessions),
        "total_investment": total_inv,
        "total_returns": total_ret,
        "diff_yen": total_ret - total_inv,
        "total_games": total_games,
        "win_rate": round(wins / len(sessions), 3) if sessions else 0,
        "machine_stats": machine_stats,
    }


@app.get("/api/machine/stats", tags=["machine"])
def get_machine_stats(machine_name: str = Query(...)) -> dict:
    """特定機種の個人統計を返す。"""
    sessions = list_sessions(machine_name=machine_name, limit=500)
    if not sessions:
        return {"total_sessions": 0, "machine_name": machine_name}

    total_inv = sum(s.investment for s in sessions)
    total_ret = sum(s.returns for s in sessions)
    total_games = sum(s.games_total for s in sessions)
    wins = sum(1 for s in sessions if s.diff_yen > 0)
    diff = total_ret - total_inv

    # 推測設定平均 (posteriorから期待値を計算)
    def _expected_setting(s) -> Optional[float]:
        d = session_to_dict(s)
        post = d.get("posterior") or {}
        if not post:
            return None
        try:
            return sum(int(k) * v for k, v in post.items())
        except Exception:
            return None

    est_vals = [v for s in sessions for v in [_expected_setting(s)] if v is not None]
    avg_est = round(sum(est_vals) / len(est_vals), 2) if est_vals else None

    # 最近5セッション
    recent = sorted(sessions, key=lambda s: s.date, reverse=True)[:5]

    return {
        "machine_name": machine_name,
        "total_sessions": len(sessions),
        "total_investment": total_inv,
        "total_returns": total_ret,
        "diff_yen": diff,
        "total_games": total_games,
        "win_rate": round(wins / len(sessions), 3),
        "avg_estimated_setting": avg_est,
        "recent_sessions": [session_to_dict(s) for s in recent],
    }


# ---------------------------------------------------------------------------
# Setting change detection
# ---------------------------------------------------------------------------

class ChangeDetectRequest(BaseModel):
    machine_name: str
    early_games: int
    late_games: int
    early_counts: dict[str, int] = Field(default_factory=dict)
    late_counts: dict[str, int] = Field(default_factory=dict)
    prior: Optional[dict[str, float]] = None
    change_prior: float = 0.10


@app.post("/api/setting_change", tags=["estimate"])
def setting_change(req: ChangeDetectRequest) -> dict:
    """前半/後半の2区間カウントから設定変更確率を推定する。"""
    path = MACHINES_DIR / f"{req.machine_name}.json"
    if not path.exists():
        raise HTTPException(404, f"機種データが見つかりません: {req.machine_name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        profile = MachineProfile.from_dict(data)
    except Exception as e:
        raise HTTPException(422, str(e))

    obs_early = Observation(req.early_games, req.early_counts)
    obs_late  = Observation(req.late_games, req.late_counts)
    result = detect_setting_change(
        profile, obs_early, obs_late,
        prior=req.prior, change_prior=req.change_prior,
    )
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


# ---------------------------------------------------------------------------
# Hall scraper endpoints
# ---------------------------------------------------------------------------

def _get_reports_conn() -> Optional[sqlite3.Connection]:
    if not HALL_REPORTS_DB.exists():
        return None
    conn = sqlite3.connect(HALL_REPORTS_DB)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/api/hall/report_dates", tags=["hall"])
def get_report_dates(hall_name: str = Query(...)) -> list[str]:
    """スクレイプ済みレポートの日付一覧を返す（新しい順）。"""
    conn = _get_reports_conn()
    if conn is None:
        return []
    rows = conn.execute(
        "SELECT DISTINCT report_date FROM hall_day_machine WHERE hall_name=? ORDER BY report_date DESC",
        (hall_name,)
    ).fetchall()
    conn.close()
    return [r["report_date"] for r in rows]


@app.get("/api/hall/report", tags=["hall"])
def get_hall_report(
    hall_name: str = Query(...),
    report_date: str = Query(...),
    limit: int = Query(50, le=200),
) -> list[dict]:
    """指定日の機種別スクレイプデータを返す（差枚降順）。"""
    conn = _get_reports_conn()
    if conn is None:
        raise HTTPException(404, "レポートDBが未作成です。先にスクレイプを実行してください。")
    rows = conn.execute(
        """SELECT machine_name, unit_count, avg_diff_coins, avg_games,
                  win_rate_pct, ev_pct, source_url
           FROM hall_day_machine
           WHERE hall_name=? AND report_date=?
           ORDER BY avg_diff_coins DESC NULLS LAST
           LIMIT ?""",
        (hall_name, report_date, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/hall/machine_trend", tags=["hall"])
def get_machine_trend(
    hall_name: str = Query(...),
    machine_name: str = Query(...),
    days: int = Query(30, le=90),
) -> list[dict]:
    """特定機種の過去N日の差枚トレンドを返す。"""
    conn = _get_reports_conn()
    if conn is None:
        return []
    rows = conn.execute(
        """SELECT report_date, avg_diff_coins, avg_games, win_rate_pct, ev_pct
           FROM hall_day_machine
           WHERE hall_name=? AND machine_name=?
           ORDER BY report_date DESC
           LIMIT ?""",
        (hall_name, machine_name, days)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/hall/top_machines", tags=["hall"])
def get_top_machines(
    hall_name: str = Query(...),
    days: int = Query(30, le=90),
    limit: int = Query(20, le=100),
) -> list[dict]:
    """過去N日の平均差枚ランキングを返す（累積平均）。"""
    conn = _get_reports_conn()
    if conn is None:
        return []
    rows = conn.execute(
        """SELECT machine_name,
                  COUNT(*) AS report_count,
                  ROUND(AVG(avg_diff_coins), 0) AS avg_diff,
                  ROUND(AVG(ev_pct), 1) AS avg_ev,
                  ROUND(AVG(win_rate_pct), 1) AS avg_win_rate
           FROM hall_day_machine
           WHERE hall_name=?
             AND report_date >= date('now', ? || ' days')
             AND avg_diff_coins IS NOT NULL
           GROUP BY machine_name
           HAVING COUNT(*) >= 3
           ORDER BY avg_diff DESC
           LIMIT ?""",
        (hall_name, f"-{days}", limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _run_scrape(hall_name: str, days: int):
    """バックグラウンドスクレイプ処理。"""
    global _scrape_status
    _scrape_status[hall_name] = "running"
    try:
        from scraper.minrepo import (
            build_tag_url, fetch_report_links,
            init_db, parse_date_from_text, scrape_report
        )
        conn = init_db()
        tag_url = build_tag_url(hall_name)
        links = fetch_report_links(tag_url, max_pages=3)
        year = date.today().year
        for date_text, report_url in links[:days]:
            date_str = parse_date_from_text(date_text, year)
            if not date_str:
                continue
            existing = conn.execute(
                "SELECT COUNT(*) FROM hall_day_machine WHERE hall_name=? AND report_date=?",
                (hall_name, date_str)
            ).fetchone()[0]
            if existing > 0:
                continue
            scrape_report(report_url, hall_name, date_str, conn)
            import time; time.sleep(1.5)
        conn.close()
        _scrape_status[hall_name] = "done"
    except Exception as e:
        _scrape_status[hall_name] = f"error: {e}"


@app.post("/api/hall/scrape", tags=["hall"])
def trigger_scrape(
    background_tasks: BackgroundTasks,
    hall_name: str = Query(...),
    days: int = Query(30, le=90),
) -> dict:
    """ホールデータのスクレイプをバックグラウンドで開始する。"""
    if _scrape_status.get(hall_name) == "running":
        return {"status": "running", "message": "すでにスクレイプ中です"}
    background_tasks.add_task(_run_scrape, hall_name, days)
    _scrape_status[hall_name] = "running"
    return {"status": "started", "message": f"{hall_name} のスクレイプを開始しました"}


@app.get("/api/hall/scrape_status", tags=["hall"])
def get_scrape_status(hall_name: str = Query(...)) -> dict:
    """スクレイプ状況を返す。"""
    conn = _get_reports_conn()
    count = 0
    latest_date = ""
    if conn:
        row = conn.execute(
            """SELECT COUNT(DISTINCT report_date) AS cnt, MAX(report_date) AS latest
               FROM hall_day_machine WHERE hall_name=?""",
            (hall_name,)
        ).fetchone()
        if row:
            count = row["cnt"] or 0
            latest_date = row["latest"] or ""
        conn.close()
    return {
        "status": _scrape_status.get(hall_name, "idle"),
        "scraped_days": count,
        "latest_date": latest_date,
    }


# ---------------------------------------------------------------------------
# アナスロ 台番別データ
# ---------------------------------------------------------------------------

_anaslo_scrape_status: dict[str, str] = {}


def _run_anaslo_scrape(hall_name: str, days: int):
    _anaslo_scrape_status[hall_name] = "running"
    try:
        from scraper.anaslo import scrape_hall as anaslo_scrape
        anaslo_scrape(hall_name, days=days)
        _anaslo_scrape_status[hall_name] = "done"
    except Exception as e:
        _anaslo_scrape_status[hall_name] = f"error: {e}"


@app.post("/api/hall/anaslo_scrape", tags=["hall"])
def trigger_anaslo_scrape(
    hall_name: str = Query(...),
    days: int = Query(30),
    background_tasks: BackgroundTasks = None,
):
    if _anaslo_scrape_status.get(hall_name) == "running":
        return {"status": "already_running"}
    background_tasks.add_task(_run_anaslo_scrape, hall_name, days)
    return {"status": "started"}


@app.get("/api/hall/anaslo_status", tags=["hall"])
def get_anaslo_status(hall_name: str = Query(...)) -> dict:
    conn = _get_reports_conn()
    count, latest = 0, ""
    if conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT report_date), MAX(report_date) FROM hall_day_seat WHERE hall_name=? AND bb_prob IS NOT NULL",
            (hall_name,)
        ).fetchone()
        if row:
            count = row[0] or 0
            latest = row[1] or ""
        conn.close()
    return {
        "status": _anaslo_scrape_status.get(hall_name, "idle"),
        "scraped_days": count,
        "latest_date": latest,
    }


@app.get("/api/hall/seat_dates", tags=["hall"])
def get_seat_dates(hall_name: str = Query(...)) -> list[str]:
    conn = _get_reports_conn()
    if not conn:
        return []
    rows = conn.execute(
        "SELECT DISTINCT report_date FROM hall_day_seat WHERE hall_name=? AND bb_prob IS NOT NULL ORDER BY report_date DESC LIMIT 60",
        (hall_name,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


@app.get("/api/hall/seat_report", tags=["hall"])
def get_seat_report(
    hall_name: str = Query(...),
    date: str = Query(...),
    machine_name: Optional[str] = Query(None),
    limit: int = Query(100),
) -> list[dict]:
    """指定日の台番別データ（差枚降順）"""
    conn = _get_reports_conn()
    if not conn:
        return []

    if machine_name:
        rows = conn.execute(
            """SELECT seat_number, machine_name, diff_coins, games, bb_prob, rb_prob, ev_pct
               FROM hall_day_seat
               WHERE hall_name=? AND report_date=? AND machine_name=? AND bb_prob IS NOT NULL
               ORDER BY diff_coins DESC LIMIT ?""",
            (hall_name, date, machine_name, limit)
        ).fetchall()
    else:
        # 全データ一覧（末尾・機種別行を除く）
        rows = conn.execute(
            """SELECT seat_number, machine_name, diff_coins, games, bb_prob, rb_prob, ev_pct
               FROM hall_day_seat
               WHERE hall_name=? AND report_date=? AND bb_prob IS NOT NULL
                 AND machine_name NOT LIKE '末尾%' AND machine_name != '全データ一覧'
               GROUP BY seat_number
               ORDER BY diff_coins DESC LIMIT ?""",
            (hall_name, date, limit)
        ).fetchall()

    conn.close()
    return [
        {
            "seat_number": r[0],
            "machine_name": r[1],
            "diff_coins": r[2],
            "games": r[3],
            "bb_count": r[4],
            "rb_count": r[5],
            "ev_pct": r[6],
        }
        for r in rows
    ]


@app.get("/api/hall/tail_analysis", tags=["hall"])
def get_tail_analysis(
    hall_name: str = Query(...),
    days: int = Query(30),
) -> list[dict]:
    """末尾別の平均差枚分析"""
    conn = _get_reports_conn()
    if not conn:
        return []
    rows = conn.execute(
        """SELECT machine_name AS tail, COUNT(*) AS cnt,
                  AVG(diff_coins) AS avg_diff, SUM(CASE WHEN diff_coins > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS win_rate
           FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL AND machine_name LIKE '末尾%'
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY machine_name HAVING cnt >= 5
           ORDER BY avg_diff DESC""",
        (hall_name, days)
    ).fetchall()
    conn.close()
    return [
        {"tail": r[0], "count": r[1], "avg_diff": round(r[2] or 0), "win_rate": round(r[3] or 0, 1)}
        for r in rows
    ]


@app.get("/api/hall/seat_bb_ranking", tags=["hall"])
def get_seat_bb_ranking(
    hall_name: str = Query(...),
    machine_name: str = Query(...),
    days: int = Query(60),
) -> list[dict]:
    """
    同一機種内での台番別BB/RB確率ランキング。
    同機種の平均BB確率との差（z-score）で「この台は高設定が多い」かを判定。
    設定判別の根拠となる最強シグナル。
    """
    conn = _get_reports_conn()
    if not conn:
        return []

    rows = conn.execute(
        """SELECT seat_number,
                  COUNT(*) as cnt,
                  AVG(bb_prob) as avg_bb,
                  AVG(rb_prob) as avg_rb,
                  AVG(diff_coins) as avg_diff,
                  MAX(report_date) as last_date,
                  AVG(CASE WHEN strftime('%w',report_date)=? THEN bb_prob END) as dow_bb
           FROM hall_day_seat
           WHERE hall_name=? AND machine_name=? AND bb_prob IS NOT NULL
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY seat_number
           HAVING cnt >= 3
           ORDER BY avg_bb DESC""",
        (str((date.today().weekday()+1) % 7), hall_name, machine_name, days)
    ).fetchall()
    conn.close()

    if not rows:
        return []

    # 機種内平均・標準偏差を計算してz-score
    import statistics as _stats
    bbs = [r[2] for r in rows if r[2] is not None]
    if len(bbs) < 2:
        return []
    mean_bb = _stats.mean(bbs)
    std_bb = _stats.stdev(bbs) if len(bbs) > 1 else 0.001

    result = []
    for r in rows:
        seat, cnt, avg_bb, avg_rb, avg_diff, last_date, dow_bb = r
        if avg_bb is None:
            continue
        z = (avg_bb - mean_bb) / max(std_bb, 0.00001)
        result.append({
            "seat_number": seat,
            "cnt": cnt,
            "avg_bb": round(avg_bb * 100, 4),
            "avg_rb": round((avg_rb or 0) * 100, 4),
            "avg_diff": int(avg_diff or 0),
            "last_date": last_date,
            "dow_bb": round(dow_bb * 100, 4) if dow_bb else None,
            "z_score": round(z, 2),
        })

    result.sort(key=lambda x: -x["z_score"])
    return result


@app.get("/api/hall/seat_detail", tags=["hall"])
def get_seat_detail(
    hall_name: str = Query(...),
    machine_name: str = Query(...),
    seat_number: int = Query(...),
    days: int = Query(90),
) -> dict:
    """特定台番の詳細: 日別履歴・曜日別実績・直近トレンド"""
    conn = _get_reports_conn()
    if not conn:
        return {}

    # 日別履歴
    history = conn.execute(
        """SELECT report_date, diff_coins, games, bb_prob, rb_prob
           FROM hall_day_seat
           WHERE hall_name=? AND machine_name=? AND seat_number=?
             AND report_date >= date('now', '-' || ? || ' days')
           ORDER BY report_date DESC""",
        (hall_name, machine_name, seat_number, days)
    ).fetchall()

    # 曜日別集計 (SQLite %w: 0=日,1=月,...,6=土)
    weekday_rows = conn.execute(
        """SELECT strftime('%w', report_date) as dow,
                  COUNT(*) as cnt,
                  ROUND(AVG(diff_coins)) as avg_diff,
                  ROUND(AVG(CASE WHEN diff_coins > 0 THEN 1.0 ELSE 0.0 END)*100) as win_rate
           FROM hall_day_seat
           WHERE hall_name=? AND machine_name=? AND seat_number=?
           GROUP BY dow ORDER BY dow""",
        (hall_name, machine_name, seat_number)
    ).fetchall()
    conn.close()

    dow_map = {"0":"日","1":"月","2":"火","3":"水","4":"木","5":"金","6":"土"}
    weekday_stats = [
        {"weekday": dow_map.get(r[0], r[0]), "count": r[1],
         "avg_diff": r[2] or 0, "win_rate": r[3] or 0}
        for r in weekday_rows
    ]

    hist_list = [
        {"date": r[0], "diff": r[1], "games": r[2],
         "bb_prob": r[3], "rb_prob": r[4]}
        for r in history
    ]

    if not hist_list:
        return {"machine_name": machine_name, "seat_number": seat_number, "history": []}

    diffs = [h["diff"] for h in hist_list]
    avg = round(sum(diffs) / len(diffs))
    win_rate = round(sum(1 for d in diffs if d > 0) / len(diffs) * 100, 1)
    best = max(diffs)
    worst = min(diffs)

    import math as _math
    variance = sum((d - avg)**2 for d in diffs) / len(diffs)
    std = round(_math.sqrt(variance))

    return {
        "machine_name": machine_name,
        "seat_number": seat_number,
        "total_days": len(hist_list),
        "avg_diff": avg,
        "win_rate": win_rate,
        "best": best,
        "worst": worst,
        "std": std,
        "history": hist_list[:60],
        "weekday_stats": weekday_stats,
    }


@app.get("/api/hall/machine_seat_ranking", tags=["hall"])
def get_machine_seat_ranking(
    hall_name: str = Query(...),
    machine_name: str = Query(...),
    days: int = Query(30),
) -> list[dict]:
    """特定機種の全台番ランキング（複合スコア付き）"""
    import datetime, math as _math
    today = datetime.date.today()
    sql_dow = str((today.weekday() + 1) % 7)

    conn = _get_reports_conn()
    if not conn:
        return []
    rows = conn.execute(
        """SELECT seat_number,
                  COUNT(*) as total_days,
                  ROUND(AVG(diff_coins)) as avg_diff,
                  ROUND(AVG(diff_coins*diff_coins) - AVG(diff_coins)*AVG(diff_coins)) as variance,
                  SUM(CASE WHEN diff_coins > 0 THEN 1 ELSE 0 END)*100.0/COUNT(*) as win_rate,
                  ROUND(AVG(CASE WHEN strftime('%w',report_date)=? THEN diff_coins END)) as avg_dow,
                  COUNT(CASE WHEN strftime('%w',report_date)=? THEN 1 END) as cnt_dow,
                  ROUND(AVG(CASE WHEN report_date >= date('now','-7 days') THEN diff_coins END)) as avg_7d
           FROM hall_day_seat
           WHERE hall_name=? AND machine_name=? AND (bb_prob IS NOT NULL OR ev_pct IS NOT NULL)
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY seat_number
           HAVING total_days >= 2
           ORDER BY avg_diff DESC""",
        (sql_dow, sql_dow, hall_name, machine_name, days)
    ).fetchall()
    conn.close()

    result = []
    for r in rows:
        avg = r[2] or 0
        var = max(r[3] or 0, 0)
        std = _math.sqrt(var)
        stability = max(0.0, 1.0 - std / (abs(avg) + 1500)) if avg > 0 else 0.0
        avg_dow = r[5] if (r[5] is not None and r[6] >= 1) else avg
        avg_7d  = r[7] if r[7] is not None else avg
        trend   = avg_7d - avg
        score   = avg * 0.40 + avg_dow * 0.25 + avg * stability * 0.20 + trend * 0.15
        result.append({
            "seat_number": r[0],
            "days": r[1],
            "avg_diff": int(avg),
            "win_rate": round(r[4] or 0, 1),
            "avg_same_dow": int(avg_dow),
            "avg_7d": int(avg_7d) if r[7] is not None else None,
            "stability": round(stability, 2),
            "score": round(score, 1),
        })
    result.sort(key=lambda x: -x["score"])
    return result


@app.get("/api/hall/today_targets", tags=["hall"])
def get_today_targets(
    hall_name: str = Query(...),
    days: int = Query(30),
) -> dict:
    """今日の狙い台TOP3 — 曜日傾向・安定性・直近トレンドを複合スコアで統合"""
    import datetime, math as _math
    today = datetime.date.today()
    weekday = today.weekday()  # 0=月 ... 6=日
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    today_name = weekday_names[weekday]
    # SQLiteの曜日: 0=日,1=月...6=土  ← Python weekday 0=月に変換
    sql_dow = (weekday + 1) % 7  # Python月→SQL月=1

    conn = _get_reports_conn()
    if not conn:
        return {"seats": [], "best_tail": None, "best_machine": None, "today_weekday": today_name}

    # 全台の過去stats（avg・分散・勝率・直近7日）bb_prob OR ev_pct があるデータを対象
    seat_rows = conn.execute(
        """SELECT machine_name, seat_number,
                  COUNT(*) as total_days,
                  ROUND(AVG(diff_coins)) as avg_diff,
                  ROUND(AVG(diff_coins*diff_coins) - AVG(diff_coins)*AVG(diff_coins)) as variance,
                  SUM(CASE WHEN diff_coins > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate,
                  ROUND(AVG(CASE WHEN report_date >= date('now','-7 days') THEN diff_coins END)) as avg_7d,
                  COUNT(CASE WHEN report_date >= date('now','-7 days') THEN 1 END) as cnt_7d,
                  ROUND(AVG(CASE WHEN strftime('%w',report_date)=? THEN diff_coins END)) as avg_same_dow,
                  COUNT(CASE WHEN strftime('%w',report_date)=? THEN 1 END) as cnt_same_dow
           FROM hall_day_seat
           WHERE hall_name=? AND machine_name NOT LIKE '末尾%'
             AND machine_name != '_NODATA_' AND machine_name NOT LIKE '%データ%'
             AND (bb_prob IS NOT NULL OR ev_pct IS NOT NULL)
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY machine_name, seat_number
           HAVING total_days >= 3""",
        (str(sql_dow), str(sql_dow), hall_name, days)
    ).fetchall()

    # 最も好調な末尾（曜日重み付き）
    tail_rows = conn.execute(
        """SELECT machine_name AS tail,
                  ROUND(AVG(diff_coins)) as avg_diff,
                  ROUND(AVG(CASE WHEN strftime('%w',report_date)=? THEN diff_coins END)) as avg_dow
           FROM hall_day_seat
           WHERE hall_name=? AND machine_name LIKE '末尾%'
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY machine_name HAVING COUNT(*) >= 3
           ORDER BY avg_diff DESC LIMIT 3""",
        (str(sql_dow), hall_name, days)
    ).fetchall()

    # 最も好調な機種（5台以上データあり）
    machine_rows = conn.execute(
        """SELECT machine_name,
                  ROUND(AVG(diff_coins)) as avg_diff,
                  COUNT(DISTINCT seat_number) as unit_cnt,
                  SUM(CASE WHEN diff_coins > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate
           FROM hall_day_seat
           WHERE hall_name=? AND machine_name NOT LIKE '末尾%'
             AND machine_name != '_NODATA_' AND machine_name NOT LIKE '%データ%'
             AND bb_prob IS NOT NULL
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY machine_name HAVING COUNT(*) >= 5
           ORDER BY avg_diff DESC LIMIT 1""",
        (hall_name, days)
    ).fetchall()

    conn.close()

    # ── 複合スコアリング ──────────────────────────────────────────────
    # score = avg_diff(40%) + 同曜日avg(25%) + 安定性(20%) + 直近トレンド(15%)
    scored = []
    for r in seat_rows:
        (machine, seat, total_days, avg_diff, variance,
         win_rate, avg_7d, cnt_7d, avg_same_dow, cnt_same_dow) = r

        avg_diff      = avg_diff or 0
        variance      = max(variance or 0, 0)
        avg_7d        = avg_7d if avg_7d is not None else avg_diff
        avg_same_dow  = avg_same_dow if (avg_same_dow is not None and cnt_same_dow >= 1) else avg_diff
        win_rate      = win_rate or 0

        # 安定性: 標準偏差が小さいほど高スコア（平均差枚に対する比率）
        std = _math.sqrt(variance)
        stability = max(0.0, 1.0 - std / (abs(avg_diff) + 1500)) if avg_diff > 0 else 0.0

        # 直近トレンド: 7日平均 vs 全期間平均の乖離（上振れ中か）
        trend = avg_7d - avg_diff if cnt_7d >= 2 else 0.0

        # 正規化スコア（差枚ベース、同曜日・安定性・トレンドで補正）
        score = (
            avg_diff      * 0.40 +
            avg_same_dow  * 0.25 +
            avg_diff * stability * 0.20 +
            trend         * 0.15
        )

        scored.append({
            "machine_name": machine,
            "seat_number": seat,
            "days": total_days,
            "avg_diff": int(avg_diff),
            "win_rate": round(win_rate, 1),
            "avg_same_dow": int(avg_same_dow),
            "avg_7d": int(avg_7d) if cnt_7d >= 1 else None,
            "stability": round(stability, 2),
            "score": round(score, 1),
        })

    scored.sort(key=lambda x: -x["score"])
    seats = scored[:3]

    # 末尾: 同曜日avg優先
    best_tail = None
    if tail_rows:
        best = max(tail_rows, key=lambda r: (r[2] or r[1]) )
        best_tail = best[0]

    best_machine = machine_rows[0][0] if machine_rows else None

    return {
        "seats": seats,
        "best_tail": best_tail,
        "best_machine": best_machine,
        "today_weekday": today_name,
        "data_days": days,
    }


@app.get("/api/hall/machine_setting_tendency", tags=["hall"])
def get_machine_setting_tendency(
    hall_name: str = Query(...),
    days: int = Query(60),
) -> list[dict]:
    """
    機種ごとの設定傾向を推定して返す。
    hall/prior.py の _estimate_prior_from_anaslo を全機種に適用。
    """
    conn = _get_reports_conn()
    if not conn:
        return []
    # データのある機種を取得
    machine_rows = conn.execute(
        """SELECT machine_name, COUNT(*) as records,
                  ROUND(AVG(diff_coins)) as avg_diff,
                  ROUND(AVG(bb_prob)*100, 4) as avg_bb_pct,
                  ROUND(AVG(rb_prob)*100, 4) as avg_rb_pct,
                  COUNT(DISTINCT seat_number) as unit_cnt
           FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL
             AND machine_name NOT LIKE '末尾%'
             AND machine_name != '_NODATA_'
             AND machine_name NOT LIKE '%データ%'
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY machine_name
           HAVING records >= 5
           ORDER BY records DESC""",
        (hall_name, days)
    ).fetchall()

    # トレンド計算: 直近14日 vs 前14-28日の平均差枚比較
    trend_rows = conn.execute(
        """SELECT machine_name,
                  ROUND(AVG(CASE WHEN report_date >= date('now','-14 days') THEN diff_coins END)) as recent,
                  ROUND(AVG(CASE WHEN report_date < date('now','-14 days')
                                 AND report_date >= date('now','-28 days') THEN diff_coins END)) as prev
           FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
             AND machine_name NOT LIKE '%データ%'
             AND report_date >= date('now','-28 days')
           GROUP BY machine_name""",
        (hall_name,)
    ).fetchall()
    conn.close()

    trend_map: dict[str, float] = {}
    for tr in trend_rows:
        if tr[1] is not None and tr[2] is not None:
            trend_map[tr[0]] = round(float(tr[1]) - float(tr[2]))

    from hall.prior import _estimate_prior_from_anaslo, _load_machine_theory
    import datetime
    today_weekday = datetime.date.today().weekday()

    result = []
    for row in machine_rows:
        machine_name = row[0]
        records = row[1]
        avg_diff = row[2] or 0
        avg_bb_pct = row[3]
        avg_rb_pct = row[4]
        unit_cnt = row[5]

        settings = ["1","2","3","4","5","6"]
        prior = _estimate_prior_from_anaslo(hall_name, machine_name, settings, today_weekday)
        theory = _load_machine_theory(machine_name)

        # 推定設定分布があれば期待設定を計算
        est_setting = None
        high_prob = None
        if prior:
            est_setting = round(sum(int(s)*p for s,p in prior.items()), 2)
            high_prob = round(sum(p for s,p in prior.items() if int(s) >= 4), 3)

        # 理論値との比較（BB確率）
        theory_bb_range = None
        if theory:
            bb_el = next((e for e in theory.get("elements",[]) if any(k in e["name"] for k in ["BB","BIG","ボーナス合算","AT初当"])), None)
            if bb_el and avg_bb_pct:
                p_by_s = bb_el.get("p", {})
                lo = min(p_by_s.values()) * 100 if p_by_s else None
                hi = max(p_by_s.values()) * 100 if p_by_s else None
                theory_bb_range = [round(lo, 4), round(hi, 4)] if lo and hi else None

        trend_delta = trend_map.get(machine_name)

        result.append({
            "machine_name": machine_name,
            "records": records,
            "unit_cnt": unit_cnt,
            "avg_diff": int(avg_diff),
            "avg_bb_pct": float(avg_bb_pct or 0),
            "avg_rb_pct": float(avg_rb_pct or 0),
            "setting_dist": prior or {},
            "est_setting": est_setting,
            "high_setting_prob": high_prob,
            "theory_bb_range": theory_bb_range,
            "trend_delta": int(trend_delta) if trend_delta is not None else None,
        })

    # 推定設定（高いほど高設定店の証拠）でソート
    result.sort(key=lambda x: -(x["est_setting"] or 0))
    return result


# ---------------------------------------------------------------------------
# マップ
# ---------------------------------------------------------------------------

import urllib.parse, urllib.request, time as _time

_COORDS_FILE = Path(__file__).parent.parent / "data" / "hall_coords.json"


def _load_coords() -> dict:
    if _COORDS_FILE.exists():
        return json.loads(_COORDS_FILE.read_text(encoding="utf-8"))
    return {}


def _save_coords(cache: dict):
    _COORDS_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _geocode(hall_name: str) -> Optional[list]:
    cache = _load_coords()
    if hall_name in cache:
        return cache[hall_name]
    query = f"{hall_name} 大阪府"
    url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(query)}&format=json&limit=1&countrycodes=jp"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pachi-tool/1.0 (local research)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        if data:
            coords = [float(data[0]["lat"]), float(data[0]["lon"])]
            cache[hall_name] = coords
            _save_coords(cache)
            _time.sleep(1.1)
            return coords
    except Exception:
        pass
    return None


@app.get("/api/map/halls", tags=["map"])
def get_map_halls(days: int = Query(30)) -> list[dict]:
    """マップ用ホール強度データ（差枚スコアで色分け）"""
    conn = _get_reports_conn()
    if not conn:
        return []

    rows = conn.execute("""
        SELECT hall_name,
               AVG(diff_coins) AS avg_diff,
               COUNT(DISTINCT report_date) AS days_cnt,
               SUM(CASE WHEN diff_coins > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS win_rate
        FROM hall_day_seat
        WHERE bb_prob IS NOT NULL
          AND report_date >= date('now', '-' || ? || ' days')
          AND machine_name NOT LIKE '末尾%' AND machine_name != '全データ一覧'
        GROUP BY hall_name
        HAVING days_cnt >= 1
        ORDER BY avg_diff DESC
    """, (days,)).fetchall()
    conn.close()

    if not rows:
        return []

    diffs = [r[1] or 0 for r in rows]
    mn, mx = min(diffs), max(diffs)
    rng = max(mx - mn, 1)

    result = []
    for r in rows:
        coords = _geocode(r[0])
        if not coords:
            continue
        score = (r[1] - mn) / rng  # 0.0（弱）〜 1.0（強）
        # 赤(強) → 黄 → 緑(弱)
        if score >= 0.7:
            color = "#e53e3e"
        elif score >= 0.4:
            color = "#dd8800"
        elif score >= 0.2:
            color = "#c8b800"
        else:
            color = "#38a169"
        result.append({
            "hall_name": r[0],
            "lat": coords[0],
            "lng": coords[1],
            "avg_diff": round(r[1] or 0),
            "win_rate": round(r[3] or 0, 1),
            "days_cnt": r[2],
            "score": round(score, 3),
            "color": color,
        })
    return result


@app.get("/api/hall/compare", tags=["hall"])
def get_hall_compare(days: int = Query(30)) -> list[dict]:
    """
    全ホールの設定レベルを比較。
    ホール間の機種別推定設定と差枚を一覧化することで、どのホールが出ているかを分析。
    """
    conn = _get_reports_conn()
    if not conn:
        return []
    rows = conn.execute(
        """SELECT hall_name,
                  ROUND(AVG(diff_coins)) as avg_diff,
                  COUNT(*) as records,
                  COUNT(DISTINCT machine_name) as machine_cnt,
                  COUNT(DISTINCT report_date) as days_cnt,
                  ROUND(AVG(CASE WHEN diff_coins > 0 THEN 1.0 ELSE 0.0 END)*100) as win_rate,
                  ROUND(AVG(bb_prob)*100, 4) as avg_bb_pct,
                  MAX(report_date) as last_date
           FROM hall_day_seat
           WHERE machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
             AND machine_name NOT LIKE '%データ%'
             AND (bb_prob IS NOT NULL OR ev_pct IS NOT NULL)
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY hall_name
           HAVING records >= 10
           ORDER BY avg_diff DESC""",
        (days,)
    ).fetchall()
    conn.close()
    return [
        {
            "hall_name": r[0],
            "avg_diff": int(r[1] or 0),
            "records": r[2],
            "machine_cnt": r[3],
            "days_cnt": r[4],
            "win_rate": float(r[5] or 0),
            "avg_bb_pct": float(r[6] or 0),
            "last_date": r[7],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# AI エンドポイント
# ---------------------------------------------------------------------------

try:
    from api.ai_service import chat as ai_chat, generate_report, comment_estimate
    AI_AVAILABLE = True
except ImportError:
    try:
        from ai_service import chat as ai_chat, generate_report, comment_estimate
        AI_AVAILABLE = True
    except ImportError:
        AI_AVAILABLE = False


class ChatRequest(BaseModel):
    message: str
    hall_name: str = "ベガスベガス大東店"
    history: list = []


@app.post("/api/ai/chat")
def api_ai_chat(req: ChatRequest):
    if not AI_AVAILABLE:
        return {"reply": "AIサービスが利用できません。"}
    reply = ai_chat(req.message, req.hall_name, req.history)
    return {"reply": reply}


@app.get("/api/ai/report")
def api_ai_report(hall_name: str = "ベガスベガス大東店"):
    if not AI_AVAILABLE:
        return {"report": "AIサービスが利用できません。"}
    report = generate_report(hall_name)
    return {"report": report}


@app.post("/api/ai/estimate_comment")
def api_ai_estimate_comment(body: dict):
    if not AI_AVAILABLE:
        return {"comment": ""}
    comment = comment_estimate(
        machine_name=body.get("machine_name", ""),
        games=body.get("games", 0),
        element_counts=body.get("element_counts", {}),
        posterior=body.get("posterior", {}),
        ev=body.get("ev", 0),
        recommendation=body.get("recommendation", ""),
        element_analysis=body.get("element_analysis", []),
        credible_interval=body.get("credible_interval"),
        element_powers=body.get("element_powers"),
        correlated_elements=body.get("correlated_elements"),
    )
    return {"comment": comment}


@app.get("/api/ai/status")
def api_ai_status():
    import os
    has_key = bool(os.environ.get("GROQ_API_KEY", ""))
    return {"available": has_key and AI_AVAILABLE}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")
