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
# Static frontend
# ---------------------------------------------------------------------------

if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")
