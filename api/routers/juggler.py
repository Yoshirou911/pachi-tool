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
from juggler.models import assess_juggler, catalog, profile_for_machine


router = APIRouter()


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
    region: Literal["all", "matsumoto_shiojiri", "nagano", "osaka"] = "all",
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
        "notice": "ジャグラー履歴はまだありません。今後の収集分から朝一分析を育てます。",
    }
    if conn is None:
        return empty

    start_date = target_date - timedelta(days=days)
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hall_day_seat'"
        ).fetchone()
        if not table_exists:
            return empty
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
    if not rows:
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
            if profile_id and row["bb_prob"] is not None and row["rb_prob"] is not None:
                try:
                    assessment = assess_juggler(
                        profile_id, games, int(row["bb_prob"]), int(row["rb_prob"])
                    )
                    signal = assessment["high_setting_probability_pct"] >= 60
                except ValueError:
                    signal = None
            if signal is None and row["diff_coins"] is not None:
                # BB/RBがない取得元は差枚だけを弱い補助材料として使う。
                signal = int(row["diff_coins"]) > 0
                weight *= 0.55
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
        if usable_days >= 20 and strong_rate >= 55 and avg_diff > 0 and stale_days <= 45:
            action = "朝一候補"
        elif usable_days >= 5 and stale_days <= 90:
            action = "要確認"
        else:
            action = "データ不足"
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
            "profile_id": profile_id,
            "score": score,
            "action": action,
            "strong_rate_pct": strong_rate,
            "avg_diff": avg_diff,
            "sample_days": usable_days,
            "latest_date": latest_date,
            "stale_days": stale_days,
            "source_urls": sorted(source_urls)[:3],
            "reason": (
                f"過去{usable_days}日・指定曜日/日付を重視・高設定寄り{strong_rate}%"
            ),
        })

    priority = {"朝一候補": 2, "要確認": 1, "データ不足": 0}
    candidates.sort(
        key=lambda item: (priority[item["action"]], item["score"], item["sample_days"]),
        reverse=True,
    )
    candidates = candidates[:limit]
    for rank, candidate in enumerate(candidates, 1):
        candidate["rank"] = rank

    distinct_dates = {row["report_date"] for row in rows}
    return {
        "visit_date": visit_date,
        "region": region,
        "region_label": region_label(region),
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "candidates": candidates,
        "data_coverage": {
            "rows": len(rows),
            "days": len(distinct_dates),
            "halls": len({row["hall_name"] for row in rows}),
            "latest_date": max(distinct_dates) if distinct_dates else None,
        },
        "notice": (
            "朝一候補は過去傾向です。当日の設定投入や勝利を保証しません。"
            if candidates else empty["notice"]
        ),
    }
