"""イベント情報エンドポイント (/api/events/*)"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
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
from api import scheduler
from hall.machine_scope import clean_machine_display_name, is_smartslot_machine, normalize_machine_key
from hall.regions import region_label, region_matches

router = APIRouter()

_EVENT_PROGRESS: dict = {
    "running": False,
    "started_at": None,
    "halls": [],   # [{name, status, found, by_source}]
}


@router.get("/api/events/calendar", tags=["events"])
def get_event_calendar(
    month: str = Query(..., description="YYYY-MM"),
    hall_name: Optional[str] = Query(None),
    include_excluded: bool = Query(False),
) -> dict:
    """月次カレンダー用イベントデータ。日付→イベントリストのマップを返す。"""
    ckey = f"event_calendar_v3:{month}:{hall_name}:{include_excluded}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    try:
        conn = _get_event_conn()
        q = """SELECT id, hall_name, event_date, event_type, event_title, source,
                      source_url, event_kind, source_trust, prediction_eligible,
                      classification_reason
                 FROM hall_event WHERE event_date LIKE ?"""
        params: list = [f"{month}%"]
        if not include_excluded:
            q += " AND prediction_eligible=1"
        if hall_name:
            q += " AND hall_name=?"
            params.append(hall_name)
        q += " ORDER BY event_date, hall_name"
        rows = conn.execute(q, params).fetchall()
        conn.close()

        by_date: dict = {}
        for r in rows:
            d = r["event_date"]
            by_date.setdefault(d, []).append({
                "id": r["id"],
                "hall_name": r["hall_name"],
                "event_type": r["event_type"],
                "event_title": r["event_title"],
                "source": r["source"],
                "source_url": r["source_url"],
                "event_kind": r["event_kind"],
                "source_trust": r["source_trust"],
                "prediction_eligible": bool(r["prediction_eligible"]),
                "classification_reason": r["classification_reason"],
            })
        result = {
            "month": month,
            "events": by_date,
            "include_excluded": include_excluded,
            "notice": "予測対象外の実績掲載日は通常表示から除外しています。",
        }
        _cache_set(ckey, result)
        return result
    except Exception as e:
        return {"month": month, "events": {}, "error": str(e)}


@router.get("/api/events/day", tags=["events"])
def get_event_day(
    date_str: str = Query(..., description="YYYY-MM-DD"),
    hall_name: Optional[str] = Query(None),
    include_excluded: bool = Query(False),
) -> dict:
    """特定日のイベント＋みんレポ実績データを返す"""
    ckey = f"event_day_v3:{date_str}:{hall_name}:{include_excluded}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    try:
        conn = _get_event_conn()
        q = """SELECT id, hall_name, event_type, event_title, source, source_url,
                      event_kind, source_trust, prediction_eligible,
                      classification_reason
                 FROM hall_event WHERE event_date=?"""
        params: list = [date_str]
        if not include_excluded:
            q += " AND prediction_eligible=1"
        if hall_name:
            q += " AND hall_name=?"
            params.append(hall_name)
        events = [dict(r) for r in conn.execute(q, params).fetchall()]
        conn.close()

        # みんレポ実績 (hall_day_machine)
        results = []
        rconn = _get_reports_conn()
        if rconn:
            try:
                rq = "SELECT hall_name, machine_name, avg_diff_coins, unit_count FROM hall_day_machine WHERE report_date=?"
                rparams: list = [date_str]
                if hall_name:
                    rq += " AND hall_name=?"
                    rparams.append(hall_name)
                rq += " ORDER BY avg_diff_coins DESC"
                results = [dict(r) for r in rconn.execute(rq, rparams).fetchall()]
            except Exception:
                pass
            rconn.close()

        result = {
            "date": date_str,
            "events": events,
            "results": results,
            "include_excluded": include_excluded,
        }
        _cache_set(ckey, result)
        return result
    except Exception as e:
        return {"date": date_str, "events": [], "results": [], "error": str(e)}


@router.get("/api/events/strength", tags=["events"])
def get_event_strength(hall_name: Optional[str] = Query(None)) -> list[dict]:
    """イベントタイプ別の強度分析（イベント日 vs 通常日の差枚比較）"""
    try:
        rconn = _get_reports_conn()
        econn = _get_event_conn()
        if not rconn:
            return []

        # イベントがある日付を全取得
        eq = """SELECT hall_name, event_date, event_type, source_trust
                  FROM hall_event WHERE prediction_eligible=1"""
        eparams: list = []
        if hall_name:
            eq += " AND hall_name=?"
            eparams.append(hall_name)
        event_rows = econn.execute(eq, eparams).fetchall()
        econn.close()

        if not event_rows:
            rconn.close()
            return []

        from collections import defaultdict
        type_data: dict = defaultdict(lambda: {"event_diffs": [], "normal_diffs": []})

        # hall_day_machineから全日付の平均差枚を取得
        rq = "SELECT hall_name, report_date, AVG(avg_diff_coins) as avg_diff FROM hall_day_machine WHERE avg_diff_coins IS NOT NULL GROUP BY hall_name, report_date"
        rparams2: list = []
        if hall_name:
            rq += " HAVING hall_name=?"
            rparams2.append(hall_name)
        day_avgs = {(r[0], r[1]): r[2] for r in rconn.execute(rq, rparams2).fetchall()}
        rconn.close()

        event_days: set = set()
        for er in event_rows:
            key = (er["hall_name"], er["event_date"])
            event_days.add(key)
            if key in day_avgs:
                type_data[er["event_type"]]["event_diffs"].append(day_avgs[key])

        # 通常日（イベントなし日）
        for (hname, rdate), avg in day_avgs.items():
            if (hname, rdate) not in event_days:
                for et in type_data:
                    type_data[et]["normal_diffs"].append(avg)

        result = []
        for etype, data in type_data.items():
            ev_diffs = data["event_diffs"]
            no_diffs = data["normal_diffs"]
            if not ev_diffs:
                continue
            avg_ev = sum(ev_diffs) / len(ev_diffs)
            avg_no = sum(no_diffs) / len(no_diffs) if no_diffs else 0
            result.append({
                "event_type": etype,
                "event_days": len(ev_diffs),
                "avg_diff_event": round(avg_ev),
                "avg_diff_normal": round(avg_no),
                "diff_vs_normal": round(avg_ev - avg_no),
                "win_rate_event": round(sum(1 for v in ev_diffs if v > 0) / len(ev_diffs) * 100),
                "strength_score": round((avg_ev - avg_no) / max(abs(avg_no), 1) * 100),
            })
        result.sort(key=lambda x: x["diff_vs_normal"], reverse=True)
        return result
    except Exception as e:
        return [{"error": str(e)}]


@router.get("/api/events/quality", tags=["events"])
def get_event_quality(hall_name: Optional[str] = Query(None)) -> dict:
    """イベントDBの予測対象・隔離件数と取得元品質を返す。"""
    conn = _get_event_conn()
    try:
        where = " WHERE hall_name=?" if hall_name else ""
        params = [hall_name] if hall_name else []
        summary = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN prediction_eligible=1 THEN 1 ELSE 0 END) AS eligible,
                   SUM(CASE WHEN prediction_eligible=0 THEN 1 ELSE 0 END) AS excluded,
                   SUM(CASE WHEN event_kind='report_day' THEN 1 ELSE 0 END) AS report_days
              FROM hall_event{where}
            """,
            params,
        ).fetchone()
        by_source = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT COALESCE(source, '不明') AS source,
                       source_trust,
                       COUNT(*) AS records,
                       SUM(CASE WHEN prediction_eligible=1 THEN 1 ELSE 0 END) AS eligible
                  FROM hall_event{where}
                 GROUP BY source, source_trust
                 ORDER BY records DESC
                """,
                params,
            ).fetchall()
        ]
        evidence_where = " WHERE hall_name=?" if hall_name else ""
        evidence_summary = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN analysis_eligible=1 THEN 1 ELSE 0 END) AS eligible,
                   SUM(CASE WHEN evidence_scope='schedule_only' THEN 1 ELSE 0 END) AS schedule_only
              FROM hall_event_evidence{evidence_where}
            """,
            params,
        ).fetchone()
        day_summary = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   COUNT(DISTINCT hall_name) AS halls
              FROM hall_source_day_summary{evidence_where}
             {"AND" if evidence_where else "WHERE"} analysis_eligible=1
            """,
            params,
        ).fetchone()
        eligible = int(summary["eligible"] or 0)
        excluded = int(summary["excluded"] or 0)
        return {
            "hall_name": hall_name,
            "total": int(summary["total"] or 0),
            "eligible": eligible,
            "excluded": excluded,
            "report_days": int(summary["report_days"] or 0),
            "by_source": by_source,
            "event_evidence": {
                "total": int(evidence_summary["total"] or 0),
                "eligible": int(evidence_summary["eligible"] or 0),
                "schedule_only": int(evidence_summary["schedule_only"] or 0),
            },
            "day_summaries": {
                "records": int(day_summary["total"] or 0),
                "halls": int(day_summary["halls"] or 0),
            },
            "message": (
                f"予測対象{eligible}件、実績掲載日など{excluded}件を安全のため隔離中です。"
            ),
        }
    finally:
        conn.close()


def _event_analysis_grade(matched_days: int, lower_bound: int, lift: int, positive_rate: int) -> tuple[str, str]:
    if matched_days < 3:
        return "未検証", "実績3回未満"
    if matched_days >= 5 and lower_bound >= 100 and positive_rate >= 60:
        return "S", "安全側でも通常日を100枚以上上回る"
    if matched_days >= 5 and lower_bound >= 30 and positive_rate >= 55:
        return "A", "安全側でも通常日を上回る"
    if lift > 0:
        return "B", "平均では通常日を上回るが、まだ変動が大きい"
    if lift >= -50:
        return "C", "通常日と大差なし"
    return "D", "通常日を下回る回収傾向"


def _recurring_event_dates(title: str, start: date, end: date) -> tuple[set[str], Optional[str]]:
    """公開タイトルに明示された定例日だけを過去検証用に再現する。"""
    normalized = unicodedata.normalize("NFKC", title)
    digit_match = re.search(r"([0-9])\s*のつく日", normalized)
    monthly_match = re.search(r"毎月\s*([0-9]{1,2})\s*日", normalized)
    if digit_match:
        final_digit = int(digit_match.group(1))
        matcher = lambda current: current.day % 10 == final_digit
        basis = f"公開された『{final_digit}のつく日』を直近1年で再現"
    elif monthly_match and 1 <= int(monthly_match.group(1)) <= 31:
        target_day = int(monthly_match.group(1))
        matcher = lambda current: current.day == target_day
        basis = f"公開された『毎月{target_day}日』を直近1年で再現"
    else:
        return set(), None

    values: set[str] = set()
    current = start
    while current <= end:
        if matcher(current):
            values.add(current.isoformat())
        current += timedelta(days=1)
    return values, basis


def _wilson_lower_pct(successes: int, trials: int, z: float = 1.96) -> Optional[int]:
    if trials <= 0:
        return None
    ratio = successes / trials
    denominator = 1 + z * z / trials
    centre = ratio + z * z / (2 * trials)
    margin = z * math.sqrt((ratio * (1 - ratio) + z * z / (4 * trials)) / trials)
    return round(max(0.0, (centre - margin) / denominator) * 100)


def _event_quality_gate(
    *,
    matched_days: int,
    normal_days: int,
    direct_event_days: int,
    eligible_evidence_records: int,
    latest_matched_date: Optional[str],
    source_trust: str,
    backtest: dict,
    reference: date,
) -> dict:
    """予測前にデータ量・対象範囲・鮮度を検査し、弱い根拠を隔離する。"""
    sample_score = min(20, round(matched_days / 5 * 20))
    baseline_score = min(15, round(normal_days / 20 * 15))
    scope_score = (
        min(25, 15 + direct_event_days * 2)
        if direct_event_days
        else min(8, eligible_evidence_records * 2)
    )
    recommended_days = int(backtest.get("recommended_days") or 0)
    lower_bound = int(backtest.get("lower_bound_pct") or 0)
    backtest_score = min(20, round(recommended_days / 5 * 12) + round(lower_bound / 100 * 8))
    trust_score = {"A": 10, "B": 8, "C": 6}.get(source_trust, 0)
    freshness_days: Optional[int] = None
    freshness_score = 0
    if latest_matched_date:
        try:
            freshness_days = max(0, (reference - date.fromisoformat(latest_matched_date)).days)
            freshness_score = 10 if freshness_days <= 45 else 6 if freshness_days <= 90 else 2 if freshness_days <= 180 else 0
        except ValueError:
            freshness_days = None
    score = min(100, sample_score + baseline_score + scope_score + backtest_score + trust_score + freshness_score)

    blockers: list[str] = []
    warnings: list[str] = []
    if matched_days < 3:
        blockers.append("同条件の結果が3日未満")
    if normal_days < 10:
        blockers.append("比較する通常日が10日未満")
    if direct_event_days < 3:
        blockers.append("店舗全体の結果が3日未満")
    if recommended_days < 5:
        blockers.append("先読み答え合わせが5回未満")
    if source_trust not in {"A", "B", "C"}:
        blockers.append("予定の取得元が未確認")
    if freshness_days is None:
        blockers.append("最終実績日を確認できない")
    elif freshness_days > 90:
        blockers.append("実績が90日より古い")
    if eligible_evidence_records == 0:
        warnings.append("イベント名と同日の店全体結果を直接照合できていません")
    if direct_event_days and direct_event_days < matched_days:
        warnings.append("一部の日は機種別公開実績を台数加重しています")
    passed = not blockers and score >= 70
    return {
        "passed": passed,
        "status": "予測利用可" if passed else "参考止まり",
        "quality_score": score,
        "quality_label": "高" if score >= 80 else "中" if score >= 60 else "低",
        "matched_days": matched_days,
        "normal_days": normal_days,
        "direct_event_days": direct_event_days,
        "eligible_evidence_records": eligible_evidence_records,
        "freshness_days": freshness_days,
        "blockers": blockers,
        "warnings": warnings,
        "definition": "同条件3日・通常日10日・店全体結果3日・先読み5回・90日以内をすべて満たすと予測利用可",
    }


def _event_baseline_forecast(grade: str, lift: int, positive_rate: int, quality_gate: dict, backtest: dict) -> dict:
    """説明可能な固定式。AI導入前の比較基準として保持する。"""
    grade_base = {"S": 82, "A": 72, "B": 55, "C": 38, "D": 15, "未検証": 20}.get(grade, 20)
    score = grade_base
    score += max(-8, min(8, round(lift / 75)))
    score += max(-5, min(5, round((positive_rate - 50) / 5)))
    score += max(-5, min(5, round(((backtest.get("lower_bound_pct") or 0) - 50) / 10)))
    score += max(-5, min(5, round((quality_gate.get("quality_score", 0) - 70) / 6)))
    if not quality_gate.get("passed"):
        score = min(score, 49)
    score = max(0, min(100, score))
    if quality_gate.get("passed") and grade in {"S", "A"} and int(backtest.get("lower_bound_pct") or 0) >= 40:
        decision = "実戦候補"
    elif quality_gate.get("passed") and grade in {"S", "A", "B"}:
        decision = "要確認"
    else:
        decision = "参考止まり"
    return {
        "model": "説明可能ベースライン v1",
        "score": score,
        "decision": decision,
        "is_actionable": decision == "実戦候補",
        "definition": "通常日比・プラス率・先読み95%下限・データ品質だけを使用。未来の結果は不使用",
    }


def _event_walk_forward(
    hall_name: str,
    historical_dates: list[str],
    hall_day_avg: dict[tuple[str, str], float],
    excluded_dates: set[str],
) -> dict:
    """各開催日より前の情報だけで強い日を予測し、後から答え合わせする。"""
    observations = [
        (day, hall_day_avg[(hall_name, day)])
        for day in historical_dates
        if (hall_name, day) in hall_day_avg
    ]
    answers = []
    for index in range(3, len(observations)):
        report_date, actual = observations[index]
        prior_event_values = [value for _, value in observations[:index]]
        prior_normal_values = [
            value for (hall, day), value in hall_day_avg.items()
            if hall == hall_name and day < report_date and day not in excluded_dates
        ]
        if len(prior_normal_values) < 10:
            continue
        predicted_lift = round(
            sum(prior_event_values) / len(prior_event_values)
            - sum(prior_normal_values) / len(prior_normal_values)
        )
        prior_positive_rate = round(
            sum(value > 0 for value in prior_event_values) / len(prior_event_values) * 100
        )
        recommended = predicted_lift >= 30 and prior_positive_rate >= 55
        answers.append({
            "event_date": report_date,
            "recommended": recommended,
            "predicted_lift": predicted_lift,
            "actual_avg_diff": round(actual),
            "hit": bool(recommended and actual > 0),
        })
    recommended_answers = [answer for answer in answers if answer["recommended"]]
    hits = sum(answer["hit"] for answer in recommended_answers)
    return {
        "evaluated_days": len(answers),
        "recommended_days": len(recommended_answers),
        "hits": hits,
        "success_pct": round(hits / len(recommended_answers) * 100) if recommended_answers else None,
        "lower_bound_pct": _wilson_lower_pct(hits, len(recommended_answers)),
        "status": "検証済み" if len(recommended_answers) >= 5 else "データ不足",
        "recent_answers": recommended_answers[-5:][::-1],
        "definition": "各開催日より前の実績だけで推奨し、当日の店舗平均差枚がプラスなら成功",
    }


def _event_model_comparison(analysis: dict) -> dict:
    """イベントパターン単位の先読み結果を集計し、モデル採用可否を安全側で判定する。"""
    backtests = [
        item.get("backtest") or {}
        for item in analysis.get("event_analysis") or []
    ]
    evaluated_trials = sum(int(item.get("evaluated_days") or 0) for item in backtests)
    recommended_trials = sum(int(item.get("recommended_days") or 0) for item in backtests)
    hits = sum(int(item.get("hits") or 0) for item in backtests)
    success_pct = round(hits / recommended_trials * 100) if recommended_trials else None
    lower_bound_pct = _wilson_lower_pct(hits, recommended_trials)
    minimum_trials = 30
    baseline_ready = recommended_trials >= minimum_trials
    baseline = {
        "model": "説明可能ベースライン v1",
        "role": "現在の判定モデル",
        "evaluated_trials": evaluated_trials,
        "recommended_trials": recommended_trials,
        "hits": hits,
        "success_pct": success_pct,
        "lower_bound_pct": lower_bound_pct,
        "status": "継続検証中" if baseline_ready else "検証件数不足",
        "minimum_trials": minimum_trials,
    }
    candidate = {
        "model": "AI予測候補",
        "role": "未導入（現在のAIは説明専用）",
        "evaluated_trials": 0,
        "recommended_trials": 0,
        "hits": 0,
        "success_pct": None,
        "lower_bound_pct": None,
        "status": "比較対象なし",
        "changed_decisions": 0,
    }
    blockers = ["AI予測候補はまだ判定を出していません"]
    if not baseline_ready:
        blockers.append(f"同一定義のベースライン推奨が{minimum_trials}件未満")
    return {
        "active_model": baseline["model"],
        "baseline": baseline,
        "candidate": candidate,
        "promotion_gate": {
            "passed": False,
            "status": "現行モデルを維持",
            "minimum_candidate_recommendations": minimum_trials,
            "required_lower_bound_improvement_points": 5,
            "blockers": blockers,
            "definition": "同じ期間・同じ成功定義で30件以上を先読み検証し、95%下限が現行モデルを5ポイント以上上回る場合だけ採用審査",
        },
        "trial_unit": "店舗×イベントパターン×開催日。未来の実績は予測時点で使用しません",
    }


def _save_event_model_evaluation(
    comparison: dict, *, region: str, reference_date: str,
    evaluated_on: Optional[date] = None,
) -> None:
    baseline = comparison["baseline"]
    conn = _get_event_conn()
    try:
        conn.execute(
            """
            INSERT INTO event_model_evaluation
              (model_name,evaluated_on,region,reference_date,evaluated_trials,
               recommended_trials,hits,success_pct,lower_bound_pct,promotion_status,detail_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(model_name,evaluated_on,region) DO UPDATE SET
              reference_date=excluded.reference_date,
              evaluated_trials=excluded.evaluated_trials,
              recommended_trials=excluded.recommended_trials,
              hits=excluded.hits,
              success_pct=excluded.success_pct,
              lower_bound_pct=excluded.lower_bound_pct,
              promotion_status=excluded.promotion_status,
              detail_json=excluded.detail_json,
              created_at=datetime('now','localtime')
            """,
            (
                baseline["model"], (evaluated_on or date.today()).isoformat(), region, reference_date,
                baseline["evaluated_trials"], baseline["recommended_trials"],
                baseline["hits"], baseline["success_pct"], baseline["lower_bound_pct"],
                comparison["promotion_gate"]["status"],
                json.dumps(comparison, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _record_event_predictions(analysis: dict, *, region: str, prediction_date: date) -> int:
    """翌日の本番判定を固定保存する。同じ対象日の初回判定は後から上書きしない。"""
    target_date = (prediction_date + timedelta(days=1)).isoformat()
    rows = [
        item for item in analysis.get("upcoming") or []
        if item.get("event_date") == target_date
    ]
    if not rows:
        return 0
    conn = _get_event_conn()
    saved = 0
    try:
        for item in rows:
            forecast = item.get("baseline_forecast") or {}
            gate = item.get("quality_gate") or {}
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO event_prediction_log
                  (model_name,prediction_date,target_date,region,hall_name,event_name,
                   decision,score,quality_passed,snapshot_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    forecast.get("model") or "説明可能ベースライン v1",
                    prediction_date.isoformat(), target_date, region,
                    item.get("hall_name") or "", item.get("event_name") or "イベント",
                    forecast.get("decision") or "参考止まり",
                    int(forecast.get("score") or 0), int(bool(gate.get("passed"))),
                    json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            saved += int(cursor.rowcount > 0)
        conn.commit()
    finally:
        conn.close()
    return saved


def _resolve_event_prediction_outcomes(as_of: date) -> int:
    """本番予測後に公開された店全体結果だけで答え合わせする。"""
    conn = _get_event_conn()
    try:
        pending = [
            dict(row) for row in conn.execute(
                """
                SELECT id,hall_name,target_date
                  FROM event_prediction_log
                 WHERE outcome_success IS NULL AND target_date<=?
                """,
                (as_of.isoformat(),),
            ).fetchall()
        ]
        if not pending:
            return 0
        reports = _get_reports_conn()
        if reports is None:
            return 0
        try:
            has_summary = bool(reports.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hall_source_day_summary'"
            ).fetchone())
            has_machine_rows = bool(reports.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hall_day_machine'"
            ).fetchone())
            resolved = 0
            for item in pending:
                value: Optional[float] = None
                source = ""
                if has_summary:
                    row = reports.execute(
                        """
                        SELECT avg_diff_coins,source
                          FROM hall_source_day_summary
                         WHERE hall_name=? AND report_date=? AND analysis_eligible=1
                           AND avg_diff_coins IS NOT NULL
                         ORDER BY CASE source_trust WHEN 'A' THEN 1 WHEN 'B' THEN 2
                                  WHEN 'C' THEN 3 ELSE 4 END
                         LIMIT 1
                        """,
                        (item["hall_name"], item["target_date"]),
                    ).fetchone()
                    if row:
                        value = float(row["avg_diff_coins"])
                        source = f"{row['source']}・店舗全体"
                if value is None and has_machine_rows:
                    rows = reports.execute(
                        """
                        SELECT avg_diff_coins,unit_count
                          FROM hall_day_machine
                         WHERE hall_name=? AND report_date=? AND avg_diff_coins IS NOT NULL
                        """,
                        (item["hall_name"], item["target_date"]),
                    ).fetchall()
                    if rows:
                        total_units = sum(max(1, int(row["unit_count"] or 1)) for row in rows)
                        value = sum(
                            float(row["avg_diff_coins"]) * max(1, int(row["unit_count"] or 1))
                            for row in rows
                        ) / total_units
                        source = "機種別公開実績を台数加重"
                if value is None:
                    continue
                conn.execute(
                    """
                    UPDATE event_prediction_log
                       SET outcome_avg_diff_coins=?, outcome_success=?, outcome_source=?,
                           resolved_at=datetime('now','localtime')
                     WHERE id=?
                    """,
                    (round(value), int(value > 0), source, item["id"]),
                )
                resolved += 1
            conn.commit()
            return resolved
        finally:
            reports.close()
    finally:
        conn.close()


def _event_production_audit(region: str) -> dict:
    conn = _get_event_conn()
    try:
        summary = conn.execute(
            """
            SELECT COUNT(*) AS predictions,
                   SUM(CASE WHEN outcome_success IS NULL THEN 1 ELSE 0 END) AS unresolved,
                   SUM(CASE WHEN decision='実戦候補' AND outcome_success IS NOT NULL THEN 1 ELSE 0 END) AS recommended_resolved,
                   SUM(CASE WHEN decision='実戦候補' AND outcome_success=1 THEN 1 ELSE 0 END) AS hits
              FROM event_prediction_log
             WHERE model_name='説明可能ベースライン v1' AND region=?
            """,
            (region,),
        ).fetchone()
    finally:
        conn.close()
    recommended = int(summary["recommended_resolved"] or 0)
    hits = int(summary["hits"] or 0)
    return {
        "predictions": int(summary["predictions"] or 0),
        "unresolved": int(summary["unresolved"] or 0),
        "recommended_resolved": recommended,
        "hits": hits,
        "success_pct": round(hits / recommended * 100) if recommended else None,
        "lower_bound_pct": _wilson_lower_pct(hits, recommended),
        "status": "本番検証中" if recommended < 30 else "本番評価可能",
        "definition": "前日に固定保存した実戦候補だけを、後日公開された店舗平均差枚がプラスかで答え合わせ",
    }


def run_event_model_evaluation(
    region: Literal["all", "shijonawate", "matsumoto_shiojiri", "nagano", "osaka"] = "shijonawate",
    evaluation_date: Optional[date] = None,
) -> dict:
    """日次スケジューラーから呼ぶ自動答え合わせ。"""
    as_of = evaluation_date or date.today()
    resolved = _resolve_event_prediction_outcomes(as_of)
    analysis = get_event_analysis(
        visit_date=(as_of + timedelta(days=1)).isoformat(),
        region=region,
        hall_name=None,
        history_days=730,
        future_days=31,
    )
    recorded = _record_event_predictions(analysis, region=region, prediction_date=as_of)
    comparison = _event_model_comparison(analysis)
    comparison["production_audit"] = _event_production_audit(region)
    comparison["daily_run"] = {"recorded": recorded, "resolved": resolved}
    _save_event_model_evaluation(
        comparison, region=region, reference_date=analysis["reference_date"],
        evaluated_on=as_of,
    )
    return comparison


@router.get("/api/events/analysis", tags=["events"])
def get_event_analysis(
    visit_date: str = Query(..., description="YYYY-MM-DD"),
    region: Literal["all", "shijonawate", "matsumoto_shiojiri", "nagano", "osaka"] = "shijonawate",
    hall_name: Optional[str] = Query(None),
    history_days: int = Query(730, ge=90, le=1460),
    future_days: int = Query(31, ge=1, le=90),
) -> dict:
    """イベント名×店舗×スマスロ機種の過去成績と今後の予定を返す。"""
    try:
        target = date.fromisoformat(visit_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="visit_date は YYYY-MM-DD 形式です") from exc
    reference = min(target - timedelta(days=1), date.today())
    history_start = reference - timedelta(days=history_days - 1)
    future_end = target + timedelta(days=future_days - 1)

    econn = _get_event_conn()
    evidence_rows: list[dict] = []
    try:
        query = """
            SELECT hall_name,event_date,event_type,event_title,source,source_url,
                   event_kind,source_trust
              FROM hall_event
             WHERE prediction_eligible=1
               AND event_date>=? AND event_date<=?
        """
        params: list = [history_start.isoformat(), future_end.isoformat()]
        if hall_name:
            query += " AND hall_name=?"
            params.append(hall_name)
        event_rows = [dict(row) for row in econn.execute(query, params).fetchall()]
        evidence_query = """
            SELECT hall_name,event_date,event_name,evidence_scope,avg_diff_coins,
                   total_diff_coins,unit_count,source_trust,analysis_eligible,
                   quality_reason,source_url
              FROM hall_event_evidence
             WHERE event_date>=? AND event_date<=?
        """
        evidence_params: list = [history_start.isoformat(), reference.isoformat()]
        if hall_name:
            evidence_query += " AND hall_name=?"
            evidence_params.append(hall_name)
        evidence_rows = [
            dict(row) for row in econn.execute(evidence_query, evidence_params).fetchall()
        ]
    finally:
        econn.close()
    event_rows = [
        row for row in event_rows
        if region_matches(row["hall_name"], None, region)
    ]
    evidence_rows = [
        row for row in evidence_rows
        if region_matches(row["hall_name"], None, region)
    ]

    rconn = _get_reports_conn()
    performance_rows: list[dict] = []
    direct_summary_rows: list[dict] = []
    if rconn is not None:
        try:
            query = """
                SELECT hall_name,report_date,machine_name,avg_diff_coins,unit_count
                  FROM hall_day_machine
                 WHERE avg_diff_coins IS NOT NULL
                   AND report_date>=? AND report_date<=?
            """
            params = [history_start.isoformat(), reference.isoformat()]
            if hall_name:
                query += " AND hall_name=?"
                params.append(hall_name)
            performance_rows = [dict(row) for row in rconn.execute(query, params).fetchall()]
            table_exists = rconn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hall_source_day_summary'"
            ).fetchone()
            if table_exists:
                summary_query = """
                    SELECT hall_name,report_date,avg_diff_coins,unit_count,source,
                           source_trust,evidence_scope
                      FROM hall_source_day_summary
                     WHERE analysis_eligible=1
                       AND avg_diff_coins IS NOT NULL
                       AND report_date>=? AND report_date<=?
                """
                summary_params: list = [history_start.isoformat(), reference.isoformat()]
                if hall_name:
                    summary_query += " AND hall_name=?"
                    summary_params.append(hall_name)
                direct_summary_rows = [
                    dict(row) for row in rconn.execute(summary_query, summary_params).fetchall()
                ]
        finally:
            rconn.close()
    performance_rows = [
        row for row in performance_rows
        if region_matches(row["hall_name"], None, region)
    ]
    direct_summary_rows = [
        row for row in direct_summary_rows
        if region_matches(row["hall_name"], None, region)
    ]

    def event_name(row: dict) -> str:
        title = re.sub(r"\s*（[A-Z]ランク）\s*$", "", str(row.get("event_title") or "")).strip()
        return title or str(row.get("event_type") or "イベント")

    trust_order = {"A": 4, "B": 3, "C": 2, "D": 1, "未確認": 0}
    event_groups: dict[tuple[str, str], dict] = {}
    all_event_dates: dict[str, set[str]] = defaultdict(set)
    for row in event_rows:
        name = event_name(row)
        key = (row["hall_name"], name)
        group = event_groups.setdefault(key, {
            "hall_name": row["hall_name"], "event_name": name,
            "event_type": row.get("event_type") or "その他", "dates": set(),
            "sources": set(), "source_urls": set(), "source_trust": row.get("source_trust") or "未確認",
        })
        group["dates"].add(row["event_date"])
        all_event_dates[row["hall_name"]].add(row["event_date"])
        if row.get("source"):
            group["sources"].add(row["source"])
        if row.get("source_url"):
            group["source_urls"].add(row["source_url"])
        if trust_order.get(row.get("source_trust"), 0) > trust_order.get(group["source_trust"], 0):
            group["source_trust"] = row["source_trust"]

    # 公開ページが今後分しか保持しない定例イベントは、タイトルに明示された
    # 日付規則だけを直近1年へ展開する。単発取材や曖昧なイベントは補完しない。
    recurring_start = max(history_start, reference - timedelta(days=364))
    for group in event_groups.values():
        inferred_dates, history_basis = _recurring_event_dates(
            group["event_name"], recurring_start, reference
        )
        group["dates"].update(inferred_dates)
        group["inferred_dates"] = inferred_dates
        group["history_basis"] = history_basis or "記録済みの公開予定だけを使用"
        all_event_dates[group["hall_name"]].update(inferred_dates)

    hall_day_values: dict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)
    machine_rows_by_hall: dict[str, list[dict]] = defaultdict(list)
    for row in performance_rows:
        units = max(1, int(row.get("unit_count") or 1))
        hall_day_values[(row["hall_name"], row["report_date"])].append(
            (float(row["avg_diff_coins"]), units)
        )
        if is_smartslot_machine(row["machine_name"]):
            machine_rows_by_hall[row["hall_name"]].append(row)
    hall_day_avg = {
        key: sum(value * units for value, units in values) / sum(units for _, units in values)
        for key, values in hall_day_values.items()
    }
    direct_summary_keys: set[tuple[str, str]] = set()
    for row in direct_summary_rows:
        key = (row["hall_name"], row["report_date"])
        hall_day_avg[key] = float(row["avg_diff_coins"])
        direct_summary_keys.add(key)

    evidence_by_group: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in evidence_rows:
        evidence_by_group[(row["hall_name"], row["event_name"])].append(row)

    analyses = []
    analysis_by_key: dict[tuple[str, str], dict] = {}
    for key, group in event_groups.items():
        historical_dates = sorted(day for day in group["dates"] if day <= reference.isoformat())
        event_values = [
            hall_day_avg[(group["hall_name"], day)]
            for day in historical_dates
            if (group["hall_name"], day) in hall_day_avg
        ]
        normal_values = [
            value for (hall, day), value in hall_day_avg.items()
            if hall == group["hall_name"] and day not in all_event_dates[hall]
        ]
        event_avg = round(sum(event_values) / len(event_values)) if event_values else 0
        normal_avg = round(sum(normal_values) / len(normal_values)) if normal_values else 0
        lift = event_avg - normal_avg if event_values else 0
        positive_rate = round(sum(value > 0 for value in event_values) / len(event_values) * 100) if event_values else 0
        relative_values = [value - normal_avg for value in event_values]
        if len(relative_values) >= 2:
            mean = sum(relative_values) / len(relative_values)
            variance = sum((value - mean) ** 2 for value in relative_values) / (len(relative_values) - 1)
            lower_bound = round(mean - 1.96 * math.sqrt(variance / len(relative_values)))
        else:
            lower_bound = lift
        grade, grade_reason = _event_analysis_grade(len(event_values), lower_bound, lift, positive_rate)
        backtest = _event_walk_forward(
            group["hall_name"], historical_dates, hall_day_avg,
            all_event_dates[group["hall_name"]],
        )
        group_evidence = evidence_by_group.get(key, [])
        eligible_evidence_records = sum(
            int(row.get("analysis_eligible") or 0) for row in group_evidence
        )
        matched_dates = [
            day for day in historical_dates
            if (group["hall_name"], day) in hall_day_avg
        ]
        direct_event_days = sum(
            (group["hall_name"], day) in direct_summary_keys for day in matched_dates
        )
        quality_gate = _event_quality_gate(
            matched_days=len(event_values),
            normal_days=len(normal_values),
            direct_event_days=direct_event_days,
            eligible_evidence_records=eligible_evidence_records,
            latest_matched_date=matched_dates[-1] if matched_dates else None,
            source_trust=group["source_trust"],
            backtest=backtest,
            reference=reference,
        )
        baseline_forecast = _event_baseline_forecast(
            grade, lift, positive_rate, quality_gate, backtest
        )

        machine_stats: dict[str, dict] = {}
        hall_event_dates = set(historical_dates)
        for row in machine_rows_by_hall.get(group["hall_name"], []):
            machine_key = normalize_machine_key(row["machine_name"])
            if not machine_key:
                continue
            item = machine_stats.setdefault(machine_key, {
                "machine_name": clean_machine_display_name(row["machine_name"]),
                "event_values": [], "normal_values": [],
            })
            value = float(row["avg_diff_coins"])
            if row["report_date"] in hall_event_dates:
                item["event_values"].append(value)
            elif row["report_date"] not in all_event_dates[group["hall_name"]]:
                item["normal_values"].append(value)
        machines = []
        for item in machine_stats.values():
            if not item["event_values"]:
                continue
            machine_event_avg = round(sum(item["event_values"]) / len(item["event_values"]))
            machine_normal_avg = round(sum(item["normal_values"]) / len(item["normal_values"])) if item["normal_values"] else 0
            machines.append({
                "machine_name": item["machine_name"],
                "matched_days": len(item["event_values"]),
                "event_avg_diff": machine_event_avg,
                "normal_avg_diff": machine_normal_avg,
                "lift_vs_normal": machine_event_avg - machine_normal_avg,
                "positive_rate_pct": round(sum(value > 0 for value in item["event_values"]) / len(item["event_values"]) * 100),
                "status": "参考" if len(item["event_values"]) >= 3 else "データ不足",
            })
        machines.sort(key=lambda item: (item["status"] == "参考", item["lift_vs_normal"], item["matched_days"]), reverse=True)

        upcoming_dates = sorted(day for day in group["dates"] if target.isoformat() <= day <= future_end.isoformat())
        analysis = {
            "hall_name": group["hall_name"],
            "event_name": group["event_name"],
            "event_type": group["event_type"],
            "source_trust": group["source_trust"],
            "sources": sorted(group["sources"]),
            "source_urls": sorted(group["source_urls"])[:5],
            "scheduled_history_days": len(historical_dates),
            "matched_days": len(event_values),
            "inferred_history_days": len(group.get("inferred_dates") or set()),
            "history_basis": group.get("history_basis") or "記録済みの公開予定だけを使用",
            "backtest": backtest,
            "evidence_records": len(group_evidence),
            "eligible_evidence_records": eligible_evidence_records,
            "quality_gate": quality_gate,
            "baseline_forecast": baseline_forecast,
            "performance_basis": (
                "店舗全体の日別差枚を優先" if any(
                    (group["hall_name"], day) in direct_summary_keys
                    for day in historical_dates
                ) else "機種別公開実績を台数加重"
            ),
            "event_avg_diff": event_avg,
            "normal_avg_diff": normal_avg,
            "lift_vs_normal": lift,
            "lower_bound_lift": lower_bound,
            "positive_rate_pct": positive_rate,
            "grade": grade,
            "grade_reason": grade_reason,
            "latest_historical_date": historical_dates[-1] if historical_dates else None,
            "next_date": upcoming_dates[0] if upcoming_dates else None,
            "strong_machines": machines[:8],
            "notice": "実績3回未満は熱いイベントと断定しません。",
        }
        analyses.append(analysis)
        analysis_by_key[key] = analysis

    grade_rank = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1, "未検証": 0}
    analyses.sort(
        key=lambda item: (grade_rank.get(item["grade"], 0), item["lower_bound_lift"], item["matched_days"]),
        reverse=True,
    )
    upcoming = []
    seen_upcoming: set[tuple[str, str, str]] = set()
    for row in sorted(event_rows, key=lambda item: (item["event_date"], item["hall_name"])):
        if not (target.isoformat() <= row["event_date"] <= future_end.isoformat()):
            continue
        name = event_name(row)
        dedupe_key = (row["hall_name"], row["event_date"], name)
        if dedupe_key in seen_upcoming:
            continue
        seen_upcoming.add(dedupe_key)
        analysis = analysis_by_key.get((row["hall_name"], name), {})
        upcoming.append({
            "event_date": row["event_date"], "hall_name": row["hall_name"],
            "event_name": name, "event_type": row.get("event_type") or "その他",
            "source_trust": row.get("source_trust") or "未確認",
            "source": row.get("source") or "", "source_url": row.get("source_url") or "",
            "grade": analysis.get("grade", "未検証"),
            "matched_days": analysis.get("matched_days", 0),
            "lift_vs_normal": analysis.get("lift_vs_normal", 0),
            "positive_rate_pct": analysis.get("positive_rate_pct", 0),
            "backtest": analysis.get("backtest", {
                "recommended_days": 0, "success_pct": None, "lower_bound_pct": None,
                "status": "データ不足",
            }),
            "quality_gate": analysis.get("quality_gate", {
                "passed": False, "status": "参考止まり", "quality_score": 0,
                "blockers": ["過去実績なし"], "warnings": [],
            }),
            "baseline_forecast": analysis.get("baseline_forecast", {
                "model": "説明可能ベースライン v1", "score": 0,
                "decision": "参考止まり", "is_actionable": False,
            }),
        })

    upcoming.sort(
        key=lambda item: (
            item["event_date"],
            -int(item["baseline_forecast"].get("score") or 0),
            item["hall_name"],
        )
    )

    result = {
        "visit_date": visit_date,
        "region": region,
        "region_label": region_label(region),
        "history_start": history_start.isoformat(),
        "reference_date": reference.isoformat(),
        "upcoming": upcoming,
        "event_analysis": analyses,
        "summary": {
            "upcoming_count": len(upcoming),
            "analyzed_patterns": len(analyses),
            "verified_patterns": sum(item["grade"] != "未検証" for item in analyses),
            "strong_patterns": sum(item["grade"] in {"S", "A"} for item in analyses),
            "backtested_patterns": sum(
                item["backtest"]["status"] == "検証済み" for item in analyses
            ),
            "eligible_evidence_records": sum(
                int(row.get("analysis_eligible") or 0) for row in evidence_rows
            ),
            "quality_passed_patterns": sum(
                bool(item["quality_gate"]["passed"]) for item in analyses
            ),
            "actionable_upcoming": sum(
                bool(item["baseline_forecast"]["is_actionable"]) for item in upcoming
            ),
        },
        "notice": "従来集計：規則から補完した日を含み、予定の取得時点は未監査です。3.26の店舗×イベント比較・本番の事前成績とは別です。",
    }
    result["model_comparison"] = _event_model_comparison(result)
    result["model_comparison"]["production_audit"] = _event_production_audit(region)
    return result


@router.get("/api/events/model_comparison", tags=["events"])
def get_event_model_comparison(
    visit_date: str = Query(..., description="YYYY-MM-DD"),
    region: Literal["all", "shijonawate", "matsumoto_shiojiri", "nagano", "osaka"] = "shijonawate",
    history_days: int = Query(730, ge=90, le=1460),
) -> dict:
    """現行ベースラインと将来の予測候補を、同じ答え合わせ条件で比較する。"""
    analysis = get_event_analysis(
        visit_date=visit_date,
        region=region,
        hall_name=None,
        history_days=history_days,
        future_days=31,
    )
    comparison = _event_model_comparison(analysis)
    comparison["production_audit"] = _event_production_audit(region)
    conn = _get_event_conn()
    try:
        history = [
            dict(row)
            for row in conn.execute(
                """
                SELECT evaluated_on,reference_date,evaluated_trials,recommended_trials,
                       hits,success_pct,lower_bound_pct,promotion_status
                  FROM event_model_evaluation
                 WHERE model_name=? AND region=?
                 ORDER BY evaluated_on DESC
                 LIMIT 30
                """,
                (comparison["active_model"], region),
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "visit_date": visit_date,
        "reference_date": analysis["reference_date"],
        "region": region,
        "region_label": analysis["region_label"],
        **comparison,
        "evaluation_history": history,
        "notice": "見かけの的中率だけでなく95%下限と検証件数を使い、未来の結果を混ぜずに比較します。",
    }


@router.get("/api/events/scrape_status", tags=["events"])
def get_event_scrape_status() -> dict:
    """イベントスクレイプの進捗を返す"""
    halls = _EVENT_PROGRESS.get("halls", [])
    done = sum(1 for h in halls if h["status"] == "done")
    failed = sum(1 for h in halls if h["status"] == "failed")
    total = len(halls)
    current = next((h["name"] for h in halls if h["status"] == "running"), None)

    elapsed = 0
    if _EVENT_PROGRESS.get("started_at"):
        import datetime as _dt3
        try:
            elapsed = (_dt3.datetime.now() - _dt3.datetime.fromisoformat(
                _EVENT_PROGRESS["started_at"]
            )).total_seconds()
        except Exception:
            pass

    finished = done + failed
    eta_min = 0
    if finished > 0 and total > finished and elapsed > 0:
        eta_min = round(elapsed / finished * (total - finished) / 60)

    return {
        "running": _EVENT_PROGRESS.get("running", False),
        "total": total,
        "done": done,
        "failed": failed,
        "current_hall": current,
        "eta_min": eta_min,
        "halls": halls,
    }


@router.post("/api/events/scrape", tags=["events"])
def trigger_event_scrape(
    hall_name: Optional[str] = Query(None, description="Noneなら全ホール"),
    background_tasks: BackgroundTasks = ...,
) -> dict:
    """イベントスクレイプをバックグラウンドで実行"""
    if _EVENT_PROGRESS.get("running"):
        return {"ok": False, "message": "すでに実行中です"}

    import datetime as _dt_ev
    halls = [{"hall_name": hall_name}] if hall_name else scheduler._get_active_halls()
    hall_names = [h["hall_name"] if isinstance(h, dict) else h for h in halls]

    _EVENT_PROGRESS.update({
        "running": True,
        "started_at": _dt_ev.datetime.now().isoformat(),
        "halls": [{"name": n, "status": "waiting", "found": 0, "by_source": {}} for n in hall_names],
    })

    def _set_ev_hall(name: str, status: str, found: int = 0, by_source: dict = {}):
        for h in _EVENT_PROGRESS["halls"]:
            if h["name"] == name:
                h["status"] = status
                if found:
                    h["found"] = found
                if by_source:
                    h["by_source"] = by_source
                break

    def _run():
        from scraper.events import scrape_all
        try:
            for hname in hall_names:
                _set_ev_hall(hname, "running")
                try:
                    result = scrape_all(hname, save=True)
                    _set_ev_hall(hname, "done",
                                 found=result.get("total", 0),
                                 by_source=result.get("by_source", {}))
                except Exception as e:
                    _set_ev_hall(hname, "failed", by_source={"error": str(e)[:60]})
                time.sleep(2)
        finally:
            _EVENT_PROGRESS["running"] = False

    background_tasks.add_task(_run)
    return {"ok": True, "message": f"{len(hall_names)}店舗のイベント取得を開始しました"}

@router.post("/api/events/manual", tags=["events"])
def add_manual_event(
    hall_name: str = Query(...),
    event_date: str = Query(..., description="YYYY-MM-DD"),
    event_type: str = Query("その他"),
    event_title: str = Query(""),
) -> dict:
    """手動でイベントを登録"""
    try:
        from scraper.events import classify_event_record

        classification = classify_event_record("manual", event_title)
        conn = _get_event_conn()
        conn.execute("""
            INSERT OR IGNORE INTO hall_event
              (hall_name, event_date, event_type, event_title, source,
               event_kind, source_trust, prediction_eligible, classification_reason, created_at)
            VALUES (?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?)
        """, (
            hall_name, event_date, event_type, event_title,
            classification["event_kind"], classification["source_trust"],
            classification["prediction_eligible"],
            classification["classification_reason"],
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        conn.close()
        _cache_invalidate_prefix("event_")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/events/debug_scrape", tags=["events"])
def debug_event_scrape(
    hall_name: str = Query(...),
    source: str = Query("dste", description="dste | pworld | twitter | google"),
) -> dict:
    """スクレーパーのデバッグ: 何が取れているか確認用"""
    import traceback
    result = {"hall_name": hall_name, "source": source, "events": [], "debug": {}}
    try:
        import requests as _req, urllib.parse as _up
        from scraper.events import HEADERS, NITTER_INSTANCES, _get

        if source == "dste":
            from scraper.events import _dste_search, scrape_dste
            search_url = f"https://dste.jp/search/?q={_up.quote(hall_name)}"
            r0 = _get(search_url)
            result["debug"]["search_url"] = search_url
            result["debug"]["search_status"] = r0.status_code if r0 else "failed"
            result["debug"]["search_html"] = r0.text[:3000] if r0 else ""
            if r0:
                from bs4 import BeautifulSoup as _BS
                soup = _BS(r0.text, "html.parser")
                result["debug"]["all_links"] = [a.get("href","") for a in soup.select("a[href]")][:40]
            hall_url = _dste_search(hall_name)
            result["debug"]["hall_url"] = hall_url
            evs = scrape_dste(hall_name)
            result["events"] = evs

        elif source == "pworld":
            from scraper.events import _pworld_search, scrape_pworld
            hall_url = _pworld_search(hall_name)
            result["debug"]["hall_url"] = hall_url
            evs = scrape_pworld(hall_name)
            result["events"] = evs

        elif source == "twitter":
            result["debug"]["nitter_instances"] = NITTER_INSTANCES
            query = _up.quote(f"{hall_name} イベント")
            for inst in NITTER_INSTANCES[:3]:
                url = f"{inst}/search?q={query}&f=tweets"
                r = _get(url, timeout=10)
                result["debug"][f"{inst}_status"] = r.status_code if r else "failed"
                result["debug"][f"{inst}_html"] = r.text[:800] if r else ""
            from scraper.events import scrape_twitter
            evs = scrape_twitter(hall_name)
            result["events"] = evs

        elif source == "google":
            from scraper.events import scrape_google
            evs = scrape_google(hall_name)
            result["events"] = evs
    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()[-500:]
    return result


@router.delete("/api/events/{event_id}", tags=["events"])
def delete_event(event_id: int) -> dict:
    """イベントを削除"""
    try:
        conn = _get_event_conn()
        conn.execute("DELETE FROM hall_event WHERE id=?", (event_id,))
        conn.commit()
        conn.close()
        _cache_invalidate_prefix("event_")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

