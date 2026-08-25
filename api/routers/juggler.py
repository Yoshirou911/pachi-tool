"""ジャグラー専用API（営業中判定・朝一候補）。"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import _get_reports_conn
from hall.regions import region_label, region_matches
from hall.target_validation import decide_action, grade_policy, walk_forward_backtest
from juggler.models import assess_juggler, catalog, profile_for_machine


router = APIRouter()


def _bonus_count(raw_value: float | int | None, games: int) -> int | None:
    """収集元ごとに異なるBB/RB表現を実回数へ統一する。

    アナスロ由来のDB値は ``1/289.8`` を 0.003451 のような確率で保存する。
    手入力・将来の取得元は回数を直接保存する場合があるため両方を扱う。
    """
    if raw_value is None or games <= 0:
        return None
    value = float(raw_value)
    if value < 0:
        return None
    count = round(games * value) if 0 <= value < 1 else round(value)
    return count if 0 <= count <= games else None


def _validated_action(
    avg_diff: int,
    strong_rate: int,
    stale_days: int,
    validation: dict,
    usable_days: int,
    evidence_level: str,
) -> str:
    """BB/RBで検証できた履歴だけを高信頼の朝一候補へ昇格する。"""
    validation_action, _ = decide_action(avg_diff, stale_days, validation, strong_rate)
    if validation_action == "見送り":
        return "見送り"
    # 機種平均差枚は「出た」ことしか示さず、設定を直接示さないため上限を設ける。
    if evidence_level != "bonus_counts":
        return "要確認" if usable_days >= 5 and stale_days <= 90 else "データ不足"
    if validation_action == "狙う・90%級":
        return "朝一候補・90%級"
    if validation_action == "狙う・80%級":
        return "朝一候補・80%級"
    if validation_action.startswith("狙う") and usable_days >= 30:
        return "朝一候補"
    if usable_days >= 5 and stale_days <= 90:
        return "要確認"
    return "データ不足"


class JugglerAssessmentRequest(BaseModel):
    profile_id: str = Field(min_length=1, max_length=40)
    games: int = Field(gt=0, le=100000)
    bb_count: int = Field(ge=0, le=1000)
    rb_count: int = Field(ge=0, le=1000)


@router.get("/api/juggler/catalog", tags=["juggler"])
def get_juggler_catalog() -> list[dict]:
    return catalog()


@router.post("/api/juggler/assess", tags=["juggler"])
def assess_juggler_api(body: JugglerAssessmentRequest) -> dict:
    try:
        return assess_juggler(body.profile_id, body.games, body.bb_count, body.rb_count)
    except KeyError as exc:
        raise HTTPException(404, "対応していないジャグラー機種です") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/api/juggler/targets", tags=["juggler"])
def get_juggler_targets(
    visit_date: str = Query(..., description="朝一候補を探す日 YYYY-MM-DD"),
    days: int = Query(180, ge=30, le=730),
    limit: int = Query(20, ge=1, le=100),
    region: Literal["all", "shijonawate", "matsumoto_shiojiri", "nagano", "osaka"] = "all",
) -> dict:
    try:
        target_date = date.fromisoformat(visit_date)
    except ValueError as exc:
        raise HTTPException(400, "visit_date は YYYY-MM-DD で指定してください") from exc

    conn = _get_reports_conn()
    empty = {
        "visit_date": visit_date,
        "region": region,
        "region_label": region_label(region),
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "candidates": [],
        "data_coverage": {"rows": 0, "days": 0, "halls": 0, "latest_date": None},
        "validation_policy": grade_policy(),
        "notice": "ジャグラー履歴はまだありません。今後の収集分から朝一分析を育てます。",
    }
    if conn is None:
        return empty

    start_date = target_date - timedelta(days=days)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        rows = []
        if "hall_day_seat" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(hall_day_seat)")}
            bb_sql = "bb_prob" if "bb_prob" in columns else "NULL AS bb_prob"
            rb_sql = "rb_prob" if "rb_prob" in columns else "NULL AS rb_prob"
            rows = conn.execute(
                f"""SELECT hall_name,report_date,machine_name,seat_number,diff_coins,games,
                           {bb_sql},{rb_sql},source_url
                      FROM hall_day_seat
                     WHERE report_date >= ? AND report_date < ?
                       AND machine_name LIKE '%ジャグラー%'
                       AND seat_number > 0 AND games > 0
                     ORDER BY report_date""",
                (start_date.isoformat(), target_date.isoformat()),
            ).fetchall()

        machine_rows = []
        if "hall_day_machine" in tables:
            machine_rows = conn.execute(
                """SELECT hall_name,report_date,machine_name,unit_count,avg_diff_coins,
                          avg_games,win_rate_pct,source_url
                     FROM hall_day_machine
                    WHERE report_date >= ? AND report_date < ?
                      AND machine_name LIKE '%ジャグラー%'
                      AND avg_games > 0
                    ORDER BY report_date""",
                (start_date.isoformat(), target_date.isoformat()),
            ).fetchall()

        prefectures: dict[str, str | None] = {}
        try:
            config_columns = {row[1] for row in conn.execute("PRAGMA table_info(scrape_hall_config)")}
            if "prefecture" in config_columns:
                config_rows = conn.execute(
                    "SELECT hall_name,prefecture FROM scrape_hall_config WHERE enabled=1"
                ).fetchall()
                prefectures = {row[0]: row[1] for row in config_rows}
        except sqlite3.OperationalError:
            prefectures = {}
    finally:
        conn.close()

    rows = [
        row for row in rows
        if region_matches(row["hall_name"], prefectures.get(row["hall_name"]), region)
    ]
    machine_rows = [
        row for row in machine_rows
        if region_matches(row["hall_name"], prefectures.get(row["hall_name"]), region)
    ]
    if not rows and not machine_rows:
        return empty

    by_seat: dict[tuple[str, str, int], list] = {}
    for row in rows:
        by_seat.setdefault(
            (row["hall_name"], row["machine_name"], int(row["seat_number"])), []
        ).append(row)

    candidates = []
    for (hall_name, machine_name, seat_number), history in by_seat.items():
        weighted_hits = 0.0
        weight_total = 0.0
        weighted_diff = 0.0
        usable_days = 0
        source_urls = set()
        verified_validation_points = []
        proxy_validation_points = []
        profile_id = profile_for_machine(machine_name)
        for row in history:
            report_day = date.fromisoformat(row["report_date"])
            age = max(0, (target_date - report_day).days)
            weight = math.exp(-age / 120)
            if report_day.weekday() == target_date.weekday():
                weight *= 1.5
            if report_day.day % 10 == target_date.day % 10:
                weight *= 1.3
            games = int(row["games"] or 0)
            if games < 1500:
                continue
            signal = None
            bb_count = _bonus_count(row["bb_prob"], games)
            rb_count = _bonus_count(row["rb_prob"], games)
            if profile_id and games >= 4000 and bb_count is not None and rb_count is not None:
                try:
                    assessment = assess_juggler(
                        profile_id, games, bb_count, rb_count
                    )
                    signal = assessment["high_setting_probability_pct"] >= 70
                    verified_validation_points.append(
                        (report_day, 100.0 if signal else 0.0)
                    )
                except ValueError:
                    signal = None
            if signal is None and games >= 3000 and row["diff_coins"] is not None:
                # BB/RBがない取得元は差枚だけを弱い補助材料として使う。
                signal = int(row["diff_coins"]) >= 150
                proxy_validation_points.append((report_day, 100.0 if signal else 0.0))
                weight *= 0.35
            if signal is None:
                continue
            usable_days += 1
            weight_total += weight
            weighted_hits += weight if signal else 0
            weighted_diff += float(row["diff_coins"] or 0) * weight
            if row["source_url"]:
                source_urls.add(row["source_url"])

        if not weight_total:
            continue
        strong_rate = round(weighted_hits / weight_total * 100)
        avg_diff = round(weighted_diff / weight_total)
        latest_date = max(row["report_date"] for row in history)
        stale_days = max(0, (target_date - date.fromisoformat(latest_date)).days)
        evidence_level = "bonus_counts" if len(verified_validation_points) >= 30 else "diff_proxy"
        validation_points = (
            verified_validation_points if evidence_level == "bonus_counts"
            else proxy_validation_points
        )
        validation = walk_forward_backtest(validation_points)
        action = _validated_action(
            avg_diff, strong_rate, stale_days, validation, usable_days, evidence_level
        )
        score = max(
            0,
            min(
                100,
                round(strong_rate * 0.55 + min(25, usable_days) + max(0, min(20, avg_diff / 25 + 10))),
            ),
        )
        candidates.append({
            "hall_name": hall_name,
            "machine_name": machine_name,
            "seat_number": seat_number,
            "scope": "seat",
            "profile_id": profile_id,
            "score": score,
            "action": action,
            "strong_rate_pct": strong_rate,
            "avg_diff": avg_diff,
            "sample_days": usable_days,
            "latest_date": latest_date,
            "stale_days": stale_days,
            "source_urls": sorted(source_urls)[:3],
            "validation": validation,
            "evidence_level": evidence_level,
            "evidence_label": (
                "BB・REG実績で検証" if evidence_level == "bonus_counts"
                else "差枚による補助判定"
            ),
            "verified_signal_days": len(verified_validation_points),
            "proxy_signal_days": len(proxy_validation_points),
            "reason": (
                f"過去{usable_days}日・指定曜日/日付を重視・高設定寄り{strong_rate}%。"
                f"{('BB・REG' if evidence_level == 'bonus_counts' else '差枚補助')}の先読みなし検証"
                f"{validation['test_days']}日・品質{validation.get('quality_score', 0)}点"
            ),
        })

    # 台番号データがない店舗でも、公開されている機種別日次集計から
    # 店舗×機種の朝一候補を出す。台番号候補より根拠が粗いことを明示する。
    seat_machine_keys = {(item["hall_name"], item["machine_name"]) for item in candidates}
    by_machine: dict[tuple[str, str], list] = {}
    for row in machine_rows:
        by_machine.setdefault((row["hall_name"], row["machine_name"]), []).append(row)
    for (hall_name, machine_name), history in by_machine.items():
        if (hall_name, machine_name) in seat_machine_keys:
            continue
        weighted_hits = 0.0
        weight_total = 0.0
        weighted_diff = 0.0
        usable_days = 0
        source_urls = set()
        for row in history:
            games = int(row["avg_games"] or 0)
            if games < 1500 or row["avg_diff_coins"] is None:
                continue
            report_day = date.fromisoformat(row["report_date"])
            age = max(0, (target_date - report_day).days)
            weight = math.exp(-age / 120) * 0.7
            if report_day.weekday() == target_date.weekday():
                weight *= 1.5
            if report_day.day % 10 == target_date.day % 10:
                weight *= 1.3
            avg_diff = float(row["avg_diff_coins"])
            win_rate = float(row["win_rate_pct"] or 0)
            signal = avg_diff >= 150 and win_rate >= 55
            usable_days += 1
            weight_total += weight
            weighted_hits += weight if signal else 0
            weighted_diff += avg_diff * weight
            if row["source_url"]:
                source_urls.add(row["source_url"])
        if not weight_total:
            continue
        strong_rate = round(weighted_hits / weight_total * 100)
        avg_diff = round(weighted_diff / weight_total)
        latest_date = max(row["report_date"] for row in history)
        stale_days = max(0, (target_date - date.fromisoformat(latest_date)).days)
        proxy_points = [
            (
                date.fromisoformat(row["report_date"]),
                100.0 if float(row["avg_diff_coins"] or 0) >= 150
                and float(row["win_rate_pct"] or 0) >= 55 else 0.0,
            )
            for row in history
            if int(row["avg_games"] or 0) >= 3000 and row["avg_diff_coins"] is not None
        ]
        validation = walk_forward_backtest(proxy_points)
        action = _validated_action(
            avg_diff, strong_rate, stale_days, validation, usable_days, "diff_proxy"
        )
        score = max(0, min(100, round(
            strong_rate * 0.5 + min(25, usable_days) + max(0, min(20, avg_diff / 25 + 10))
        )))
        candidates.append({
            "hall_name": hall_name,
            "machine_name": machine_name,
            "seat_number": None,
            "scope": "machine",
            "profile_id": profile_for_machine(machine_name),
            "score": score,
            "action": action,
            "strong_rate_pct": strong_rate,
            "avg_diff": avg_diff,
            "sample_days": usable_days,
            "latest_date": latest_date,
            "stale_days": stale_days,
            "source_urls": sorted(source_urls)[:3],
            "validation": validation,
            "evidence_level": "diff_proxy",
            "evidence_label": "機種平均差枚による補助判定",
            "verified_signal_days": 0,
            "proxy_signal_days": len(proxy_points),
            "reason": (
                f"機種別の過去{usable_days}日・平均+150枚かつ勝率55%以上が{strong_rate}%"
                f"・差枚補助の先読みなし検証{validation['test_days']}日"
                f"・品質{validation.get('quality_score', 0)}点（台番号は未特定）"
            ),
        })

    def priority(action: str) -> int:
        if action == "朝一候補・90%級": return 5
        if action == "朝一候補・80%級": return 4
        if action == "朝一候補": return 3
        if action == "要確認": return 2
        if action == "見送り": return 1
        return 0

    candidates.sort(
        key=lambda item: (priority(item["action"]), item["score"], item["sample_days"]),
        reverse=True,
    )
    candidates = candidates[:limit]
    for rank, candidate in enumerate(candidates, 1):
        candidate["rank"] = rank

    all_rows = list(rows) + list(machine_rows)
    distinct_dates = {row["report_date"] for row in all_rows}
    return {
        "visit_date": visit_date,
        "region": region,
        "region_label": region_label(region),
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "candidates": candidates,
        "data_coverage": {
            "rows": len(all_rows),
            "days": len(distinct_dates),
            "halls": len({row["hall_name"] for row in all_rows}),
            "latest_date": max(distinct_dates) if distinct_dates else None,
        },
        "validation_policy": grade_policy(),
        "notice": (
            "朝一候補は過去傾向です。90%級・80%級へ上がるのはBB・REG実績を先読みなしで検証できた候補だけで、当日の設定投入や勝利を保証しません。"
            if candidates else empty["notice"]
        ),
    }
