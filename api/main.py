"""
pachi-tool FastAPI バックエンド。

起動:
    uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
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

# ---------------------------------------------------------------------------
# 簡易インメモリキャッシュ（TTLベース）
# ---------------------------------------------------------------------------
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 600  # 10分


def _cache_get(key: str) -> object | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and time.time() - entry[0] < _CACHE_TTL:
            return entry[1]
    return None


def _cache_set(key: str, value: object) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), value)


def _cache_invalidate_prefix(prefix: str) -> None:
    with _CACHE_LOCK:
        keys = [k for k in _CACHE if k.startswith(prefix)]
        for k in keys:
            del _CACHE[k]

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


@app.get("/api/sessions/estimation_accuracy", tags=["sessions"])
def get_estimation_accuracy(
    hall_name: Optional[str] = Query(None),
    limit: int = Query(100),
) -> dict:
    """
    推定設定 vs 実差枚の相関分析。
    推測エンジンの精度を評価し、「高設定推定時に実際に収益がプラスだった率」を返す。
    """
    from records.models import list_sessions
    sessions = list_sessions(hall_name=hall_name)
    if not sessions:
        return {"message": "セッションなし"}

    valid = []
    for s in sessions[-limit:]:
        if s.posterior is None or s.diff_coins is None:
            continue
        try:
            post = json.loads(s.posterior) if isinstance(s.posterior, str) else s.posterior
            if not post:
                continue
            exp_s = sum(float(k) * v for k, v in post.items())
            high_p = sum(v for k, v in post.items() if float(k) >= 4)
            valid.append({
                "expected_setting": exp_s,
                "high_prob": high_p,
                "diff_coins": s.diff_coins,
                "games": s.games_total,
                "is_positive": s.diff_coins > 0,
            })
        except Exception:
            continue

    if not valid:
        return {"message": "推測データ付きセッションなし"}

    # 高設定推定（≥4）時の勝率
    high_est = [v for v in valid if v["expected_setting"] >= 4.0]
    low_est  = [v for v in valid if v["expected_setting"] < 3.0]
    high_est_winrate = sum(1 for v in high_est if v["is_positive"]) / len(high_est) if high_est else None
    low_est_winrate  = sum(1 for v in low_est if v["is_positive"]) / len(low_est) if low_est else None

    # 高設定確率別の勝率区分
    brackets = []
    for lo, hi in [(0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.0)]:
        grp = [v for v in valid if lo <= v["high_prob"] < hi]
        if grp:
            wr = sum(1 for v in grp if v["is_positive"]) / len(grp)
            avg_diff = sum(v["diff_coins"] for v in grp) / len(grp)
            brackets.append({
                "bracket": f"高設定確率{int(lo*100)}~{int(hi*100)}%",
                "count": len(grp),
                "win_rate": round(wr * 100, 1),
                "avg_diff": round(avg_diff),
            })

    # 期待設定との相関（単純な方向性）
    correct_direction = sum(
        1 for v in valid
        if (v["expected_setting"] >= 4 and v["diff_coins"] > 0) or
           (v["expected_setting"] < 3 and v["diff_coins"] <= 0)
    )
    direction_accuracy = correct_direction / len(valid) if valid else 0

    return {
        "total_sessions_analyzed": len(valid),
        "overall_win_rate": round(sum(1 for v in valid if v["is_positive"]) / len(valid) * 100, 1),
        "high_setting_est_sessions": len(high_est),
        "high_setting_est_win_rate": round(high_est_winrate * 100, 1) if high_est_winrate is not None else None,
        "low_setting_est_sessions": len(low_est),
        "low_setting_est_win_rate": round(low_est_winrate * 100, 1) if low_est_winrate is not None else None,
        "direction_accuracy": round(direction_accuracy * 100, 1),
        "high_prob_brackets": brackets,
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


@app.get("/api/hall/compare", tags=["hall"])
def compare_halls(days: int = Query(30)) -> list[dict]:
    """
    全ホールのBB/RB平均・設定傾向を横断比較する。
    最も高設定率が高いホールをランキング順で返す。
    """
    ckey = f"hall_compare:{days}"
    cached = _cache_get(ckey)
    if cached:
        return cached

    conn = _get_reports_conn()
    if not conn:
        return []

    rows = conn.execute(
        """SELECT hall_name,
                  COUNT(DISTINCT report_date) as days_count,
                  COUNT(DISTINCT machine_name) as machine_count,
                  COUNT(DISTINCT seat_number) as seat_count,
                  AVG(bb_prob) as avg_bb,
                  AVG(rb_prob) as avg_rb,
                  AVG(diff_coins) as avg_diff,
                  SUM(CASE WHEN diff_coins > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate,
                  MAX(report_date) as latest_date,
                  COUNT(*) as record_count
           FROM hall_day_seat
           WHERE bb_prob IS NOT NULL
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY hall_name
           HAVING record_count >= 20
           ORDER BY avg_bb DESC""",
        (days,)
    ).fetchall()
    conn.close()

    if not rows:
        return []

    import statistics as _stats
    bbs = [float(r[4]) for r in rows if r[4]]
    if len(bbs) < 2:
        return []
    mean_bb = _stats.mean(bbs)
    std_bb = _stats.stdev(bbs) if len(bbs) > 1 else 0.0001

    result = []
    for i, r in enumerate(rows):
        bb = float(r[4]) if r[4] else 0
        z = round((bb - mean_bb) / max(std_bb, 0.00001), 2)
        result.append({
            "rank": i + 1,
            "hall_name": r[0],
            "days_data": r[1],
            "machine_count": r[2],
            "seat_count": r[3] or 0,
            "avg_bb_pct": round(bb * 100, 4),
            "avg_rb_pct": round(float(r[5] or 0) * 100, 4),
            "avg_diff": round(float(r[6] or 0)),
            "win_rate": round(float(r[7] or 0), 1),
            "latest_date": r[8] or "",
            "record_count": r[9],
            "bb_z": z,
        })

    _cache_set(ckey, result)
    return result


@app.post("/api/cache/clear", tags=["admin"])
def clear_cache(hall_name: Optional[str] = Query(None)) -> dict:
    """インメモリキャッシュを消去する。hall_name 指定でそのホールのみ。"""
    with _CACHE_LOCK:
        if hall_name:
            keys = [k for k in _CACHE if hall_name in k]
            for k in keys:
                del _CACHE[k]
            cleared = len(keys)
        else:
            cleared = len(_CACHE)
            _CACHE.clear()
    return {"cleared": cleared, "message": f"{cleared}件のキャッシュを削除しました"}


@app.get("/api/cache/stats", tags=["admin"])
def cache_stats() -> dict:
    """キャッシュの統計情報を返す。"""
    import time as _t
    now = _t.time()
    with _CACHE_LOCK:
        entries = [(k, now - v[0]) for k, v in _CACHE.items()]
    return {
        "total_entries": len(entries),
        "ttl_seconds": _CACHE_TTL,
        "entries": [{"key": k, "age_seconds": round(a)} for k, a in sorted(entries, key=lambda x: x[1])]
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


@app.get("/api/hall/tail_bb_analysis", tags=["hall"])
def get_tail_bb_analysis(
    hall_name: str = Query(...),
    days: int = Query(90),
) -> list[dict]:
    """
    末尾番号別のBB確率分析。
    差枚より精度が高い（差枚はゲーム数に依存するが、BB確率は設定に直接対応）。
    同ホール内の末尾間でBB確率をz-score比較し、設定配分傾向を推定する。
    """
    conn = _get_reports_conn()
    if not conn:
        return []
    rows = conn.execute(
        """SELECT (seat_number % 10) as tail,
                  COUNT(*) as cnt,
                  AVG(bb_prob) as avg_bb,
                  AVG(rb_prob) as avg_rb,
                  AVG(diff_coins) as avg_diff,
                  AVG(CASE WHEN diff_coins > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
                  COUNT(DISTINCT seat_number) as seat_cnt
           FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
             AND machine_name NOT LIKE '%データ%'
             AND seat_number IS NOT NULL
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY tail HAVING cnt >= 5
           ORDER BY avg_bb DESC""",
        (hall_name, days)
    ).fetchall()
    conn.close()

    if not rows:
        return []

    import statistics as _stats
    bbs = [float(r[2]) for r in rows if r[2] is not None]
    if len(bbs) < 2:
        return []
    mean_bb = _stats.mean(bbs)
    std_bb = _stats.stdev(bbs) if len(bbs) > 1 else 0.0001

    result = []
    for r in rows:
        tail, cnt, avg_bb, avg_rb, avg_diff, win_rate, seat_cnt = r
        if avg_bb is None:
            continue
        z = (float(avg_bb) - mean_bb) / max(std_bb, 0.00001)
        result.append({
            "tail": int(tail),
            "count": cnt,
            "avg_bb": round(float(avg_bb) * 100, 4),
            "avg_rb": round(float(avg_rb or 0) * 100, 4),
            "avg_diff": int(avg_diff or 0),
            "win_rate": round(float(win_rate or 0) * 100, 1),
            "seat_cnt": seat_cnt,
            "z_score": round(z, 2),
        })

    result.sort(key=lambda x: -x["z_score"])
    return result


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

    # 連続好調日数（直近から遡ってdiff > 0 が続く日数）
    streak = 0
    for h in hist_list:
        if (h["diff"] or 0) > 0:
            streak += 1
        else:
            break

    # BB確率トレンド：直近14日 vs 過去14-28日
    bbs = [(h["date"], h["bb_prob"]) for h in hist_list if h["bb_prob"]]
    recent14 = [b for d, b in bbs[:14]]
    prev14 = [b for d, b in bbs[14:28]]
    bb_trend = None
    if recent14 and prev14:
        bb_trend = round((sum(recent14)/len(recent14) - sum(prev14)/len(prev14)) * 100, 4)

    return {
        "machine_name": machine_name,
        "seat_number": seat_number,
        "total_days": len(hist_list),
        "avg_diff": avg,
        "win_rate": win_rate,
        "best": best,
        "worst": worst,
        "std": std,
        "win_streak": streak,
        "bb_trend_14d": bb_trend,
        "history": hist_list[:60],
        "weekday_stats": weekday_stats,
    }


@app.get("/api/hall/machine_seat_ranking", tags=["hall"])
def get_machine_seat_ranking(
    hall_name: str = Query(...),
    machine_name: str = Query(...),
    days: int = Query(30),
) -> list[dict]:
    """特定機種の全台番ランキング（複合スコア + BB z-score付き）"""
    import datetime, math as _math
    today = datetime.date.today()
    sql_dow = str((today.weekday() + 1) % 7)

    conn = _get_reports_conn()
    if not conn:
        return []

    # メイン集計
    rows = conn.execute(
        """SELECT seat_number,
                  COUNT(*) as total_days,
                  ROUND(AVG(diff_coins)) as avg_diff,
                  ROUND(AVG(diff_coins*diff_coins) - AVG(diff_coins)*AVG(diff_coins)) as variance,
                  SUM(CASE WHEN diff_coins > 0 THEN 1 ELSE 0 END)*100.0/COUNT(*) as win_rate,
                  ROUND(AVG(CASE WHEN strftime('%w',report_date)=? THEN diff_coins END)) as avg_dow,
                  COUNT(CASE WHEN strftime('%w',report_date)=? THEN 1 END) as cnt_dow,
                  ROUND(AVG(CASE WHEN report_date >= date('now','-7 days') THEN diff_coins END)) as avg_7d,
                  AVG(bb_prob) as avg_bb
           FROM hall_day_seat
           WHERE hall_name=? AND machine_name=? AND (bb_prob IS NOT NULL OR ev_pct IS NOT NULL)
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY seat_number
           HAVING total_days >= 2
           ORDER BY avg_diff DESC""",
        (sql_dow, sql_dow, hall_name, machine_name, days)
    ).fetchall()
    conn.close()

    # BB z-score 計算 (機種内で相対化)
    import statistics as _stats
    bb_vals = [float(r[8]) for r in rows if r[8] is not None]
    bb_mean = _stats.mean(bb_vals) if bb_vals else 0
    bb_std = _stats.stdev(bb_vals) if len(bb_vals) > 1 else 0.0001

    result = []
    for r in rows:
        avg = r[2] or 0
        var = max(r[3] or 0, 0)
        std = _math.sqrt(var)
        stability = max(0.0, 1.0 - std / (abs(avg) + 1500)) if avg > 0 else 0.0
        avg_dow = r[5] if (r[5] is not None and r[6] >= 1) else avg
        avg_7d  = r[7] if r[7] is not None else avg
        trend   = avg_7d - avg
        avg_bb  = float(r[8]) if r[8] is not None else None
        bb_z    = round((avg_bb - bb_mean) / max(bb_std, 1e-8), 2) if avg_bb is not None else None
        bb_bonus = (bb_z * 500) if bb_z is not None else 0.0
        score = avg * 0.35 + avg_dow * 0.20 + avg * stability * 0.15 + trend * 0.10 + bb_bonus * 0.20
        result.append({
            "seat_number": r[0],
            "days": r[1],
            "avg_diff": int(avg),
            "win_rate": round(r[4] or 0, 1),
            "avg_same_dow": int(avg_dow),
            "avg_7d": int(avg_7d) if r[7] is not None else None,
            "stability": round(stability, 2),
            "avg_bb": round(avg_bb * 100, 4) if avg_bb is not None else None,
            "bb_z": bb_z,
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

    # 全台の過去stats（avg・分散・勝率・直近7日・BB確率）
    seat_rows = conn.execute(
        """SELECT machine_name, seat_number,
                  COUNT(*) as total_days,
                  ROUND(AVG(diff_coins)) as avg_diff,
                  ROUND(AVG(diff_coins*diff_coins) - AVG(diff_coins)*AVG(diff_coins)) as variance,
                  SUM(CASE WHEN diff_coins > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate,
                  ROUND(AVG(CASE WHEN report_date >= date('now','-7 days') THEN diff_coins END)) as avg_7d,
                  COUNT(CASE WHEN report_date >= date('now','-7 days') THEN 1 END) as cnt_7d,
                  ROUND(AVG(CASE WHEN strftime('%w',report_date)=? THEN diff_coins END)) as avg_same_dow,
                  COUNT(CASE WHEN strftime('%w',report_date)=? THEN 1 END) as cnt_same_dow,
                  AVG(bb_prob) as avg_bb,
                  AVG(CASE WHEN strftime('%w',report_date)=? THEN bb_prob END) as dow_bb
           FROM hall_day_seat
           WHERE hall_name=? AND machine_name NOT LIKE '末尾%'
             AND machine_name != '_NODATA_' AND machine_name NOT LIKE '%データ%'
             AND (bb_prob IS NOT NULL OR ev_pct IS NOT NULL)
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY machine_name, seat_number
           HAVING total_days >= 3""",
        (str(sql_dow), str(sql_dow), str(sql_dow), hall_name, days)
    ).fetchall()

    # 機種ごとのBB平均・標準偏差（z-score計算用）
    machine_bb_stats: dict[str, tuple[float, float]] = {}
    machine_bb_rows = conn.execute(
        """SELECT machine_name, seat_number, AVG(bb_prob) as avg_bb
           FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY machine_name, seat_number HAVING COUNT(*) >= 2""",
        (hall_name, days)
    ).fetchall()
    _m_bbs: dict[str, list[float]] = {}
    for mr in machine_bb_rows:
        _m_bbs.setdefault(mr[0], []).append(float(mr[2]))
    for mname, bbs in _m_bbs.items():
        if len(bbs) >= 2:
            import statistics as _stats2
            m = _stats2.mean(bbs)
            s = _stats2.stdev(bbs) if len(bbs) > 1 else 0.001
            machine_bb_stats[mname] = (m, s)

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
    # score = avg_diff(35%) + 同曜日avg(20%) + 安定性(15%) + トレンド(10%) + BB_z(20%)
    scored = []
    for r in seat_rows:
        (machine, seat, total_days, avg_diff, variance,
         win_rate, avg_7d, cnt_7d, avg_same_dow, cnt_same_dow,
         avg_bb, dow_bb) = r

        avg_diff      = avg_diff or 0
        variance      = max(variance or 0, 0)
        avg_7d        = avg_7d if avg_7d is not None else avg_diff
        avg_same_dow  = avg_same_dow if (avg_same_dow is not None and cnt_same_dow >= 1) else avg_diff
        win_rate      = win_rate or 0

        # 安定性: 標準偏差が小さいほど高スコア
        std = _math.sqrt(variance)
        stability = max(0.0, 1.0 - std / (abs(avg_diff) + 1500)) if avg_diff > 0 else 0.0

        # 直近トレンド
        trend = avg_7d - avg_diff if cnt_7d >= 2 else 0.0

        # BB確率z-score（機種内比較）→ 差枚スコアに上乗せ
        bb_z = 0.0
        if avg_bb and machine in machine_bb_stats:
            m_bb, s_bb = machine_bb_stats[machine]
            bb_z = (float(avg_bb) - m_bb) / max(s_bb, 1e-8)
        # 同曜日BB確率も考慮（あれば）
        if dow_bb and machine in machine_bb_stats:
            m_bb, s_bb = machine_bb_stats[machine]
            dow_bb_z = (float(dow_bb) - m_bb) / max(s_bb, 1e-8)
            bb_z = bb_z * 0.6 + dow_bb_z * 0.4  # 同曜日を重く

        # BB z-score → 差枚換算（z=1.0 ≈ +500枚相当で換算）
        bb_bonus = bb_z * 500

        # 正規化スコア（差枚ベース + BB確率シグナル）
        score = (
            avg_diff     * 0.35 +
            avg_same_dow * 0.20 +
            avg_diff * stability * 0.15 +
            trend        * 0.10 +
            bb_bonus     * 0.20
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
            "bb_z": round(bb_z, 2),
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


@app.get("/api/hall/machine_dow_heatmap", tags=["hall"])
def get_machine_dow_heatmap(
    hall_name: str = Query(...),
    days: int = Query(90),
    top_n: int = Query(10),
) -> dict:
    """
    機種×曜日の平均BB確率ヒートマップ。
    各セルに曜日基準からのz-scoreを返す（プラス=当日好調曜日）。
    """
    ckey = f"dow_heatmap:{hall_name}:{days}"
    cached = _cache_get(ckey)
    if cached:
        return cached

    conn = _get_reports_conn()
    if not conn:
        return {"machines": [], "dow_labels": []}

    # 上位機種を先に絞り込み
    top_machines = conn.execute(
        """SELECT machine_name, COUNT(*) as cnt
           FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY machine_name HAVING cnt >= 10
           ORDER BY cnt DESC LIMIT ?""",
        (hall_name, days, top_n)
    ).fetchall()

    if not top_machines:
        conn.close()
        return {"machines": [], "dow_labels": []}

    machine_names = [r[0] for r in top_machines]
    placeholders = ','.join('?' * len(machine_names))

    rows = conn.execute(
        f"""SELECT machine_name,
                   CAST(strftime('%w', report_date) AS INTEGER) as dow,
                   AVG(bb_prob) as avg_bb, COUNT(*) as cnt
            FROM hall_day_seat
            WHERE hall_name=? AND bb_prob IS NOT NULL
              AND machine_name IN ({placeholders})
              AND report_date >= date('now', '-' || ? || ' days')
            GROUP BY machine_name, dow""",
        [hall_name] + machine_names + [days]
    ).fetchall()
    conn.close()

    import math as _math
    # strftime %w: 0=日, 1=月...6=土 → 日本式に変換: 月=0〜日=6
    _DOW_MAP = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 0: 6}
    DOW_LABELS = ["月", "火", "水", "木", "金", "土", "日"]

    # machine → dow → avg_bb
    data: dict[str, dict[int, float]] = {}
    for row in rows:
        mn, dow_raw, avg_bb, cnt = row
        if mn not in data:
            data[mn] = {}
        jp_dow = _DOW_MAP.get(int(dow_raw), int(dow_raw))
        data[mn][jp_dow] = float(avg_bb)

    result_machines = []
    for mn in machine_names:
        if mn not in data:
            continue
        vals = list(data[mn].values())
        if len(vals) < 2:
            continue
        mean_bb = sum(vals) / len(vals)
        std_bb = _math.sqrt(sum((v - mean_bb)**2 for v in vals) / len(vals)) if len(vals) > 1 else 0.0001
        if std_bb < 1e-9:
            std_bb = 0.0001

        cells = []
        for jp_dow in range(7):
            bb = data[mn].get(jp_dow)
            if bb is not None:
                z = round((bb - mean_bb) / std_bb, 2)
                cells.append({"dow": jp_dow, "bb": round(bb * 100, 4), "z": z})
            else:
                cells.append({"dow": jp_dow, "bb": None, "z": None})

        result_machines.append({
            "machine": mn,
            "mean_bb": round(mean_bb * 100, 4),
            "cells": cells,
        })

    out = {"machines": result_machines, "dow_labels": DOW_LABELS}
    _cache_set(ckey, out)
    return out


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


@app.get("/api/hall/prior_quality", tags=["hall"])
def get_prior_quality(
    hall_name: str = Query(...),
    machine_name: str = Query(...),
) -> dict:
    """
    ホール×機種の事前分布品質スコアを返す。
    - records: アナスロデータ件数
    - bb_coverage: BB/RBデータ有率
    - avg_games: 平均ゲーム数
    - theory_match: 機種JSONが存在するか
    - quality_score: 0-100の総合スコア
    - quality_label: テキスト評価
    """
    conn = _get_reports_conn()
    if not conn:
        return {"quality_score": 0, "quality_label": "データなし"}
    row = conn.execute(
        """SELECT COUNT(*) as total,
                  SUM(CASE WHEN bb_prob IS NOT NULL THEN 1 ELSE 0 END) as bb_cnt,
                  ROUND(AVG(games)) as avg_games,
                  COUNT(DISTINCT seat_number) as seat_cnt,
                  MAX(report_date) as last_date,
                  COUNT(DISTINCT report_date) as date_cnt
           FROM hall_day_seat
           WHERE hall_name=? AND machine_name=?""",
        (hall_name, machine_name)
    ).fetchone()
    conn.close()

    total = row[0] or 0
    bb_cnt = row[1] or 0
    avg_games = float(row[2] or 0)
    seat_cnt = row[3] or 0
    last_date = row[4]
    date_cnt = row[5] or 0

    if total == 0:
        return {"quality_score": 0, "quality_label": "データなし", "records": 0}

    from hall.prior import _load_machine_theory
    theory = _load_machine_theory(machine_name)
    theory_match = theory is not None

    # スコア計算
    rec_score  = min(40, total * 2)           # 件数: max 40点 (20件以上で満点)
    bb_score   = (bb_cnt / total) * 20        # BB/RBカバレッジ: max 20点
    game_score = min(20, avg_games / 50)      # 平均G数: max 20点 (1000G以上で満点)
    theory_score = 15 if theory_match else 0  # 理論値あり: 15点
    seat_score = min(5, seat_cnt)             # 台数: max 5点

    total_score = int(rec_score + bb_score + game_score + theory_score + seat_score)
    total_score = min(100, total_score)

    if total_score >= 75:
        label = "高品質 ★★★"
    elif total_score >= 50:
        label = "中品質 ★★"
    elif total_score >= 25:
        label = "低品質 ★"
    else:
        label = "データ不足"

    return {
        "quality_score": total_score,
        "quality_label": label,
        "records": total,
        "bb_coverage": round(bb_cnt / total * 100) if total else 0,
        "avg_games": int(avg_games),
        "seat_cnt": seat_cnt,
        "date_cnt": date_cnt,
        "last_date": last_date,
        "theory_match": theory_match,
    }


@app.get("/api/hall/event_day_pattern", tags=["hall"])
def get_event_day_pattern(
    hall_name: str = Query(...),
    days: int = Query(180),
) -> dict:
    """
    日付パターン分析。「5のつく日」「月末」「毎週特定曜日」など
    どのパターンがBB確率上昇と相関するかを統計的に分析。
    """
    ckey = f"event_day:{hall_name}:{days}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached  # type: ignore
    conn = _get_reports_conn()
    if not conn:
        return {}

    rows = conn.execute(
        """SELECT report_date, AVG(bb_prob) as avg_bb, COUNT(*) as cnt
           FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY report_date HAVING cnt >= 3
           ORDER BY report_date""",
        (hall_name, days)
    ).fetchall()
    conn.close()

    if len(rows) < 10:
        return {"message": "データ不足（10日以上必要）"}

    import datetime as _dt
    import statistics as _s

    all_bbs = [float(r[1]) for r in rows]
    global_mean = _s.mean(all_bbs)
    global_std = _s.stdev(all_bbs) if len(all_bbs) > 1 else 0.001

    def analyze_pattern(pattern_rows, other_rows):
        if not pattern_rows or not other_rows:
            return None
        p_bbs = [float(r[1]) for r in pattern_rows]
        o_bbs = [float(r[1]) for r in other_rows]
        p_mean = sum(p_bbs) / len(p_bbs)
        o_mean = sum(o_bbs) / len(o_bbs)
        z = (p_mean - o_mean) / max(global_std, 1e-8)
        return {"pattern_mean": round(p_mean * 100, 4), "other_mean": round(o_mean * 100, 4),
                "z": round(z, 2), "count": len(pattern_rows)}

    # 日付のlast digit（末尾パターン）
    tail_results = {}
    for tail in range(10):
        p = [r for r in rows if _dt.date.fromisoformat(r[0]).day % 10 == tail]
        o = [r for r in rows if _dt.date.fromisoformat(r[0]).day % 10 != tail]
        if len(p) >= 3:
            res = analyze_pattern(p, o)
            if res:
                tail_results[str(tail)] = res

    # 曜日パターン
    dow_results = {}
    dow_names = {0:"月",1:"火",2:"水",3:"木",4:"金",5:"土",6:"日"}
    for dow in range(7):
        p = [r for r in rows if _dt.date.fromisoformat(r[0]).weekday() == dow]
        o = [r for r in rows if _dt.date.fromisoformat(r[0]).weekday() != dow]
        if len(p) >= 3:
            res = analyze_pattern(p, o)
            if res:
                dow_results[dow_names[dow]] = res

    # 5のつく日(5,15,25)
    fives = [r for r in rows if _dt.date.fromisoformat(r[0]).day in (5, 15, 25)]
    others_fives = [r for r in rows if _dt.date.fromisoformat(r[0]).day not in (5, 15, 25)]
    fives_result = analyze_pattern(fives, others_fives) if len(fives) >= 2 else None

    # 上位パターンを抽出
    top_patterns = []
    for tail, res in tail_results.items():
        if res["z"] >= 0.5:
            top_patterns.append({"type": f"末尾{tail}の日", "z": res["z"],
                                  "count": res["count"], "bb_mean": res["pattern_mean"]})
    for dow, res in dow_results.items():
        if res["z"] >= 0.5:
            top_patterns.append({"type": f"{dow}曜日", "z": res["z"],
                                  "count": res["count"], "bb_mean": res["pattern_mean"]})
    if fives_result and fives_result["z"] >= 0.5:
        top_patterns.append({"type": "5・15・25日", "z": fives_result["z"],
                              "count": fives_result["count"], "bb_mean": fives_result["pattern_mean"]})

    top_patterns.sort(key=lambda x: -x["z"])

    result = {
        "global_mean_bb": round(global_mean * 100, 4),
        "tail_results": tail_results,
        "dow_results": dow_results,
        "fives_result": fives_result,
        "top_patterns": top_patterns[:5],
        "total_days": len(rows),
    }
    _cache_set(ckey, result)
    return result


@app.get("/api/hall/zone_analysis", tags=["hall"])
def get_zone_analysis(
    hall_name: str = Query(...),
    days: int = Query(90),
    zone_size: int = Query(10),
) -> list[dict]:
    """
    台番号をzone_size単位でグループ化して高設定率を比較。
    特定の「島」または「ゾーン」に高設定が集中するパターンを検知。
    """
    ckey = f"zone:{hall_name}:{days}:{zone_size}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached  # type: ignore
    conn = _get_reports_conn()
    if not conn:
        return []

    rows = conn.execute(
        """SELECT seat_number, AVG(bb_prob) as avg_bb, COUNT(*) as cnt
           FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL AND seat_number IS NOT NULL
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY seat_number HAVING cnt >= 3""",
        (hall_name, days)
    ).fetchall()
    conn.close()

    if not rows:
        return []

    import statistics as _stats

    # ゾーンに集計
    zone_data: dict[int, list[float]] = {}
    seat_counts: dict[int, int] = {}
    for seat_num, avg_bb, cnt in rows:
        z_key = ((int(seat_num) - 1) // zone_size) * zone_size + 1
        zone_data.setdefault(z_key, []).append(float(avg_bb))
        seat_counts[z_key] = seat_counts.get(z_key, 0) + 1

    if len(zone_data) < 2:
        return []

    zone_means = {k: sum(v) / len(v) for k, v in zone_data.items()}
    all_bbs = [v for vals in zone_data.values() for v in vals]
    global_mean = _stats.mean(all_bbs)
    global_std = _stats.stdev(all_bbs) if len(all_bbs) > 1 else 0.0001

    result = []
    for z_start in sorted(zone_data.keys()):
        vals = zone_data[z_start]
        mean_bb = zone_means[z_start]
        z_score = (mean_bb - global_mean) / max(global_std, 1e-8)
        result.append({
            "zone_start": z_start,
            "zone_end": z_start + zone_size - 1,
            "label": f"{z_start}~{z_start+zone_size-1}番台",
            "seat_count": seat_counts[z_start],
            "record_count": len(vals),
            "avg_bb": round(mean_bb * 100, 4),
            "z_score": round(z_score, 2),
        })

    result.sort(key=lambda x: -x["z_score"])
    _cache_set(ckey, result)
    return result


@app.get("/api/hall/machine_high_rate", tags=["hall"])
def get_machine_high_rate(
    hall_name: str = Query(...),
    days: int = Query(90),
) -> list[dict]:
    """
    機種ごとの「高設定投入率」推定。
    各台日のBB確率を機種内でz-score化し、z>=1.0の割合を「高設定率」として返す。
    高設定率が高い機種 = このホールが力を入れている機種。
    """
    ckey = f"machine_high_rate:{hall_name}:{days}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached  # type: ignore
    conn = _get_reports_conn()
    if not conn:
        return []

    rows = conn.execute(
        """SELECT machine_name, seat_number, AVG(bb_prob) as avg_bb, COUNT(*) as cnt
           FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY machine_name, seat_number HAVING cnt >= 3""",
        (hall_name, days)
    ).fetchall()
    conn.close()

    import statistics as _s

    # 機種ごとにグループ化
    from collections import defaultdict
    machine_seats: dict[str, list[float]] = defaultdict(list)
    machine_counts: dict[str, int] = defaultdict(int)
    for m, s, avg_bb, cnt in rows:
        machine_seats[m].append(float(avg_bb))
        machine_counts[m] += cnt

    result = []
    for mname, bbs in machine_seats.items():
        if len(bbs) < 3:
            continue
        mean_bb = _s.mean(bbs)
        std_bb = _s.stdev(bbs) if len(bbs) > 1 else 0.001
        if std_bb < 1e-8:
            continue
        high_seats = sum(1 for b in bbs if (b - mean_bb) / std_bb >= 1.0)
        medium_seats = sum(1 for b in bbs if 0.3 <= (b - mean_bb) / std_bb < 1.0)
        high_rate = high_seats / len(bbs)
        result.append({
            "machine_name": mname,
            "total_seats": len(bbs),
            "high_seats": high_seats,
            "medium_seats": medium_seats,
            "high_rate": round(high_rate * 100, 1),
            "avg_bb": round(mean_bb * 100, 4),
            "records": machine_counts[mname],
        })

    result.sort(key=lambda x: (-x["high_rate"], -x["total_seats"]))
    out = result[:20]
    _cache_set(ckey, out)
    return out


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


@app.get("/api/hall/today_briefing", tags=["hall"])
def get_today_briefing(hall_name: str = Query(...)) -> dict:
    """
    本日の攻略ブリーフィング。開店前に確認すべき全情報を1回のAPIコールで返す。
    - イベント日候補か
    - 今日の曜日の傾向スコア
    - BB急上昇台リスト（上位3台）
    - 連続好調台リスト（上位3台）
    - 今日の狙い台ランキング（上位5台）
    - 高設定率機種TOP3
    """
    import datetime as _dt, statistics as _s
    today = _dt.date.today()
    sql_dow = str((today.weekday() + 1) % 7)
    dow_ja = ["月","火","水","木","金","土","日"][today.weekday()]
    conn = _get_reports_conn()
    if not conn:
        return {"error": "DB接続失敗"}

    # 今日の曜日傾向
    dow_rows = conn.execute(
        """SELECT strftime('%w',report_date) as dow, AVG(diff_coins) as avg_diff, COUNT(*) as cnt
           FROM hall_day_seat WHERE hall_name=? AND bb_prob IS NOT NULL
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
           GROUP BY dow HAVING cnt >= 5""",
        (hall_name,)
    ).fetchall()
    dow_data = {r[0]: float(r[1] or 0) for r in dow_rows}
    today_dow_diff = dow_data.get(sql_dow)
    dow_all = sorted(dow_data.values(), reverse=True)
    dow_rank = dow_all.index(today_dow_diff) + 1 if today_dow_diff is not None else None

    # BB急上昇台
    prev_date = (today - _dt.timedelta(days=3)).isoformat()
    recent_bb = {(r[0], r[1]): float(r[2]) for r in conn.execute(
        """SELECT machine_name, seat_number, AVG(bb_prob) FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL AND report_date >= ?
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
           GROUP BY machine_name, seat_number HAVING COUNT(*) >= 1""",
        (hall_name, prev_date)
    ).fetchall()}
    baseline_bb = {(r[0], r[1]): float(r[2]) for r in conn.execute(
        """SELECT machine_name, seat_number, AVG(bb_prob) FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL AND report_date < ?
             AND report_date >= date(?, '-60 days')
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
           GROUP BY machine_name, seat_number HAVING COUNT(*) >= 3""",
        (hall_name, prev_date, prev_date)
    ).fetchall()}
    m_bbs: dict = {}
    for (m, s), b in baseline_bb.items():
        m_bbs.setdefault(m, []).append(b)
    m_std = {m: (_s.stdev(v) if len(v) > 1 else 0.001) for m, v in m_bbs.items()}
    surges = []
    for (m, s), rec_bb in recent_bb.items():
        base = baseline_bb.get((m, s))
        if base is None:
            continue
        z = (rec_bb - base) / max(m_std.get(m, 0.001), 1e-8)
        if z >= 0.8:
            surges.append({"machine": m, "seat": s, "surge_z": round(z, 1),
                           "recent_bb": round(rec_bb * 100, 3), "baseline_bb": round(base * 100, 3)})
    surges.sort(key=lambda x: -x["surge_z"])

    # 狙い台TOP5
    top_rows = conn.execute(
        """SELECT machine_name, seat_number,
                  COUNT(*) as days,
                  ROUND(AVG(diff_coins)) as avg_diff,
                  ROUND(AVG(CASE WHEN diff_coins > 0 THEN 1.0 ELSE 0.0 END)*100) as win_rate,
                  AVG(bb_prob) as avg_bb
           FROM hall_day_seat
           WHERE hall_name=? AND (bb_prob IS NOT NULL OR ev_pct IS NOT NULL)
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
             AND report_date >= date('now','-30 days')
           GROUP BY machine_name, seat_number HAVING days >= 3
           ORDER BY avg_diff DESC LIMIT 5""",
        (hall_name,)
    ).fetchall()
    top_seats = [{"machine": r[0], "seat": r[1], "days": r[2],
                  "avg_diff": int(r[3] or 0), "win_rate": round(r[4] or 0, 1),
                  "avg_bb": round(float(r[5] or 0) * 100, 3)} for r in top_rows]

    # 高設定率機種TOP3
    hr_rows = conn.execute(
        """SELECT machine_name, seat_number, AVG(bb_prob) as avg_bb
           FROM hall_day_seat WHERE hall_name=? AND bb_prob IS NOT NULL
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
             AND report_date >= date('now','-90 days')
           GROUP BY machine_name, seat_number HAVING COUNT(*) >= 3""",
        (hall_name,)
    ).fetchall()
    conn.close()
    mach_seats: dict = {}
    for m, s, bb in hr_rows:
        mach_seats.setdefault(m, []).append(float(bb))
    hr_list = []
    for m, bbs in mach_seats.items():
        if len(bbs) < 2:
            continue
        mean_bb = sum(bbs) / len(bbs)
        std_bb = _s.stdev(bbs)
        high_r = sum(1 for b in bbs if (b - mean_bb) / max(std_bb, 1e-8) >= 1.0) / len(bbs)
        hr_list.append({"machine": m, "high_rate": round(high_r * 100, 1), "seats": len(bbs)})
    hr_list.sort(key=lambda x: -x["high_rate"])

    # イベント日判定
    event_z = None
    try:
        from hall.prior import _compute_today_event_z
        event_z = _compute_today_event_z(hall_name)
    except Exception:
        pass

    return {
        "date": today.isoformat(),
        "weekday": dow_ja,
        "is_event_candidate": event_z is not None and event_z >= 0.5,
        "event_z": round(event_z, 2) if event_z else None,
        "dow_avg_diff": round(today_dow_diff) if today_dow_diff is not None else None,
        "dow_rank": dow_rank,
        "dow_total": len(dow_data),
        "bb_surge_seats": surges[:3],
        "top_seats": top_seats,
        "high_rate_machines": hr_list[:3],
    }


@app.get("/api/hall/bb_surge_seats", tags=["hall"])
def get_bb_surge_seats(
    hall_name: str = Query(...),
    days: int = Query(3),
    min_surge: float = Query(0.5),
) -> list[dict]:
    """
    前日比でBB確率が急上昇した台を検出。
    設定入れ替え（低→高）の強いシグナル。
    min_surge: 機種内z-scoreの最低上昇量（デフォルト0.5σ以上の急上昇）
    """
    import datetime as _dt
    conn = _get_reports_conn()
    if not conn:
        return []

    prev_date = (date.today() - _dt.timedelta(days=days)).isoformat()

    recent = conn.execute(
        """SELECT machine_name, seat_number, AVG(bb_prob) as avg_bb, COUNT(*) as cnt
           FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL
             AND report_date >= ?
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
           GROUP BY machine_name, seat_number HAVING cnt >= 1""",
        (hall_name, prev_date)
    ).fetchall()

    baseline = conn.execute(
        """SELECT machine_name, seat_number, AVG(bb_prob) as avg_bb
           FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL
             AND report_date < ?
             AND report_date >= date(?, '-60 days')
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
           GROUP BY machine_name, seat_number HAVING COUNT(*) >= 3""",
        (hall_name, prev_date, prev_date)
    ).fetchall()

    machine_std: dict[str, float] = {}
    m_bbs: dict[str, list[float]] = {}
    for r in baseline:
        m_bbs.setdefault(r[0], []).append(float(r[2]))
    for mname, bbs in m_bbs.items():
        import statistics as _s
        machine_std[mname] = (_s.stdev(bbs) if len(bbs) > 1 else 0.001) or 0.001

    baseline_map = {(r[0], r[1]): float(r[2]) for r in baseline}
    conn.close()

    results = []
    for r in recent:
        mname, seat, rec_bb = r[0], r[1], float(r[2])
        base_bb = baseline_map.get((mname, seat))
        if base_bb is None:
            continue
        std = machine_std.get(mname, 0.001)
        surge_z = (rec_bb - base_bb) / std
        if surge_z >= min_surge:
            results.append({
                "machine_name": mname,
                "seat_number": seat,
                "recent_bb": round(rec_bb * 100, 4),
                "baseline_bb": round(base_bb * 100, 4),
                "surge_z": round(surge_z, 2),
                "recent_days": days,
            })

    results.sort(key=lambda x: -x["surge_z"])
    return results[:20]


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
