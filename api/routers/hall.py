"""ホール分析エンドポイント (/api/hall/*, /api/machine/*)"""
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
from hall.prior import (
    DAITO_MACHINE_SCORES,
    DAITO_WEEKDAY_AVG,
    compute_prior,
    day_rating,
    machine_ranking,
)
from hall.machine_scope import is_smartslot_machine, machine_names_match
from hall.regions import region_label, region_matches
from hall.target_validation import (
    date_weighted_estimate,
    decide_action,
    walk_forward_backtest,
)
from records.models import list_sessions, session_to_dict

router = APIRouter()

_scrape_status: dict[str, str] = {}  # hall_name -> "idle"|"running"|"done"|"error"
_anaslo_scrape_status: dict[str, str] = {}


def _run_scrape(hall_name: str, days: int):
    """バックグラウンドスクレイプ処理（みんレポ）。"""
    global _scrape_status
    _scrape_status[hall_name] = "running"
    try:
        from scraper.minrepo import (
            build_tag_url, fetch_report_links,
            init_db, parse_date_from_text, scrape_report
        )
        conn = init_db()
        tag_url = build_tag_url(hall_name)
        max_pages = max(1, (days + 9) // 10)  # ~10件/ページ
        links = fetch_report_links(tag_url, max_pages=max_pages, expected_hall_name=hall_name)
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


def _run_minrepo_nightly(hall_list: list, days: int = 2) -> None:
    """夜間バッチ：みんレポを全ホール取得（直近days日分、取得済みはスキップ）"""
    logger.info(f"[みんレポ] 夜間バッチ開始: {len(hall_list)}店舗")
    for h in hall_list:
        hname = h["hall_name"] if isinstance(h, dict) else h
        try:
            _run_scrape(hname, days=days)
            logger.info(f"[みんレポ] {hname} 完了")
        except Exception as e:
            logger.warning(f"[みんレポ] {hname} エラー: {e}")
        time.sleep(3)
    logger.info("[みんレポ] 夜間バッチ完了")


def _run_anaslo_scrape(hall_name: str, days: int):
    _anaslo_scrape_status[hall_name] = "running"
    try:
        from scraper.anaslo import scrape_hall as anaslo_scrape
        anaslo_scrape(hall_name, days=days)
        _anaslo_scrape_status[hall_name] = "done"
    except Exception as e:
        _anaslo_scrape_status[hall_name] = f"error: {e}"


@router.get("/api/hall/prior", tags=["hall"])
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


@router.get("/api/hall/daito", tags=["hall"])
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


@router.get("/api/hall/day_rating", tags=["hall"])
def get_day_rating(
    hall_name: str = Query(...),
    weekday: int = Query(..., ge=0, le=6),
) -> dict:
    return day_rating(hall_name, weekday)


@router.get("/api/hall/machine_ranking", tags=["hall"])
def get_machine_ranking(hall_name: str = Query(...)) -> list[dict]:
    ckey = f"machine_ranking:{hall_name}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    result = machine_ranking(hall_name)
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/weekday_machine_stats", tags=["hall"])
def get_weekday_machine_stats(
    hall_name: str = Query(...),
    days: int = Query(90),
) -> list[dict]:
    """曜日×機種のクロス集計（どの曜日にどの機種が強いか）"""
    ckey = f"weekday_machine_stats:{hall_name}:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
    result = [
        {"weekday": dow_map.get(r[0], r[0]), "machine_name": r[1],
         "count": r[2], "avg_diff": r[3] or 0, "win_rate": r[4] or 0}
        for r in rows
    ]
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/today_machine_ranking", tags=["hall"])
def get_today_machine_ranking(
    hall_name: str = Query(...),
    days: int = Query(120),
) -> list[dict]:
    """
    本日の曜日に絞った機種別成績ランキング。
    過去の同曜日データのみで集計し、「今日どの機種が強いか」を提示する。
    """
    ckey = f"today_machine_ranking:{hall_name}:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
    result = [
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
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/stats", tags=["hall"])
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


@router.get("/api/machine/stats", tags=["machine"])
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


@router.get("/api/hall/report_dates", tags=["hall"])
def get_report_dates(hall_name: str = Query(...)) -> list[str]:
    """スクレイプ済みレポートの日付一覧を返す（新しい順）。"""
    ckey = f"report_dates:{hall_name}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    conn = _get_reports_conn()
    if conn is None:
        return []
    rows = conn.execute(
        "SELECT DISTINCT report_date FROM hall_day_machine WHERE hall_name=? ORDER BY report_date DESC",
        (hall_name,)
    ).fetchall()
    conn.close()
    result = [r["report_date"] for r in rows]
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/installation_snapshot", tags=["hall"])
def get_installation_snapshot(hall_name: str = Query(...)) -> dict:
    """最新の設置スマスロ一覧と、前回取得時からの追加・削除を返す。"""
    conn = _get_reports_conn()
    if conn is None:
        return {"hall_name": hall_name, "snapshot_date": None, "machines": []}
    try:
        dates = conn.execute(
            """SELECT DISTINCT snapshot_date FROM hall_machine_snapshot
               WHERE hall_name=? ORDER BY snapshot_date DESC LIMIT 2""",
            (hall_name,),
        ).fetchall()
        if not dates:
            return {"hall_name": hall_name, "snapshot_date": None, "machines": []}
        latest_date = dates[0][0]
        latest_rows = conn.execute(
            """SELECT machine_name, machine_id, source_url
               FROM hall_machine_snapshot
               WHERE hall_name=? AND snapshot_date=? ORDER BY machine_name""",
            (hall_name, latest_date),
        ).fetchall()
        latest_names = {row[0] for row in latest_rows}
        previous_date = dates[1][0] if len(dates) > 1 else None
        previous_names: set[str] = set()
        if previous_date:
            previous_names = {
                row[0]
                for row in conn.execute(
                    """SELECT machine_name FROM hall_machine_snapshot
                       WHERE hall_name=? AND snapshot_date=?""",
                    (hall_name, previous_date),
                ).fetchall()
            }
        return {
            "hall_name": hall_name,
            "snapshot_date": latest_date,
            "previous_date": previous_date,
            "machine_count": len(latest_rows),
            "machines": [
                {"machine_name": row[0], "machine_id": row[1], "source_url": row[2]}
                for row in latest_rows
            ],
            "added": sorted(latest_names - previous_names) if previous_date else [],
            "removed": sorted(previous_names - latest_names) if previous_date else [],
        }
    except sqlite3.OperationalError:
        return {"hall_name": hall_name, "snapshot_date": None, "machines": []}
    finally:
        conn.close()


@router.get("/api/hall/data_coverage", tags=["hall"])
def get_data_coverage(hall_name: str = Query(...)) -> dict:
    """店舗分析に使えるデータ量と、現時点の分析可否を返す。"""
    from opportunity.models import get_intraday_coverage
    intraday = get_intraday_coverage(hall_name)
    empty = {
        "hall_name": hall_name,
        "performance": {
            "machine_records": 0, "machine_days": 0,
            "machine_first_date": None, "machine_latest_date": None,
            "seat_records": 0, "seat_days": 0,
            "seat_first_date": None, "seat_latest_date": None,
            "total_records": 0, "performance_days": 0,
            "latest_date": None, "age_days": None,
        },
        "installation": {"records": 0, "days": 0, "latest_date": None},
        "events": {"records": 0, "days": 0, "latest_date": None},
        "intraday": intraday,
        "readiness": {
            "trend_level": "insufficient", "trend_label": "不足",
            "trend_ready": False, "seat_ready": False,
            "reasons": ["店舗実績がまだありません"],
        },
    }
    conn = _get_reports_conn()
    if conn is None:
        return empty

    def table_exists(name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def aggregate(table: str, date_column: str, value_column: str | None = None) -> dict:
        if not table_exists(table):
            return {"records": 0, "days": 0, "first_date": None, "latest_date": None}
        value_filter = f" AND {value_column} IS NOT NULL" if value_column else ""
        row = conn.execute(
            f"""SELECT COUNT(*) AS records,
                       COUNT(DISTINCT {date_column}) AS days,
                       MIN({date_column}) AS first_date,
                       MAX({date_column}) AS latest_date
                FROM {table} WHERE hall_name=?{value_filter}""",
            (hall_name,),
        ).fetchone()
        return {
            "records": int(row["records"] or 0),
            "days": int(row["days"] or 0),
            "first_date": row["first_date"],
            "latest_date": row["latest_date"],
        }

    try:
        machine = aggregate("hall_day_machine", "report_date", "avg_diff_coins")
        seat = aggregate("hall_day_seat", "report_date", "diff_coins")
        installation = aggregate("hall_machine_snapshot", "snapshot_date")
        events = aggregate("hall_event", "event_date")
    finally:
        conn.close()

    latest_dates = [value for value in (machine["latest_date"], seat["latest_date"]) if value]
    latest_date = max(latest_dates) if latest_dates else None
    age_days = None
    if latest_date:
        try:
            age_days = max(0, (date.today() - date.fromisoformat(latest_date)).days)
        except ValueError:
            pass
    performance_days = max(machine["days"], seat["days"])
    total_records = machine["records"] + seat["records"]
    fresh = age_days is not None and age_days <= 14
    reasonably_fresh = age_days is not None and age_days <= 30
    if performance_days >= 60 and fresh:
        trend_level, trend_label, trend_ready = "sufficient", "十分", True
    elif performance_days >= 30 and reasonably_fresh:
        trend_level, trend_label, trend_ready = "limited", "参考", True
    elif performance_days >= 14:
        trend_level, trend_label, trend_ready = "limited", "参考", False
    else:
        trend_level, trend_label, trend_ready = "insufficient", "不足", False
    seat_ready = seat["days"] >= 30 and reasonably_fresh
    reasons: list[str] = []
    if performance_days == 0:
        reasons.append("日別の差枚・勝率実績が未収集")
    elif performance_days < 30:
        reasons.append(f"実績は{performance_days}日分。曜日・特定日の比較には30日以上を推奨")
    if age_days is not None and age_days > 30:
        reasons.append(f"最新実績から{age_days}日経過しているため、現在傾向の信頼度は低め")
    if seat["records"] == 0:
        reasons.append("台番号別実績が未収集のため、熱い座席は分析不可")
    if installation["records"] == 0:
        reasons.append("設置機種スナップショットが未収集")
    if not intraday["ready"]:
        reasons.append(f"時間帯別は{intraday['records']}件。30件・3日以上から実測分析を開始")

    return {
        "hall_name": hall_name,
        "performance": {
            "machine_records": machine["records"], "machine_days": machine["days"],
            "machine_first_date": machine["first_date"], "machine_latest_date": machine["latest_date"],
            "seat_records": seat["records"], "seat_days": seat["days"],
            "seat_first_date": seat["first_date"], "seat_latest_date": seat["latest_date"],
            "total_records": total_records, "performance_days": performance_days,
            "latest_date": latest_date, "age_days": age_days,
        },
        "installation": {
            "records": installation["records"], "days": installation["days"],
            "latest_date": installation["latest_date"],
        },
        "events": {
            "records": events["records"], "days": events["days"],
            "latest_date": events["latest_date"],
        },
        "intraday": intraday,
        "readiness": {
            "trend_level": trend_level, "trend_label": trend_label,
            "trend_ready": trend_ready, "seat_ready": seat_ready,
            "reasons": reasons,
        },
    }


@router.get("/api/hall/target_search", tags=["hall"])
def get_target_search(
    visit_date: str = Query(..., description="狙い台を探す日 YYYY-MM-DD"),
    days: int = Query(120, ge=14, le=365),
    limit: int = Query(8, ge=1, le=20),
    region: Literal["all", "matsumoto_shiojiri", "nagano", "osaka"] = "all",
) -> dict:
    """蓄積済みデータから、指定日に狙う店舗と機種の候補を根拠付きで返す。"""
    try:
        target_date = date.fromisoformat(visit_date)
    except ValueError as exc:
        raise HTTPException(400, "visit_date は YYYY-MM-DD で指定してください") from exc

    conn = _get_reports_conn()
    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    weekday_name = weekday_names[target_date.weekday()]
    empty_result = {
        "visit_date": visit_date,
        "weekday": weekday_name,
        "region": region,
        "region_label": region_label(region),
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "halls": [],
        "insufficient_halls": [],
        "notice": "公開データが不足しているため候補を算出できません。",
    }
    if conn is None:
        return empty_result

    reference_date = min(target_date, date.today())
    start_date = reference_date - timedelta(days=days)
    try:
        machine_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(hall_day_machine)").fetchall()
        }
        avg_games_sql = "avg_games" if "avg_games" in machine_columns else "NULL AS avg_games"
        rows = conn.execute(
            f"""SELECT hall_name, report_date, machine_name, avg_diff_coins,
                      win_rate_pct, unit_count, source_url, {avg_games_sql}
               FROM hall_day_machine
               WHERE report_date >= ? AND report_date <= ?
                 AND machine_name != '_NODATA_'
                 AND avg_diff_coins IS NOT NULL
               ORDER BY report_date""",
            (start_date.isoformat(), reference_date.isoformat()),
        ).fetchall()
        active_hall_prefectures: dict[str, str | None] = {}
        try:
            config_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(scrape_hall_config)").fetchall()
            }
            if "prefecture" in config_columns:
                config_rows = conn.execute(
                    "SELECT hall_name,prefecture FROM scrape_hall_config WHERE enabled=1"
                ).fetchall()
                active_hall_prefectures = {row[0]: row[1] for row in config_rows}
            else:
                config_rows = conn.execute(
                    "SELECT hall_name FROM scrape_hall_config WHERE enabled=1"
                ).fetchall()
                active_hall_prefectures = {row[0]: None for row in config_rows}
        except sqlite3.OperationalError:
            active_hall_prefectures = {}

        # 古いDBで都道府県列がない場合も、既定店舗だけは安全に地域判定できる。
        try:
            from api.scheduler import _DEFAULT_HALLS
            default_prefectures = {
                item["hall_name"]: item.get("prefecture") for item in _DEFAULT_HALLS
            }
        except Exception:
            default_prefectures = {}
        for hall_name in list(active_hall_prefectures):
            active_hall_prefectures[hall_name] = (
                active_hall_prefectures[hall_name] or default_prefectures.get(hall_name)
            )

        installation_by_hall: dict[str, dict] = {}
        try:
            snapshot_rows = conn.execute(
                """SELECT snapshot.hall_name,snapshot.snapshot_date,snapshot.machine_name
                   FROM hall_machine_snapshot AS snapshot
                   JOIN (
                     SELECT hall_name,MAX(snapshot_date) AS latest_date
                     FROM hall_machine_snapshot GROUP BY hall_name
                   ) AS latest
                   ON latest.hall_name=snapshot.hall_name
                  AND latest.latest_date=snapshot.snapshot_date"""
            ).fetchall()
            for snapshot_row in snapshot_rows:
                item = installation_by_hall.setdefault(
                    snapshot_row[0], {"snapshot_date": snapshot_row[1], "machines": []}
                )
                item["machines"].append(snapshot_row[2])
        except sqlite3.OperationalError:
            installation_by_hall = {}
    finally:
        conn.close()

    active_halls = set(active_hall_prefectures)
    if active_halls:
        rows = [row for row in rows if row["hall_name"] in active_halls]
    # このツールの狙い台分析はスマスロ専用。末尾集計・順位・ジャグラー等を除外する。
    rows = [row for row in rows if is_smartslot_machine(row["machine_name"])]
    rows = [
        row for row in rows
        if region_matches(
            row["hall_name"],
            active_hall_prefectures.get(row["hall_name"]),
            region,
        )
    ]

    by_hall: dict[str, list] = {}
    for row in rows:
        by_hall.setdefault(row["hall_name"], []).append(row)

    ranked_halls: list[dict] = []
    insufficient: list[dict] = []
    candidate_halls = active_halls or set(by_hall)
    all_hall_names = sorted(
        hall_name for hall_name in candidate_halls
        if region_matches(
            hall_name,
            active_hall_prefectures.get(hall_name),
            region,
        )
    )

    for hall_name in all_hall_names:
        hall_rows = by_hall.get(hall_name, [])
        rows_with_games = [row for row in hall_rows if row["avg_games"] is not None and row["avg_games"] > 0]
        zero_diff_played = sum(row["avg_diff_coins"] == 0 for row in rows_with_games)
        suspicious_zero_rate = (
            zero_diff_played / len(rows_with_games) if rows_with_games else 0.0
        )
        if len(rows_with_games) >= 30 and suspicious_zero_rate >= 0.30:
            insufficient.append({
                "hall_name": hall_name,
                "sample_days": len({row["report_date"] for row in hall_rows}),
                "reason": (
                    f"差枚0かつ稼働ありの行が{suspicious_zero_rate:.0%}あり、"
                    "公開元の差枚欠損が疑われるため候補から除外"
                ),
            })
            continue
        estimate = date_weighted_estimate(hall_rows, target_date)
        if estimate["sample_days"] < 3:
            insufficient.append({
                "hall_name": hall_name,
                "sample_days": estimate["sample_days"],
                "reason": "分析には最低3日分の差枚データが必要です",
            })
            continue
        basis = (
            f"全体{estimate['sample_days']}日＋{weekday_name}曜{estimate['weekday_days']}日"
            f"＋末尾{target_date.day % 10}の日{estimate['digit_days']}日"
        )
        avg_diff = estimate["projected"]
        positive_rate = estimate["positive_rate"]
        latest_date = estimate["latest_date"]
        stale_days = max(0, (reference_date - date.fromisoformat(latest_date)).days)
        freshness_points = 10 if stale_days <= 7 else 6 if stale_days <= 30 else 2 if stale_days <= 90 else 0
        score = round(
            max(0, min(50, 25 + avg_diff / 20))
            + max(0, min(30, positive_rate * 0.30))
            + min(10, estimate["sample_days"] / 60 * 10)
            + freshness_points
        )
        score = max(0, min(100, score))
        confidence = (
            "高" if estimate["sample_days"] >= 60 and estimate["weekday_days"] >= 8 and stale_days <= 14
            else "中" if estimate["sample_days"] >= 30 and estimate["weekday_days"] >= 4 and stale_days <= 30
            else "低"
        )
        validation = walk_forward_backtest(hall_rows)
        action, action_reason = decide_action(
            avg_diff, stale_days, validation, positive_rate
        )

        by_machine: dict[str, list] = {}
        for row in hall_rows:
            by_machine.setdefault(row["machine_name"], []).append(row)
        machine_candidates = []
        excluded_not_installed = 0
        excluded_stale = 0
        installation = installation_by_hall.get(hall_name)
        installation_names = installation["machines"] if installation else []
        snapshot_age = (
            max(0, (reference_date - date.fromisoformat(installation["snapshot_date"])).days)
            if installation else None
        )
        installation_is_fresh = bool(installation and snapshot_age is not None and snapshot_age <= 21)
        for machine_name, machine_rows in by_machine.items():
            machine_dates = {row["report_date"] for row in machine_rows}
            if len(machine_dates) < 2:
                continue
            installed_now = any(
                machine_names_match(machine_name, installed_name)
                for installed_name in installation_names
            )
            if installation_is_fresh and not installed_now:
                excluded_not_installed += 1
                continue
            machine_latest_date = max(machine_dates)
            machine_stale_days = max(
                0, (reference_date - date.fromisoformat(machine_latest_date)).days
            )
            if machine_stale_days > 60:
                excluded_stale += 1
                continue
            diffs = [float(row["avg_diff_coins"]) for row in machine_rows]
            machine_estimate = date_weighted_estimate(machine_rows, target_date)
            reliability = min(1.0, len(machine_dates) / 20)
            machine_avg = round(
                machine_estimate["projected"] * reliability
                + estimate["base_avg"] * (1 - reliability)
            )
            machine_positive = round(
                machine_estimate["positive_rate"] * reliability
                + estimate["positive_rate"] * (1 - reliability)
            )
            machine_score = round(
                max(0, min(55, 27.5 + machine_avg / 30))
                + max(0, min(25, machine_positive * 0.25))
                + min(20, len(machine_dates) / 20 * 20)
            )
            machine_validation = walk_forward_backtest(machine_rows)
            machine_action, machine_action_reason = decide_action(
                machine_avg, machine_stale_days, machine_validation, machine_positive
            )
            if action == "見送り":
                machine_action = "見送り"
                machine_action_reason = f"店舗判定が見送り（{action_reason}）"
            elif action == "要確認" and machine_action.startswith("狙う"):
                machine_action = "要確認"
                machine_action_reason = "店舗単位の過去検証が安全基準に未達"
            elif not installation_is_fresh and machine_action.startswith("狙う"):
                machine_action = "要確認"
                machine_action_reason = "現在の設置確認が取れていない"
            machine_candidates.append({
                "machine_name": machine_name,
                "score": max(0, min(100, machine_score)),
                "avg_diff": machine_avg,
                "raw_avg_diff": machine_estimate["projected"],
                "positive_rate": machine_positive,
                "reliability_pct": round(reliability * 100),
                "sample_days": len(machine_dates),
                "latest_date": machine_latest_date,
                "stale_days": machine_stale_days,
                "installation_status": (
                    "現行設置を確認" if installation_is_fresh and installed_now
                    else "設置情報が古い" if installation
                    else "設置未確認"
                ),
                "weekday_days": machine_estimate["weekday_days"],
                "digit_days": machine_estimate["digit_days"],
                "action": machine_action,
                "action_reason": machine_action_reason,
                "validation": machine_validation,
            })
        machine_candidates.sort(
            key=lambda item: (
                2 if item["action"].startswith("狙う") else 1 if item["action"] == "要確認" else 0,
                item["score"],
                item["sample_days"],
                item["avg_diff"],
            ),
            reverse=True,
        )

        reasons = [
            f"{basis}を重み付け",
            f"指定日の推定差枚 {avg_diff:+,}枚・プラス日率{positive_rate}%",
        ]
        if installation_is_fresh:
            reasons.append(
                f"{installation['snapshot_date']}の設置機種と照合済み"
            )
        else:
            reasons.append("最新設置情報がないため、60日以内の実績機種だけを表示")
        if excluded_not_installed or excluded_stale:
            reasons.append(
                f"撤去・設置未確認{excluded_not_installed}機種、古い実績{excluded_stale}機種を除外"
            )
        if stale_days > 30:
            reasons.append(f"最終データから{stale_days}日経過しているため信頼度を減点")
        if validation["status"] == "validated":
            reasons.append(
                f"先読みなしで過去{validation['test_days']}日を検証："
                f"狙い時成功率{validation['recommendation_success_pct']}%"
            )
        else:
            reasons.append(
                f"過去検証{validation['test_days']}日・推奨{validation['recommended_days']}回で材料不足"
            )
        ranked_halls.append({
            "hall_name": hall_name,
            "score": score,
            "confidence": confidence,
            "action": action,
            "action_reason": action_reason,
            "validation": validation,
            "basis": basis,
            "sample_days": estimate["sample_days"],
            "avg_diff": avg_diff,
            "baseline_avg": estimate["base_avg"],
            "weekday_avg": estimate["weekday_avg"],
            "digit_avg": estimate["digit_avg"],
            "weekday_sample_days": estimate["weekday_days"],
            "digit_sample_days": estimate["digit_days"],
            "positive_rate": positive_rate,
            "latest_date": latest_date,
            "stale_days": stale_days,
            "data_quality": {
                "status": "ok",
                "zero_diff_played_rate_pct": round(suspicious_zero_rate * 100),
                "installation_snapshot_date": installation["snapshot_date"] if installation else None,
                "installation_snapshot_fresh": installation_is_fresh,
                "excluded_not_installed": excluded_not_installed,
                "excluded_stale": excluded_stale,
            },
            "reasons": reasons,
            "target_machines": machine_candidates[:5],
        })

    ranked_halls.sort(
        key=lambda item: (
            2 if item["action"].startswith("狙う") else 1 if item["action"] == "要確認" else 0,
            item["score"],
            item["confidence"] == "高",
            item["sample_days"],
        ),
        reverse=True,
    )
    ranked_halls = ranked_halls[:limit]
    for index, hall in enumerate(ranked_halls, 1):
        hall["rank"] = index

    return {
        "visit_date": visit_date,
        "weekday": weekday_name,
        "region": region,
        "region_label": region_label(region),
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "halls": ranked_halls,
        "insufficient_halls": insufficient,
        "notice": (
            "候補は公開データを先読みなしで過去検証しています。"
            "80%級・90%級は過去の狙い時成功率で、勝利や高設定を保証しません。"
            if ranked_halls else empty_result["notice"]
        ),
    }


@router.get("/api/hall/report", tags=["hall"])
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


@router.get("/api/hall/machine_trend", tags=["hall"])
def get_machine_trend(
    hall_name: str = Query(...),
    machine_name: str = Query(...),
    days: int = Query(30, le=90),
) -> list[dict]:
    """特定機種の過去N日の差枚トレンドを返す。"""
    ckey = f"machine_trend:{hall_name}:{machine_name}:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
    out = [dict(r) for r in rows]
    _cache_set(ckey, out)
    return out


@router.get("/api/hall/top_machines", tags=["hall"])
def get_top_machines(
    hall_name: str = Query(...),
    days: int = Query(30, le=90),
    limit: int = Query(20, le=100),
) -> list[dict]:
    """過去N日の平均差枚ランキングを返す（累積平均）。"""
    ckey = f"top_machines:{hall_name}:{days}:{limit}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
    result = [dict(r) for r in rows]
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/trend_summary", tags=["hall"])
def get_hall_trend_summary(
    hall_name: str = Query(...),
    days: int = Query(14, le=60),
) -> dict:
    """直近N日のホール出玉トレンドサマリー（折れ線グラフ用）"""
    ckey = f"trend_summary:{hall_name}:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    conn = _get_reports_conn()
    if conn is None:
        return {"dates": [], "values": [], "hall_name": hall_name}
    rows = conn.execute(
        """SELECT report_date, ROUND(AVG(avg_diff_coins), 0) AS day_avg
           FROM hall_day_machine
           WHERE hall_name=?
             AND report_date >= date('now', ? || ' days')
             AND avg_diff_coins IS NOT NULL
           GROUP BY report_date
           ORDER BY report_date""",
        (hall_name, f"-{days}")
    ).fetchall()
    conn.close()
    if not rows:
        return {"dates": [], "values": [], "hall_name": hall_name}
    dates = [r["report_date"] for r in rows]
    values = [int(r["day_avg"]) if r["day_avg"] is not None else 0 for r in rows]
    avg = round(sum(values) / len(values)) if values else 0
    trend = values[-1] - values[0] if len(values) >= 2 else 0
    result = {
        "hall_name": hall_name,
        "dates": dates,
        "values": values,
        "avg": avg,
        "trend": trend,
        "days_data": len(dates),
    }
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/hot_machines", tags=["hall"])
def get_hot_machines(
    hall_name: Optional[str] = Query(None),
    days: int = Query(7, le=60),
    limit: int = Query(20, le=50),
) -> list[dict]:
    """期間内の急上昇機種ランキング (直近3日 vs 前週平均)"""
    ckey = f"hot_machines:{hall_name}:{days}:{limit}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    conn = _get_reports_conn()
    if conn is None:
        return []
    hall_filter = "AND hall_name = ?" if hall_name else ""
    params_base = [hall_name] if hall_name else []
    try:
        rows = conn.execute(
            f"""SELECT hall_name, machine_name,
                  AVG(CASE WHEN report_date >= date('now','-3 days') THEN avg_diff_coins END) as rec,
                  AVG(CASE WHEN report_date < date('now','-3 days')
                           AND report_date >= date('now', '-' || ? || ' days') THEN avg_diff_coins END) as base,
                  COUNT(DISTINCT report_date) as days_count,
                  AVG(avg_diff_coins) as avg_overall
               FROM hall_day_machine
               WHERE avg_diff_coins IS NOT NULL
                 AND report_date >= date('now', '-' || ? || ' days')
                 {hall_filter}
               GROUP BY hall_name, machine_name
               HAVING rec IS NOT NULL AND base IS NOT NULL AND days_count >= 3""",
            [days, days] + params_base
        ).fetchall()
    except Exception:
        conn.close()
        return []
    conn.close()
    result = []
    for r in rows:
        rec = float(r[2])
        base = float(r[3])
        if base == 0:
            continue
        surge = round(rec - base)
        result.append({
            "hall_name": r[0],
            "machine_name": r[1],
            "recent_avg": round(rec),
            "base_avg": round(base),
            "surge": surge,
            "days_count": r[4],
            "avg_overall": round(float(r[5])),
        })
    result.sort(key=lambda x: x["surge"], reverse=True)
    result = result[:limit]
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/weekly_summary", tags=["hall"])
def get_hall_weekly_summary(days: int = Query(7, le=14)) -> dict:
    """全ホールの週次サマリー — ランキング変動・急上昇・最高/最低機種"""
    ckey = f"weekly_summary:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    conn = _get_reports_conn()
    if conn is None:
        return {"highlights": [], "top_halls": [], "worst_halls": [], "generated_at": ""}

    try:
        # 今週の上位ホール
        top_rows = conn.execute(
            """SELECT hall_name, ROUND(AVG(avg_diff_coins),0) as avg_diff,
                      COUNT(DISTINCT report_date) as days_cnt
               FROM hall_day_machine
               WHERE report_date >= date('now', ? || ' days') AND avg_diff_coins IS NOT NULL
               GROUP BY hall_name HAVING days_cnt >= 2
               ORDER BY avg_diff DESC LIMIT 5""",
            (f"-{days}",)
        ).fetchall()

        # 急上昇機種（先週比で大幅改善）
        hottest = conn.execute(
            """SELECT hall_name, machine_name,
                      AVG(CASE WHEN report_date >= date('now','-3 days') THEN avg_diff_coins END) as recent,
                      AVG(CASE WHEN report_date < date('now','-3 days')
                                AND report_date >= date('now','-14 days') THEN avg_diff_coins END) as prev
               FROM hall_day_machine
               WHERE avg_diff_coins IS NOT NULL
               GROUP BY hall_name, machine_name
               HAVING recent IS NOT NULL AND prev IS NOT NULL AND recent - prev > 100
               ORDER BY recent - prev DESC LIMIT 5""",
        ).fetchall()

        highlights = []
        for r in hottest:
            diff = round(float(r["recent"]) - float(r["prev"]))
            highlights.append({
                "hall_name": r["hall_name"],
                "machine_name": r["machine_name"],
                "trend": diff,
                "recent": round(float(r["recent"])),
            })

        import datetime as _dtw
        result = {
            "top_halls": [{"hall_name": r["hall_name"], "avg_diff": int(r["avg_diff"]), "days": r["days_cnt"]} for r in top_rows],
            "highlights": highlights,
            "days": days,
            "generated_at": _dtw.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        _cache_set(ckey, result)
        return result
    except Exception as e:
        return {"error": str(e), "highlights": [], "top_halls": [], "generated_at": ""}
    finally:
        conn.close()


@router.post("/api/hall/scrape", tags=["hall"])
def trigger_scrape(
    background_tasks: BackgroundTasks,
    hall_name: str = Query(...),
    days: int = Query(30, le=365),
) -> dict:
    """ホールデータのスクレイプをバックグラウンドで開始する。"""
    if _scrape_status.get(hall_name) == "running":
        return {"status": "running", "message": "すでにスクレイプ中です"}
    background_tasks.add_task(_run_scrape, hall_name, days)
    _scrape_status[hall_name] = "running"
    return {"status": "started", "message": f"{hall_name} のスクレイプを開始しました"}


@router.get("/api/hall/scrape_status", tags=["hall"])
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


@router.get("/api/hall/month_heatmap", tags=["hall"])
def get_month_heatmap(month: str = Query(..., description="YYYY-MM")) -> dict:
    """月次ヒートマップ: 日付→ホール別平均差枚を返す（カレンダー熱量表示用）"""
    ckey = f"month_heatmap:{month}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    conn = _get_reports_conn()
    if not conn:
        return {"month": month, "days": {}}
    try:
        rows = conn.execute("""
            SELECT report_date, hall_name,
                   ROUND(AVG(avg_diff_coins)) AS avg_diff,
                   COUNT(DISTINCT machine_name) AS machine_count
            FROM hall_day_machine
            WHERE report_date LIKE ?
              AND avg_diff_coins IS NOT NULL
            GROUP BY report_date, hall_name
            ORDER BY report_date, avg_diff DESC
        """, (f"{month}%",)).fetchall()
        conn.close()
    except Exception as e:
        try: conn.close()
        except Exception: pass
        return {"month": month, "days": {}, "error": str(e)}

    by_day: dict = {}
    for r in rows:
        d, hname, avg_diff, mc = r[0], r[1], int(r[2] or 0), r[3]
        by_day.setdefault(d, {"halls": [], "avg_diff": 0, "hall_count": 0})
        by_day[d]["halls"].append({"hall_name": hname, "avg_diff": avg_diff, "machine_count": mc})

    for d, data in by_day.items():
        diffs = [h["avg_diff"] for h in data["halls"]]
        data["avg_diff"] = round(sum(diffs) / len(diffs)) if diffs else 0
        data["hall_count"] = len(data["halls"])

    result = {"month": month, "days": by_day}
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/day_machines", tags=["hall"])
def get_day_machines(
    date_str: str = Query(..., description="YYYY-MM-DD"),
    hall_name: str = Query(...),
) -> list[dict]:
    """特定日×特定ホールの台別データ（L3ドリルダウン用）"""
    ckey = f"day_machines:{hall_name}:{date_str}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    conn = _get_reports_conn()
    if not conn:
        return []
    try:
        rows = conn.execute("""
            SELECT machine_name, avg_diff_coins, unit_count, avg_games, win_rate_pct
            FROM hall_day_machine
            WHERE report_date=? AND hall_name=? AND avg_diff_coins IS NOT NULL
            ORDER BY avg_diff_coins DESC
        """, (date_str, hall_name)).fetchall()
        conn.close()
        result = [{"machine_name": r[0], "avg_diff": int(r[1] or 0),
                   "unit_count": r[2], "avg_games": r[3], "win_rate_pct": r[4]} for r in rows]
        _cache_set(ckey, result)
        return result
    except Exception:
        try: conn.close()
        except Exception: pass
        return []


@router.get("/api/hall/hot_days", tags=["hall"])
def get_hot_days(
    hall_name: str = Query(None, description="特定ホール（省略時は全ホール）"),
    months: int = Query(3, description="過去何ヶ月を分析対象にするか"),
) -> dict:
    """
    みんレポデータから統計的に「熱い日」を自動検出する。
    各ホールの日次avg_diff_coinsを分析し、z-score > 1.0 の日を熱い日として返す。
    """
    ckey = f"hot_days:{hall_name}:{months}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    import math
    conn = _get_reports_conn()
    if not conn:
        return {"hot_days": [], "hall_stats": {}}
    try:
        since = (date.today() - timedelta(days=months * 31)).isoformat()
        if hall_name:
            rows = conn.execute("""
                SELECT report_date, hall_name,
                       ROUND(AVG(avg_diff_coins)) AS avg_diff,
                       COUNT(DISTINCT machine_name) AS mc
                FROM hall_day_machine
                WHERE hall_name=? AND report_date >= ? AND avg_diff_coins IS NOT NULL
                GROUP BY report_date, hall_name
            """, (hall_name, since)).fetchall()
        else:
            rows = conn.execute("""
                SELECT report_date, hall_name,
                       ROUND(AVG(avg_diff_coins)) AS avg_diff,
                       COUNT(DISTINCT machine_name) AS mc
                FROM hall_day_machine
                WHERE report_date >= ? AND avg_diff_coins IS NOT NULL
                GROUP BY report_date, hall_name
            """, (since,)).fetchall()
        conn.close()
    except Exception as e:
        try: conn.close()
        except Exception: pass
        return {"hot_days": [], "error": str(e)}

    # ホール別に統計計算
    hall_data: dict[str, list] = {}
    for r in rows:
        date_str, hname, avg_diff, mc = r[0], r[1], float(r[2] or 0), r[3]
        hall_data.setdefault(hname, [])
        hall_data[hname].append({"date": date_str, "avg_diff": avg_diff, "machine_count": mc})

    hot_days = []
    hall_stats = {}
    for hname, entries in hall_data.items():
        if len(entries) < 5:
            continue
        diffs = [e["avg_diff"] for e in entries]
        mean = sum(diffs) / len(diffs)
        variance = sum((d - mean) ** 2 for d in diffs) / len(diffs)
        std = math.sqrt(variance) if variance > 0 else 1
        hall_stats[hname] = {"mean": round(mean), "std": round(std), "days": len(entries)}

        for e in entries:
            z = (e["avg_diff"] - mean) / std
            if z >= 0.8:  # 上位約20%の日を「熱い日」とする
                label = "超熱" if z >= 2.0 else "熱" if z >= 1.5 else "やや熱"
                hot_days.append({
                    "date": e["date"],
                    "hall_name": hname,
                    "avg_diff": round(e["avg_diff"]),
                    "z_score": round(z, 2),
                    "label": label,
                    "machine_count": e["machine_count"],
                })

    hot_days.sort(key=lambda x: (x["date"], -x["z_score"]))
    result = {"hot_days": hot_days, "hall_stats": hall_stats}
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/compare", tags=["hall"])
def compare_halls(days: int = Query(30)) -> list[dict]:
    """
    全ホールの設定傾向を横断比較する。
    アナスロ(hall_day_seat)を優先し、なければみんレポ(hall_day_machine)を使う。
    """
    ckey = f"hall_compare:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached:
        return cached

    conn = _get_reports_conn()
    if not conn:
        return []

    # アナスロ (hall_day_seat) データ
    seat_rows = []
    try:
        seat_rows = conn.execute(
            """SELECT hall_name,
                      COUNT(DISTINCT report_date) as days_count,
                      COUNT(DISTINCT machine_name) as machine_count,
                      COUNT(DISTINCT seat_number) as seat_count,
                      AVG(bb_prob) as avg_bb,
                      AVG(rb_prob) as avg_rb,
                      AVG(diff_coins) as avg_diff,
                      SUM(CASE WHEN diff_coins > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate,
                      MAX(report_date) as latest_date,
                      COUNT(*) as record_count,
                      AVG(CASE WHEN report_date >= date('now','-7 days') THEN bb_prob END) as bb_7d,
                      AVG(CASE WHEN report_date < date('now','-7 days') THEN bb_prob END) as bb_prev
               FROM hall_day_seat
               WHERE bb_prob IS NOT NULL
                 AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
                 AND machine_name NOT LIKE '%データ%'
                 AND report_date >= date('now', '-' || ? || ' days')
               GROUP BY hall_name
               HAVING record_count >= 5
               ORDER BY avg_diff DESC""",
            (days,)
        ).fetchall()
    except Exception:
        pass
    seat_halls = {r[0] for r in seat_rows}

    # みんレポ (hall_day_machine) データ — アナスロにないホールを補完
    minrepo_rows = []
    try:
        minrepo_rows = conn.execute(
            """SELECT hall_name,
                      COUNT(DISTINCT report_date) as days_count,
                      COUNT(DISTINCT machine_name) as machine_count,
                      0 as seat_count,
                      NULL as avg_bb,
                      NULL as avg_rb,
                      AVG(avg_diff_coins) as avg_diff,
                      SUM(CASE WHEN avg_diff_coins > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate,
                      MAX(report_date) as latest_date,
                      COUNT(*) as record_count,
                      NULL as bb_7d,
                      NULL as bb_prev
               FROM hall_day_machine
               WHERE report_date >= date('now', '-' || ? || ' days')
                 AND avg_diff_coins IS NOT NULL
               GROUP BY hall_name
               HAVING record_count >= 3
               ORDER BY avg_diff DESC""",
            (days,)
        ).fetchall()
    except Exception:
        pass

    # マージ（アナスロ優先、みんレポで補完）
    rows = list(seat_rows)
    for r in minrepo_rows:
        if r[0] not in seat_halls:
            rows.append(r)

    # BB急上昇台数
    surge_counts: dict[str, int] = {}
    try:
        surge_rows = conn.execute(
            """SELECT hall_name, machine_name, seat_number,
                      AVG(CASE WHEN report_date >= date('now','-3 days') THEN bb_prob END) as rec,
                      AVG(CASE WHEN report_date < date('now','-3 days')
                                AND report_date >= date('now','-33 days') THEN bb_prob END) as base
               FROM hall_day_seat
               WHERE bb_prob IS NOT NULL AND machine_name NOT LIKE '末尾%'
                 AND machine_name != '_NODATA_' AND machine_name NOT LIKE '%データ%'
               GROUP BY hall_name, machine_name, seat_number
               HAVING rec IS NOT NULL AND base IS NOT NULL AND base > 0
                 AND rec >= base * 1.5"""
        ).fetchall()
        for r in surge_rows:
            surge_counts[r[0]] = surge_counts.get(r[0], 0) + 1
    except Exception:
        pass

    conn.close()

    if not rows:
        return []

    # avg_diffでソート
    rows.sort(key=lambda r: float(r[6] or 0), reverse=True)

    # 各ホールのイベント日スコア
    event_zs: dict = {}
    try:
        from hall.prior import _compute_today_event_z
        for row in rows:
            event_zs[row[0]] = _compute_today_event_z(row[0])
    except Exception:
        pass

    import statistics as _stats
    bbs = [float(r[4]) for r in rows if r[4]]
    mean_bb = _stats.mean(bbs) if bbs else 0.0
    std_bb = max(_stats.stdev(bbs) if len(bbs) > 1 else 0.0, mean_bb * 0.20, 0.5)

    result = []
    for i, r in enumerate(rows):
        bb = float(r[4]) if r[4] else None
        z = round((bb - mean_bb) / max(std_bb, 0.00001), 2) if bb is not None else 0.0
        bb_7d = float(r[10]) if r[10] else None
        bb_prev = float(r[11]) if r[11] else None
        bb_trend = None
        if bb_7d and bb_prev and bb_prev > 0:
            bb_trend = round((bb_7d - bb_prev) / bb_prev * 100, 1)
        ez = event_zs.get(r[0])
        result.append({
            "rank": i + 1,
            "hall_name": r[0],
            "days_data": r[1],
            "machine_count": r[2],
            "seat_count": r[3] or 0,
            "avg_bb": round(bb, 2) if bb is not None else None,
            "avg_rb": round(float(r[5] or 0), 2) if r[5] else None,
            "avg_diff": round(float(r[6] or 0)),
            "win_rate": round(float(r[7] or 0), 1),
            "latest_date": r[8] or "",
            "record_count": r[9],
            "bb_z": z,
            "bb_trend_7d": bb_trend,
            "surge_seat_count": surge_counts.get(r[0], 0),
            "today_event_z": round(ez, 2) if ez is not None else None,
            "data_source": "anaslo" if r[0] in seat_halls else "minrepo",
        })

    _cache_set(ckey, result)
    return result


@router.post("/api/hall/anaslo_scrape", tags=["hall"])
def trigger_anaslo_scrape(
    hall_name: str = Query(...),
    days: int = Query(30),
    background_tasks: BackgroundTasks = None,
):
    if _anaslo_scrape_status.get(hall_name) == "running":
        return {"status": "already_running"}
    background_tasks.add_task(_run_anaslo_scrape, hall_name, days)
    return {"status": "started"}


@router.get("/api/hall/anaslo_status", tags=["hall"])
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


@router.get("/api/hall/seat_dates", tags=["hall"])
def get_seat_dates(hall_name: str = Query(...)) -> list[str]:
    ckey = f"seat_dates:{hall_name}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    conn = _get_reports_conn()
    if not conn:
        return []
    rows = conn.execute(
        "SELECT DISTINCT report_date FROM hall_day_seat WHERE hall_name=? AND bb_prob IS NOT NULL ORDER BY report_date DESC LIMIT 60",
        (hall_name,)
    ).fetchall()
    conn.close()
    result = [r[0] for r in rows]
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/seat_report", tags=["hall"])
def get_seat_report(
    hall_name: str = Query(...),
    date: str = Query(...),
    machine_name: Optional[str] = Query(None),
    limit: int = Query(100),
) -> list[dict]:
    """指定日の台番別データ（差枚降順）"""
    ckey = f"seat_report:{hall_name}:{date}:{machine_name}:{limit}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
                 AND seat_number IS NOT NULL AND seat_number > 0
               ORDER BY diff_coins DESC LIMIT ?""",
            (hall_name, date, limit)
        ).fetchall()

    conn.close()
    result = [
        {
            "seat_number": r[0],
            "machine_name": r[1],
            "diff_coins": r[2],
            "games": r[3],
            "bb_prob": r[4],
            "rb_prob": r[5],
            "ev_pct": r[6],
        }
        for r in rows
    ]
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/tail_analysis", tags=["hall"])
def get_tail_analysis(
    hall_name: str = Query(...),
    days: int = Query(30),
) -> list[dict]:
    """末尾別の平均差枚分析"""
    ckey = f"tail_analysis:{hall_name}:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
    result = [
        {"tail": r[0], "count": r[1], "avg_diff": round(r[2] or 0), "win_rate": round(r[3] or 0, 1)}
        for r in rows
    ]
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/tail_bb_analysis", tags=["hall"])
def get_tail_bb_analysis(
    hall_name: str = Query(...),
    days: int = Query(90),
) -> list[dict]:
    """
    末尾番号別のBB確率分析。
    差枚より精度が高い（差枚はゲーム数に依存するが、BB確率は設定に直接対応）。
    同ホール内の末尾間でBB確率をz-score比較し、設定配分傾向を推定する。
    """
    ckey = f"tail_bb_analysis:{hall_name}:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
            "avg_bb": round(float(avg_bb), 2),
            "avg_rb": round(float(avg_rb or 0), 2),
            "avg_diff": int(avg_diff or 0),
            "win_rate": round(float(win_rate or 0) * 100, 1),
            "seat_cnt": seat_cnt,
            "z_score": round(z, 2),
        })

    result.sort(key=lambda x: -x["z_score"])
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/seat_bb_ranking", tags=["hall"])
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
    ckey = f"seat_bb_rank:{hall_name}:{machine_name}:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached  # type: ignore

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
            "avg_bb": round(avg_bb, 2),
            "avg_rb": round((avg_rb or 0), 2),
            "avg_diff": int(avg_diff or 0),
            "last_date": last_date,
            "dow_bb": round(dow_bb, 2) if dow_bb else None,
            "z_score": round(z, 2),
        })

    result.sort(key=lambda x: -x["z_score"])
    _cache_set(ckey, result, ttl=600)
    return result


@router.get("/api/hall/seat_by_number", tags=["hall"])
def get_seat_by_number(
    hall_name: str = Query(...),
    seat_number: int = Query(...),
) -> list[dict]:
    """台番号から機種名を逆引きする（同台番に複数機種の場合あり）"""
    ckey = f"seat_by_number:{hall_name}:{seat_number}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    conn = _get_reports_conn()
    if not conn:
        return []
    rows = conn.execute(
        """SELECT machine_name, COUNT(*) as record_count
           FROM hall_day_seat
           WHERE hall_name=? AND seat_number=?
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
           GROUP BY machine_name ORDER BY record_count DESC LIMIT 10""",
        (hall_name, seat_number)
    ).fetchall()
    conn.close()
    result = [{"machine_name": r[0], "record_count": r[1]} for r in rows]
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/seat_detail", tags=["hall"])
def get_seat_detail(
    hall_name: str = Query(...),
    machine_name: str = Query(...),
    seat_number: int = Query(...),
    days: int = Query(90),
) -> dict:
    """特定台番の詳細: 日別履歴・曜日別実績・直近トレンド"""
    ckey = f"seat_detail:{hall_name}:{machine_name}:{seat_number}:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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

    # 過去最長連続好調
    max_streak = 0
    cur = 0
    for h in reversed(hist_list):
        if (h["diff"] or 0) > 0:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 0

    # BB確率トレンド：直近14日 vs 過去14-28日
    bbs = [(h["date"], h["bb_prob"]) for h in hist_list if h["bb_prob"]]
    recent14 = [b for d, b in bbs[:14]]
    prev14 = [b for d, b in bbs[14:28]]
    bb_trend = None
    if recent14 and prev14:
        r14_avg = sum(recent14) / len(recent14)
        p14_avg = sum(prev14) / len(prev14)
        bb_trend = round((r14_avg - p14_avg) / max(p14_avg, 0.001) * 100, 1)

    # 最強曜日（avg_diffが一番高い曜日）
    best_weekday = None
    if weekday_stats:
        best_wd = max(weekday_stats, key=lambda x: x["avg_diff"])
        if best_wd["count"] >= 2:
            best_weekday = {"weekday": best_wd["weekday"], "avg_diff": best_wd["avg_diff"], "count": best_wd["count"]}

    # 月の日付パターン分析（1-9日/10-19日/20-31日の3ブロック）
    date_block_data: dict[str, list[int]] = {"上旬(1-9)": [], "中旬(10-19)": [], "下旬(20-31)": []}
    for h in hist_list:
        day_num = int(h["date"].split("-")[2])
        if day_num <= 9:
            date_block_data["上旬(1-9)"].append(h["diff"] or 0)
        elif day_num <= 19:
            date_block_data["中旬(10-19)"].append(h["diff"] or 0)
        else:
            date_block_data["下旬(20-31)"].append(h["diff"] or 0)
    date_blocks = []
    for block, vals in date_block_data.items():
        if len(vals) >= 2:
            date_blocks.append({
                "block": block,
                "avg_diff": round(sum(vals) / len(vals)),
                "count": len(vals),
                "win_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
            })
    date_blocks.sort(key=lambda x: -x["avg_diff"])

    # ゾロ目台番かどうか（11, 22, 33...）
    is_zoro = seat_number > 0 and seat_number % 11 == 0

    result = {
        "machine_name": machine_name,
        "seat_number": seat_number,
        "total_days": len(hist_list),
        "avg_diff": avg,
        "win_rate": win_rate,
        "best": best,
        "worst": worst,
        "std": std,
        "win_streak": streak,
        "max_streak": max_streak,
        "bb_trend_14d": bb_trend,
        "best_weekday": best_weekday,
        "date_block_analysis": date_blocks,
        "is_zoro_seat": is_zoro,
        "history": hist_list[:60],
        "weekday_stats": weekday_stats,
    }
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/machine_seat_ranking", tags=["hall"])
def get_machine_seat_ranking(
    hall_name: str = Query(...),
    machine_name: str = Query(...),
    days: int = Query(30),
) -> list[dict]:
    """特定機種の全台番ランキング（複合スコア + BB z-score付き）"""
    ckey = f"machine_seat_ranking:{hall_name}:{machine_name}:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
    raw_bb_std = _stats.stdev(bb_vals) if len(bb_vals) > 1 else 0.0
    bb_std = max(raw_bb_std, bb_mean * 0.20, 0.5)

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
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/today_targets", tags=["hall"])
def get_today_targets(
    hall_name: str = Query(...),
    days: int = Query(30),
) -> dict:
    """今日の狙い台TOP3 — 曜日傾向・安定性・直近トレンドを複合スコアで統合"""
    ckey = f"today_targets:{hall_name}:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
            raw_s = _stats2.stdev(bbs) if len(bbs) > 1 else 0.0
            s = max(raw_s, m * 0.20, 0.5)
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

        # 勝率ボーナス: 勝率60%超の台は信頼性が高い → 小さな上乗せ
        win_bonus = max(0.0, (win_rate - 50.0) / 50.0) * avg_diff * 0.1 if avg_diff > 0 else 0.0

        # 正規化スコア（差枚35% + 同曜日20% + 安定性15% + トレンド10% + BB20%）
        # + 勝率ボーナス（~10% of avg_diff when win_rate=100%)
        score = (
            avg_diff     * 0.35 +
            avg_same_dow * 0.20 +
            avg_diff * stability * 0.15 +
            trend        * 0.10 +
            bb_bonus     * 0.20 +
            win_bonus
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

    result = {
        "seats": seats,
        "best_tail": best_tail,
        "best_machine": best_machine,
        "today_weekday": today_name,
        "data_days": days,
    }
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/machine_setting_tendency", tags=["hall"])
def get_machine_setting_tendency(
    hall_name: str = Query(...),
    days: int = Query(60),
) -> list[dict]:
    """
    機種ごとの設定傾向を推定して返す。
    hall/prior.py の _estimate_prior_from_anaslo を全機種に適用。
    """
    ckey = f"machine_setting_tendency:{hall_name}:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/machine_dow_heatmap", tags=["hall"])
def get_machine_dow_heatmap(
    hall_name: str = Query(...),
    days: int = Query(90),
    top_n: int = Query(10),
) -> dict:
    """
    機種×曜日の平均BB確率ヒートマップ。
    各セルに曜日基準からのz-scoreを返す（プラス=当日好調曜日）。
    """
    ckey = f"dow_heatmap:{hall_name}:{days}:{date.today()}"
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


@router.get("/api/hall/prior_quality", tags=["hall"])
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


@router.get("/api/hall/event_day_pattern", tags=["hall"])
def get_event_day_pattern(
    hall_name: str = Query(...),
    days: int = Query(180),
) -> dict:
    """
    日付パターン分析。「5のつく日」「月末」「毎週特定曜日」など
    どのパターンがBB確率上昇と相関するかを統計的に分析。
    """
    ckey = f"event_day:{hall_name}:{days}:{date.today()}"
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


@router.get("/api/hall/zone_analysis", tags=["hall"])
def get_zone_analysis(
    hall_name: str = Query(...),
    days: int = Query(90),
    zone_size: int = Query(10),
) -> list[dict]:
    """
    台番号をzone_size単位でグループ化して高設定率を比較。
    特定の「島」または「ゾーン」に高設定が集中するパターンを検知。
    """
    ckey = f"zone:{hall_name}:{days}:{zone_size}:{date.today()}"
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


@router.get("/api/hall/machine_high_rate", tags=["hall"])
def get_machine_high_rate(
    hall_name: str = Query(...),
    days: int = Query(90),
) -> list[dict]:
    """
    機種ごとの「高設定投入率」推定。
    各台日のBB確率を機種内でz-score化し、z>=1.0の割合を「高設定率」として返す。
    高設定率が高い機種 = このホールが力を入れている機種。
    """
    ckey = f"machine_high_rate:{hall_name}:{days}:{date.today()}"
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


@router.get("/api/hall/today_briefing", tags=["hall"])
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
    ckey = f"today_briefing:{hall_name}:{today}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
    _mf = "AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_' AND machine_name NOT LIKE '%データ%'"
    recent_bb = {(r[0], r[1]): float(r[2]) for r in conn.execute(
        f"""SELECT machine_name, seat_number, AVG(bb_prob) FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL AND report_date >= ?
             {_mf}
           GROUP BY machine_name, seat_number HAVING COUNT(*) >= 1""",
        (hall_name, prev_date)
    ).fetchall()}
    baseline_bb = {(r[0], r[1]): float(r[2]) for r in conn.execute(
        f"""SELECT machine_name, seat_number, AVG(bb_prob) FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL AND report_date < ?
             AND report_date >= date(?, '-60 days')
             {_mf}
           GROUP BY machine_name, seat_number HAVING COUNT(*) >= 3""",
        (hall_name, prev_date, prev_date)
    ).fetchall()}
    m_bbs: dict = {}
    for (m, s), b in baseline_bb.items():
        m_bbs.setdefault(m, []).append(b)
    # std下限: 機種内平均BB回数の20%（過小な分散で z-score が爆発するのを防ぐ）
    m_mean = {m: sum(v)/len(v) for m, v in m_bbs.items()}
    m_std = {
        m: max(_s.stdev(v) if len(v) > 1 else 0.0,
               m_mean[m] * 0.20)
        for m, v in m_bbs.items()
    }
    surges = []
    for (m, s), rec_bb in recent_bb.items():
        base = baseline_bb.get((m, s))
        if base is None:
            continue
        z = (rec_bb - base) / max(m_std.get(m, max(base * 0.20, 0.5)), 1e-4)
        if z >= 0.8:
            surges.append({"machine": m, "seat": s, "surge_z": round(z, 1),
                           "recent_bb": round(rec_bb, 2), "baseline_bb": round(base, 2)})
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
             AND machine_name NOT LIKE '%データ%'
             AND report_date >= date('now','-30 days')
           GROUP BY machine_name, seat_number HAVING days >= 3
           ORDER BY avg_diff DESC LIMIT 5""",
        (hall_name,)
    ).fetchall()
    top_seats = [{"machine": r[0], "seat": r[1], "days": r[2],
                  "avg_diff": int(r[3] or 0), "win_rate": round(r[4] or 0, 1),
                  "avg_bb": round(float(r[5] or 0), 2)} for r in top_rows]

    # 連続好調台（直近3日以上プラス）
    streak_rows = conn.execute(
        """SELECT machine_name, seat_number, report_date, diff_coins
           FROM hall_day_seat
           WHERE hall_name=? AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
             AND machine_name NOT LIKE '%データ%'
             AND (bb_prob IS NOT NULL OR ev_pct IS NOT NULL)
             AND report_date >= date('now', '-14 days')
           ORDER BY machine_name, seat_number, report_date DESC""",
        (hall_name,)
    ).fetchall()
    from collections import defaultdict as _defaultdict
    _seat_data: dict = _defaultdict(list)
    for m, s, d, diff in streak_rows:
        _seat_data[(m, s)].append(diff or 0)
    streak_seats = []
    for (m, s), diffs in _seat_data.items():
        k = 0
        for d in diffs:
            if d > 0:
                k += 1
            else:
                break
        if k >= 3:
            streak_seats.append({"machine": m, "seat": s, "streak": k,
                                  "avg_diff": round(sum(diffs[:k]) / k)})
    streak_seats.sort(key=lambda x: (-x["streak"], -x["avg_diff"]))

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

    result = {
        "date": today.isoformat(),
        "weekday": dow_ja,
        "is_event_candidate": event_z is not None and event_z >= 0.5,
        "event_z": round(event_z, 2) if event_z else None,
        "dow_avg_diff": round(today_dow_diff) if today_dow_diff is not None else None,
        "dow_rank": dow_rank,
        "dow_total": len(dow_data),
        "bb_surge_seats": surges[:3],
        "streak_seats": streak_seats[:3],
        "top_seats": top_seats,
        "high_rate_machines": hr_list[:3],
    }
    _cache_set(ckey, result)
    return result


@router.get("/api/hall/bb_surge_seats", tags=["hall"])
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
    ckey = f"bb_surge_seats:{hall_name}:{days}:{min_surge}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
        m_avg = sum(bbs) / len(bbs)
        raw_std = _s.stdev(bbs) if len(bbs) > 1 else 0.0
        machine_std[mname] = max(raw_std, m_avg * 0.20, 0.5)

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
                "recent_bb": round(rec_bb, 2),
                "baseline_bb": round(base_bb, 2),
                "surge_z": round(surge_z, 2),
                "recent_days": days,
            })

    results.sort(key=lambda x: -x["surge_z"])
    out = results[:20]
    _cache_set(ckey, out)
    return out


@router.get("/api/hall/slump_seats", tags=["hall"])
def get_slump_seats(
    hall_name: str = Query(...),
    days: int = Query(5),
    min_slump: float = Query(0.5),
    limit: int = Query(10, le=30),
) -> list[dict]:
    """
    直近N日間のBB確率が60日平均より有意に低い台を検出。
    スランプ台 = 低設定継続 or そろそろ設定変更待ちシグナル。
    """
    ckey = f"slump_seats:{hall_name}:{days}:{min_slump}:{date.today()}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
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
           GROUP BY machine_name, seat_number HAVING cnt >= 2""",
        (hall_name, prev_date)
    ).fetchall()
    baseline = conn.execute(
        """SELECT machine_name, seat_number, AVG(bb_prob) as avg_bb
           FROM hall_day_seat
           WHERE hall_name=? AND bb_prob IS NOT NULL
             AND report_date < ?
             AND report_date >= date(?, '-60 days')
             AND machine_name NOT LIKE '末尾%' AND machine_name != '_NODATA_'
           GROUP BY machine_name, seat_number HAVING COUNT(*) >= 5""",
        (hall_name, prev_date, prev_date)
    ).fetchall()
    import statistics as _s
    m_bbs: dict[str, list[float]] = {}
    for r in baseline:
        m_bbs.setdefault(r[0], []).append(float(r[2]))
    machine_std: dict[str, float] = {
        m: max(_s.stdev(v) if len(v) > 1 else 0.0, (sum(v)/len(v)) * 0.20, 0.5)
        for m, v in m_bbs.items()
    }
    baseline_map = {(r[0], r[1]): float(r[2]) for r in baseline}
    conn.close()
    results = []
    for r in recent:
        mname, seat, rec_bb = r[0], r[1], float(r[2])
        base_bb = baseline_map.get((mname, seat))
        if base_bb is None:
            continue
        std = machine_std.get(mname, 0.001)
        slump_z = (base_bb - rec_bb) / std
        if slump_z >= min_slump:
            results.append({
                "machine_name": mname,
                "seat_number": seat,
                "recent_bb": round(rec_bb, 2),
                "baseline_bb": round(base_bb, 2),
                "slump_z": round(slump_z, 2),
                "recent_days": days,
            })
    results.sort(key=lambda x: -x["slump_z"])
    out = results[:limit]
    _cache_set(ckey, out)
    return out


@router.get("/api/machine/cross_hall", tags=["hall"])
def get_machine_cross_hall(
    machine_name: str = Query(..., description="機種名（部分一致OK）"),
    days: int = Query(30, le=90),
    limit: int = Query(15, le=30),
) -> list[dict]:
    """
    同一機種を複数ホールで横断比較。
    どの店が最もその機種に高設定を入れているかを返す。
    """
    ckey = f"cross_hall:{machine_name}:{days}:{date.today()}"
    cached = _cache_get(ckey)
    if cached:
        return cached
    conn = _get_reports_conn()
    if not conn:
        return []
    rows = conn.execute(
        """SELECT hall_name,
                  COUNT(DISTINCT report_date) as days_count,
                  ROUND(AVG(avg_diff_coins)) as avg_diff,
                  ROUND(AVG(win_rate_pct), 1) as win_rate,
                  MAX(report_date) as latest_date,
                  COUNT(*) as record_count
           FROM hall_day_machine
           WHERE machine_name LIKE ? AND avg_diff_coins IS NOT NULL
             AND report_date >= date('now', '-' || ? || ' days')
           GROUP BY hall_name
           HAVING days_count >= 3
           ORDER BY avg_diff DESC
           LIMIT ?""",
        (f"%{machine_name}%", days, limit)
    ).fetchall()
    conn.close()
    if not rows:
        return []
    max_diff = max(abs(r[2] or 0) for r in rows) or 1
    result = [
        {
            "hall_name": r[0],
            "days_count": r[1],
            "avg_diff": int(r[2] or 0),
            "win_rate": float(r[3] or 0),
            "latest_date": r[4] or "",
            "record_count": r[5],
            "bar_pct": round(abs(r[2] or 0) / max_diff * 100),
        }
        for r in rows
    ]
    _cache_set(ckey, result)
    return result

