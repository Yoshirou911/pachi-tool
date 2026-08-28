"""店舗カルテと店内座席ヒートマップ API。"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from api.deps import HALL_REPORTS_DB, _get_reports_conn
from hall.machine_scope import clean_machine_display_name, is_smartslot_machine, normalize_machine_key
from hall.regions import region_label, region_matches
from hall.target_validation import grade_policy, walk_forward_backtest

router = APIRouter()


def _parse_date(value: str, field_name: str = "visit_date") -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, f"{field_name} は YYYY-MM-DD で指定してください") from exc


def init_layout_db() -> sqlite3.Connection:
    HALL_REPORTS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(HALL_REPORTS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hall_layout (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name TEXT NOT NULL,
            floor_name TEXT NOT NULL DEFAULT 'スロットフロア',
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            width INTEGER NOT NULL DEFAULT 1000,
            height INTEGER NOT NULL DEFAULT 700,
            source_url TEXT NOT NULL DEFAULT '',
            source_label TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'manual',
            verification_status TEXT NOT NULL DEFAULT '未確認',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(hall_name, floor_name, valid_from)
        );
        CREATE TABLE IF NOT EXISTS hall_layout_seat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            layout_id INTEGER NOT NULL REFERENCES hall_layout(id) ON DELETE CASCADE,
            seat_number INTEGER NOT NULL,
            machine_name TEXT NOT NULL DEFAULT '',
            island_name TEXT NOT NULL DEFAULT '',
            x REAL NOT NULL,
            y REAL NOT NULL,
            width REAL NOT NULL DEFAULT 48,
            height REAL NOT NULL DEFAULT 42,
            rotation REAL NOT NULL DEFAULT 0,
            UNIQUE(layout_id, seat_number)
        );
        CREATE INDEX IF NOT EXISTS idx_layout_hall_date ON hall_layout(hall_name, valid_from);
        CREATE INDEX IF NOT EXISTS idx_layout_seats_layout ON hall_layout_seat(layout_id, seat_number);
        """
    )
    conn.commit()
    return conn


class LayoutSeatInput(BaseModel):
    seat_number: int = Field(ge=1, le=99999)
    machine_name: str = Field(default="", max_length=120)
    island_name: str = Field(default="", max_length=80)
    x: float = Field(ge=0, le=5000)
    y: float = Field(ge=0, le=5000)
    width: float = Field(default=48, ge=10, le=500)
    height: float = Field(default=42, ge=10, le=500)
    rotation: float = Field(default=0, ge=-360, le=360)


class LayoutInput(BaseModel):
    hall_name: str = Field(min_length=1, max_length=120)
    floor_name: str = Field(default="スロットフロア", min_length=1, max_length=80)
    valid_from: str
    width: int = Field(default=1000, ge=320, le=5000)
    height: int = Field(default=700, ge=240, le=5000)
    source_url: str = Field(default="", max_length=1000)
    source_label: str = Field(default="", max_length=200)
    source_kind: Literal["official", "pworld", "manual", "derived", "uploaded"] = "manual"
    verification_status: Literal["確認済み", "要確認", "未確認"] = "未確認"
    notes: str = Field(default="", max_length=1000)
    seats: list[LayoutSeatInput] = Field(default_factory=list, max_length=1000)

    @field_validator("valid_from")
    @classmethod
    def _valid_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @field_validator("source_url")
    @classmethod
    def _safe_url(cls, value: str) -> str:
        if value and not value.startswith(("https://", "http://")):
            raise ValueError("source_url は http/https のURLにしてください")
        return value

    @field_validator("seats")
    @classmethod
    def _unique_seat_numbers(cls, value: list[LayoutSeatInput]) -> list[LayoutSeatInput]:
        numbers = [seat.seat_number for seat in value]
        if len(numbers) != len(set(numbers)):
            raise ValueError("同じ台番号を重複して登録できません")
        return value


class SeatResultRowInput(BaseModel):
    seat_number: int = Field(ge=1, le=99999)
    machine_name: str = Field(default="", max_length=120)
    diff_coins: int = Field(ge=-100000, le=100000)
    games: int | None = Field(default=None, ge=0, le=100000)


class SeatResultImportInput(BaseModel):
    hall_name: str = Field(min_length=1, max_length=120)
    report_date: str
    source_label: str = Field(default="現地入力", max_length=200)
    source_url: str = Field(default="", max_length=1000)
    rows: list[SeatResultRowInput] = Field(min_length=1, max_length=500)

    @field_validator("report_date")
    @classmethod
    def _valid_report_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @field_validator("source_url")
    @classmethod
    def _safe_source_url(cls, value: str) -> str:
        if value and not value.startswith(("https://", "http://")):
            raise ValueError("source_url は http/https のURLにしてください")
        return value

    @field_validator("rows")
    @classmethod
    def _unique_result_seats(cls, value: list[SeatResultRowInput]) -> list[SeatResultRowInput]:
        numbers = [row.seat_number for row in value]
        if len(numbers) != len(set(numbers)):
            raise ValueError("同じ日の同じ台番号を重複して登録できません")
        return value

def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return any(
        row[1] == column_name
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    )


def _ensure_seat_result_tables(conn: sqlite3.Connection) -> None:
    """手入力結果と取込履歴を保存できる最低限のテーブルを保証する。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hall_day_seat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name TEXT NOT NULL,
            report_date TEXT NOT NULL,
            machine_name TEXT NOT NULL,
            seat_number INTEGER NOT NULL,
            diff_coins INTEGER,
            games INTEGER,
            ev_pct REAL,
            bb_prob REAL,
            rb_prob REAL,
            source TEXT DEFAULT 'manual',
            source_url TEXT,
            scraped_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(hall_name, report_date, machine_name, seat_number)
        );
        CREATE TABLE IF NOT EXISTS seat_result_import (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name TEXT NOT NULL,
            report_date TEXT NOT NULL,
            source_label TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            submitted_rows INTEGER NOT NULL DEFAULT 0,
            inserted_rows INTEGER NOT NULL DEFAULT 0,
            updated_rows INTEGER NOT NULL DEFAULT 0,
            skipped_rows INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_seat_result_import_hall_date
            ON seat_result_import(hall_name, report_date);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hall_day_seat)").fetchall()}
    if "source" not in columns:
        # 既存行は由来不明として扱い、手入力で上書きしない。
        conn.execute("ALTER TABLE hall_day_seat ADD COLUMN source TEXT DEFAULT 'unknown'")
    if "source_url" not in columns:
        conn.execute("ALTER TABLE hall_day_seat ADD COLUMN source_url TEXT")
    conn.commit()


def _layout_machine_by_seat(conn: sqlite3.Connection, hall_name: str, report_date: str) -> dict[int, str]:
    row = conn.execute(
        """SELECT id FROM hall_layout WHERE hall_name=? AND valid_from<=?
           AND (valid_to IS NULL OR valid_to>=?) ORDER BY valid_from DESC LIMIT 1""",
        (hall_name, report_date, report_date),
    ).fetchone()
    if not row:
        return {}
    return {
        int(item[0]): item[1]
        for item in conn.execute(
            "SELECT seat_number, machine_name FROM hall_layout_seat WHERE layout_id=?",
            (row[0],),
        ).fetchall()
        if item[1]
    }


def _daily_source_rows(conn: sqlite3.Connection, hall_name: str, start: date, end: date) -> tuple[list[dict], str]:
    machine_rows = []
    if _table_exists(conn, "hall_day_machine"):
        unit_expression = "COALESCE(NULLIF(unit_count, 0), 1)" if _column_exists(
            conn, "hall_day_machine", "unit_count"
        ) else "1"
        machine_rows = conn.execute(
            f"""SELECT report_date, machine_name, avg_diff_coins AS diff, win_rate_pct,
                       source_url, {unit_expression} AS unit_count
               FROM hall_day_machine
               WHERE hall_name=? AND report_date BETWEEN ? AND ?
                 AND machine_name != '_NODATA_' AND avg_diff_coins IS NOT NULL
               ORDER BY report_date""",
            (hall_name, start.isoformat(), end.isoformat()),
        ).fetchall()
    if machine_rows:
        return [dict(row) for row in machine_rows], "みんレポ・公開店舗データ"
    if not _table_exists(conn, "hall_day_seat"):
        return [], ""
    seat_rows = conn.execute(
        """SELECT report_date, machine_name, diff_coins AS diff, NULL AS win_rate_pct, source_url
           FROM hall_day_seat
           WHERE hall_name=? AND report_date BETWEEN ? AND ?
             AND machine_name != '_NODATA_' AND diff_coins IS NOT NULL
           ORDER BY report_date""",
        (hall_name, start.isoformat(), end.isoformat()),
    ).fetchall()
    return [dict(row) for row in seat_rows], "台番号別公開データ"


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _profile_score(values: list[float], baseline: float, scale: float) -> int:
    if not values:
        return 0
    return max(0, min(100, round(50 + (_average(values) - baseline) / max(scale, 80) * 18)))


def _bounded(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _hall_market_assessment(daily: list[dict], reference: date) -> dict:
    """公開実績から店舗全体の還元・回収傾向を保守的に評価する。"""
    if not daily:
        return {
            "label": "判定保留", "score": 0, "tone": "hold", "sample_days": 0,
            "notice": "公開実績がないため判定できません。",
        }
    recent = daily[-min(30, len(daily)):]
    long_values = [float(item["avg_diff"]) for item in daily]
    recent_values = [float(item["avg_diff"]) for item in recent]
    long_avg = _average(long_values)
    recent_avg = _average(recent_values)
    long_positive = sum(value > 0 for value in long_values) / len(long_values) * 100
    recent_positive = sum(value > 0 for value in recent_values) / len(recent_values) * 100
    latest = date.fromisoformat(daily[-1]["date"])
    age_days = max(0, (reference - latest).days)
    long_score = _bounded(50 + long_avg / 4)
    recent_score = _bounded(50 + recent_avg / 4)
    sample_score = _bounded(len(daily) / 90 * 100)
    freshness_score = _bounded(100 - age_days * 3)
    score = round(
        long_score * 0.30
        + long_positive * 0.20
        + recent_score * 0.20
        + recent_positive * 0.15
        + sample_score * 0.10
        + freshness_score * 0.05
    )
    if len(daily) < 30 or age_days > 45:
        label, tone = "判定保留", "hold"
    elif score >= 70 and long_avg >= 50 and recent_avg > 0 and long_positive >= 55:
        label, tone = "優良傾向", "excellent"
    elif score >= 58 and long_avg >= 0 and recent_avg >= 0:
        label, tone = "還元寄り", "good"
    elif long_avg <= -60 and recent_avg <= -60 and long_positive < 30:
        label, tone = "回収傾向", "danger"
    elif long_avg < 0 and recent_avg < 0 and long_positive < 40:
        label, tone = "回収寄り", "caution"
    elif score >= 45:
        label, tone = "標準・日付選び", "neutral"
    elif score >= 35:
        label, tone = "回収寄り", "caution"
    else:
        label, tone = "回収傾向", "danger"
    reasons = [
        f"長期{len(daily)}日：平均{long_avg:+.0f}枚・プラス日率{long_positive:.1f}%",
        f"直近{len(recent)}日：平均{recent_avg:+.0f}枚・プラス日率{recent_positive:.1f}%",
    ]
    if age_days > 14:
        reasons.append(f"最新実績から{age_days}日経過")
    return {
        "label": label,
        "score": score,
        "tone": tone,
        "sample_days": len(daily),
        "data_age_days": age_days,
        "long_term": {"avg_diff": round(long_avg), "positive_day_rate": round(long_positive, 1)},
        "recent30": {"avg_diff": round(recent_avg), "positive_day_rate": round(recent_positive, 1), "sample_days": len(recent)},
        "reasons": reasons,
        "notice": "公開実績上の傾向評価であり、店舗の営業方針や将来結果を断定するものではありません。",
    }


def _machine_strength_validation(machine_rows: list[dict], target: date) -> dict:
    """同一機種の日別実績をまとめ、対象日を先読みしない成績を返す。"""
    by_date: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for row in machine_rows:
        report_date = str(row["report_date"])
        if date.fromisoformat(report_date) >= target:
            continue
        units = max(1, int(row.get("unit_count") or 1))
        by_date[report_date].append((float(row["diff"] or 0), units))
    points = []
    for report_date, values in sorted(by_date.items()):
        total_units = sum(units for _, units in values)
        points.append((
            date.fromisoformat(report_date),
            sum(value * units for value, units in values) / total_units,
        ))
    return walk_forward_backtest(points, max_test_days=60)


@router.get("/api/hall/trend_profile", tags=["hall"])
def get_hall_trend_profile(
    hall_name: str = Query(..., min_length=1),
    visit_date: str = Query(...),
    days: int = Query(365, ge=30, le=730),
) -> dict:
    """店舗のカレンダー・曜日・日付末尾・機種傾向を一つのカルテにする。"""
    target = _parse_date(visit_date)
    reference = min(target - timedelta(days=1), date.today())
    start = reference - timedelta(days=days - 1)
    conn = _get_reports_conn()
    if conn is None:
        return {"hall_name": hall_name, "visit_date": visit_date, "status": "データなし", "sample_days": 0}
    try:
        rows, source_label = _daily_source_rows(conn, hall_name, start, reference)
    finally:
        conn.close()
    if not rows:
        return {
            "hall_name": hall_name, "visit_date": visit_date, "status": "データなし",
            "sample_days": 0, "source_label": "", "notice": "この店舗の公開実績をまだ取得できていません。",
        }

    by_day: dict[str, list[dict]] = defaultdict(list)
    by_machine: dict[str, list[dict]] = defaultdict(list)
    source_urls: set[str] = set()
    for row in rows:
        by_day[row["report_date"]].append(row)
        if is_smartslot_machine(row["machine_name"]):
            machine_key = normalize_machine_key(row["machine_name"])
            if machine_key:
                by_machine[machine_key].append(row)
        if row.get("source_url"):
            source_urls.add(row["source_url"])

    daily = []
    for day_value, day_rows in sorted(by_day.items()):
        weighted_diffs = [
            (float(item["diff"] or 0), max(1, int(item.get("unit_count") or 1)))
            for item in day_rows
        ]
        total_units = sum(units for _, units in weighted_diffs)
        daily.append({
            "date": day_value,
            "avg_diff": round(sum(value * units for value, units in weighted_diffs) / total_units),
            "positive_rate": round(
                sum(units for value, units in weighted_diffs if value > 0) / total_units * 100,
                1,
            ),
            "machine_count": len(day_rows),
            "unit_count": total_units,
        })
    day_values = [float(item["avg_diff"]) for item in daily]
    baseline = _average(day_values)
    variance = _average([(value - baseline) ** 2 for value in day_values])
    scale = max(math.sqrt(variance), 100)
    for item in daily:
        item["score"] = max(0, min(100, round(50 + (item["avg_diff"] - baseline) / scale * 16)))

    weekday_names = ["月", "火", "水", "木", "金", "土", "日"]
    weekday_values: dict[int, list[float]] = defaultdict(list)
    digit_values: dict[int, list[float]] = defaultdict(list)
    block_values: dict[str, list[float]] = defaultdict(list)
    for item in daily:
        parsed = date.fromisoformat(item["date"])
        weekday_values[parsed.weekday()].append(item["avg_diff"])
        digit_values[parsed.day % 10].append(item["avg_diff"])
        block = "上旬" if parsed.day <= 9 else "中旬" if parsed.day <= 19 else "下旬"
        block_values[block].append(item["avg_diff"])

    weekday_profile = [
        {"weekday": weekday_names[index], "avg_diff": round(_average(values)), "sample_days": len(values),
         "score": _profile_score(values, baseline, scale)}
        for index, values in sorted(weekday_values.items())
    ]
    digit_profile = [
        {"digit": digit, "avg_diff": round(_average(values)), "sample_days": len(values),
         "score": _profile_score(values, baseline, scale)}
        for digit, values in sorted(digit_values.items())
    ]
    date_blocks = [
        {"block": block, "avg_diff": round(_average(values)), "sample_days": len(values),
         "score": _profile_score(values, baseline, scale)}
        for block, values in block_values.items()
    ]

    cutoff_recent = reference - timedelta(days=13)
    machine_profile = []
    for machine_key, machine_rows in by_machine.items():
        machine_name = clean_machine_display_name(max(
            machine_rows,
            key=lambda row: (str(row["report_date"]), str(row["machine_name"])),
        )["machine_name"])
        values = [float(row["diff"] or 0) for row in machine_rows]
        weighted_values = [
            (float(row["diff"] or 0), max(1, int(row.get("unit_count") or 1)))
            for row in machine_rows
        ]
        recent = [float(row["diff"] or 0) for row in machine_rows if date.fromisoformat(row["report_date"]) >= cutoff_recent]
        older = [float(row["diff"] or 0) for row in machine_rows if date.fromisoformat(row["report_date"]) < cutoff_recent]
        avg = sum(value * units for value, units in weighted_values) / sum(
            units for _, units in weighted_values
        )
        sample_days = len({row["report_date"] for row in machine_rows})
        reliability = min(1.0, sample_days / 20)
        adjusted_avg = avg * reliability + baseline * (1 - reliability)
        strength_margin = round(adjusted_avg - baseline)
        target_weekday_values = [
            float(row["diff"] or 0) for row in machine_rows
            if date.fromisoformat(row["report_date"]).weekday() == target.weekday()
        ]
        machine_profile.append({
            "machine_key": machine_key,
            "machine_name": machine_name,
            "avg_diff": round(adjusted_avg),
            "raw_avg_diff": round(avg),
            "strength_margin": strength_margin,
            "positive_rate": round(sum(value > 0 for value in values) / len(values) * 100, 1),
            "sample_days": sample_days,
            "reliability_pct": round(reliability * 100),
            "visit_weekday": weekday_names[target.weekday()],
            "visit_weekday_avg": round(_average(target_weekday_values)) if target_weekday_values else None,
            "visit_weekday_days": len(target_weekday_values),
            "recent_avg": round(_average(recent)) if recent else None,
            "trend": round(_average(recent) - _average(older)) if recent and older else None,
            "score": max(0, min(100, round(50 + (adjusted_avg - baseline) / max(scale, 100) * 18))),
        })
    machine_profile.sort(key=lambda item: (item["score"], item["sample_days"]), reverse=True)
    machine_profile = machine_profile[:30]
    for machine in machine_profile:
        validation = _machine_strength_validation(by_machine[machine["machine_key"]], target)
        validation_passed = validation.get("trust_level") in {
            "70%実戦基準", "80%級", "90%級",
        }
        if validation_passed and machine["strength_margin"] >= 0:
            handling_label = "70%検証済み"
            handling_rank = 4
        elif (
            machine["sample_days"] >= 30
            and machine["strength_margin"] >= 100
            and machine["positive_rate"] >= 55
        ):
            handling_label = "強い扱い候補"
            handling_rank = 3
        elif machine["sample_days"] >= 20 and machine["strength_margin"] > 0:
            handling_label = "やや強め"
            handling_rank = 2
        elif machine["sample_days"] < 21:
            handling_label = "データ不足"
            handling_rank = 0
        else:
            handling_label = "通常・弱め"
            handling_rank = 1
        machine["validation"] = validation
        machine["handling_label"] = handling_label
        machine["handling_rank"] = handling_rank
        machine.pop("machine_key", None)
    machine_profile.sort(
        key=lambda item: (item["handling_rank"], item["score"], item["sample_days"]),
        reverse=True,
    )

    best_weekday = max(weekday_profile, key=lambda item: item["score"], default=None)
    best_digit = max(digit_profile, key=lambda item: item["score"], default=None)
    next_start = max(target, date.today() + timedelta(days=1))
    next_dates = []
    for offset in range(31):
        candidate = next_start + timedelta(days=offset)
        weekday = next((item for item in weekday_profile if item["weekday"] == weekday_names[candidate.weekday()]), None)
        digit = next((item for item in digit_profile if item["digit"] == candidate.day % 10), None)
        weights = []
        if weekday:
            weights.append((weekday["score"], min(3, weekday["sample_days"])))
        if digit:
            weights.append((digit["score"], min(2, digit["sample_days"])))
        score = round(sum(value * weight for value, weight in weights) / sum(weight for _, weight in weights)) if weights else 0
        next_dates.append({"date": candidate.isoformat(), "weekday": weekday_names[candidate.weekday()], "score": score,
                           "evidence": f"{weekday_names[candidate.weekday()]}曜・末尾{candidate.day % 10}"})
    next_dates.sort(key=lambda item: (item["score"], item["date"]), reverse=True)

    confidence = "高" if len(daily) >= 60 else "中" if len(daily) >= 20 else "低"
    hall_assessment = _hall_market_assessment(daily, reference)
    insights = []
    if best_weekday:
        insights.append(f"{best_weekday['weekday']}曜日が相対的に強め（{best_weekday['sample_days']}日・平均{best_weekday['avg_diff']:+,}枚）")
    if best_digit:
        insights.append(f"日付末尾{best_digit['digit']}が強め（{best_digit['sample_days']}日・平均{best_digit['avg_diff']:+,}枚）")
    if machine_profile:
        top = machine_profile[0]
        insights.append(f"機種では{top['machine_name']}が上位（{top['sample_days']}日・平均{top['avg_diff']:+,}枚）")
    if len(daily) < 20:
        insights.append("収集20日未満のため、現在のクセ判定は暫定です")

    return {
        "hall_name": hall_name,
        "visit_date": visit_date,
        "reference_date": reference.isoformat(),
        "status": "分析済み",
        "confidence": confidence,
        "sample_days": len(daily),
        "first_date": daily[0]["date"],
        "latest_date": daily[-1]["date"],
        "overall": {"avg_diff": round(baseline), "positive_day_rate": round(sum(value > 0 for value in day_values) / len(day_values) * 100, 1)},
        "hall_assessment": hall_assessment,
        "calendar": daily[-180:],
        "weekday_profile": weekday_profile,
        "digit_profile": digit_profile,
        "date_blocks": date_blocks,
        "machine_profile": machine_profile,
        "next_dates": sorted(next_dates[:8], key=lambda item: item["date"]),
        "insights": insights,
        "source_label": source_label,
        "source_urls": sorted(source_urls)[:10],
        "validation_policy": grade_policy(),
        "notice": "予測日より前に取得した公開実績だけで計算しています。結果を保証するものではありません。",
    }


@router.get("/api/hall/machine_strength_matrix", tags=["hall"])
def get_machine_strength_matrix(
    visit_date: str = Query(...),
    days: int = Query(365, ge=30, le=730),
    region: Literal["all", "shijonawate", "matsumoto_shiojiri", "nagano", "osaka"] = "shijonawate",
    limit: int = Query(12, ge=1, le=20),
) -> dict:
    """地域内の店舗ごとに、強く扱うスマスロ機種を横並びで返す。"""
    _parse_date(visit_date)
    conn = _get_reports_conn()
    if conn is None:
        return {
            "visit_date": visit_date, "region": region, "region_label": region_label(region),
            "halls": [], "validation_policy": grade_policy(),
        }
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        hall_prefectures: dict[str, str | None] = {}
        if "scrape_hall_config" in tables:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(scrape_hall_config)")
            }
            prefecture_sql = "prefecture" if "prefecture" in columns else "NULL"
            enabled_sql = "WHERE enabled=1" if "enabled" in columns else ""
            hall_prefectures = {
                row[0]: row[1] for row in conn.execute(
                    f"SELECT hall_name,{prefecture_sql} FROM scrape_hall_config {enabled_sql}"
                ).fetchall()
            }
        if not hall_prefectures and "hall_day_machine" in tables:
            hall_prefectures = {
                row[0]: None for row in conn.execute(
                    "SELECT DISTINCT hall_name FROM hall_day_machine"
                ).fetchall()
            }
    finally:
        conn.close()

    hall_names = sorted(
        hall_name for hall_name, prefecture in hall_prefectures.items()
        if region_matches(hall_name, prefecture, region)
    )[:limit]
    halls = []
    for hall_name in hall_names:
        profile = get_hall_trend_profile(
            hall_name=hall_name, visit_date=visit_date, days=days
        )
        if profile.get("status") != "分析済み":
            continue
        all_machines = profile.get("machine_profile", [])
        machines = all_machines[:5]
        halls.append({
            "hall_name": hall_name,
            "sample_days": profile.get("sample_days", 0),
            "confidence": profile.get("confidence", "低"),
            "overall": profile.get("overall", {}),
            "hall_assessment": profile.get("hall_assessment", {}),
            "verified_machine_count": sum(
                machine.get("handling_label") == "70%検証済み" for machine in all_machines
            ),
            "strong_machine_count": sum(
                machine.get("handling_rank", 0) >= 3 for machine in all_machines
            ),
            "top_machines": machines,
        })
    halls.sort(
        key=lambda item: (
            item["verified_machine_count"], item.get("hall_assessment", {}).get("score", 0),
            item["strong_machine_count"],
            item["top_machines"][0]["score"] if item["top_machines"] else 0,
        ),
        reverse=True,
    )
    return {
        "visit_date": visit_date,
        "region": region,
        "region_label": region_label(region),
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "halls": halls,
        "validation_policy": grade_policy(),
        "notice": (
            "70%検証済みは、先読みなし検証の成功率・95%下限・直近成績・品質を"
            "すべて通過した店舗×機種だけです。"
        ),
    }


def _serialize_layout(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    seats = conn.execute(
        """SELECT seat_number, machine_name, island_name, x, y, width, height, rotation
           FROM hall_layout_seat WHERE layout_id=? ORDER BY seat_number""",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"], "hall_name": row["hall_name"], "floor_name": row["floor_name"],
        "valid_from": row["valid_from"], "valid_to": row["valid_to"], "width": row["width"], "height": row["height"],
        "source_url": row["source_url"], "source_label": row["source_label"], "source_kind": row["source_kind"],
        "verification_status": row["verification_status"], "notes": row["notes"], "generated": False,
        "seats": [dict(seat) for seat in seats],
    }


@router.get("/api/layouts", tags=["layout"])
def list_layouts(hall_name: str = Query("")) -> list[dict]:
    conn = init_layout_db()
    try:
        if hall_name:
            rows = conn.execute("SELECT * FROM hall_layout WHERE hall_name=? ORDER BY valid_from DESC", (hall_name,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM hall_layout ORDER BY hall_name, valid_from DESC").fetchall()
        return [_serialize_layout(conn, row) for row in rows]
    finally:
        conn.close()


@router.post("/api/layouts", tags=["layout"])
def save_layout(body: LayoutInput) -> dict:
    conn = init_layout_db()
    try:
        previous = conn.execute(
            "SELECT id FROM hall_layout WHERE hall_name=? AND floor_name=? AND valid_from=?",
            (body.hall_name, body.floor_name, body.valid_from),
        ).fetchone()
        if previous:
            layout_id = previous[0]
            conn.execute(
                """UPDATE hall_layout SET width=?, height=?, source_url=?, source_label=?, source_kind=?,
                          verification_status=?, notes=?, updated_at=datetime('now','localtime') WHERE id=?""",
                (body.width, body.height, body.source_url, body.source_label, body.source_kind,
                 body.verification_status, body.notes, layout_id),
            )
            conn.execute("DELETE FROM hall_layout_seat WHERE layout_id=?", (layout_id,))
        else:
            cursor = conn.execute(
                """INSERT INTO hall_layout
                   (hall_name,floor_name,valid_from,width,height,source_url,source_label,source_kind,verification_status,notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (body.hall_name, body.floor_name, body.valid_from, body.width, body.height, body.source_url,
                 body.source_label, body.source_kind, body.verification_status, body.notes),
            )
            layout_id = cursor.lastrowid
        conn.executemany(
            """INSERT INTO hall_layout_seat
               (layout_id,seat_number,machine_name,island_name,x,y,width,height,rotation)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(layout_id, seat.seat_number, seat.machine_name, seat.island_name, seat.x, seat.y,
              seat.width, seat.height, seat.rotation) for seat in body.seats],
        )
        conn.commit()
        row = conn.execute("SELECT * FROM hall_layout WHERE id=?", (layout_id,)).fetchone()
        return _serialize_layout(conn, row)
    finally:
        conn.close()


@router.post("/api/layouts/seat_results", tags=["layout"])
def import_seat_results(body: SeatResultImportInput) -> dict:
    """現地入力またはCSVの台番号別結果を保存する。公開取得データは上書きしない。"""
    conn = init_layout_db()
    conn.row_factory = sqlite3.Row
    _ensure_seat_result_tables(conn)
    manual_sources = {"manual", "manual_csv", "field_entry"}
    machine_by_seat = _layout_machine_by_seat(conn, body.hall_name, body.report_date)
    inserted = 0
    updated = 0
    skipped = 0
    skipped_seats: list[int] = []
    unresolved_seats: list[int] = []
    try:
        for item in body.rows:
            existing = conn.execute(
                """SELECT machine_name, source FROM hall_day_seat
                   WHERE hall_name=? AND report_date=? AND seat_number=?""",
                (body.hall_name, body.report_date, item.seat_number),
            ).fetchall()
            if any((row["source"] or "unknown") not in manual_sources for row in existing):
                skipped += 1
                skipped_seats.append(item.seat_number)
                continue
            machine_name = item.machine_name.strip() or machine_by_seat.get(item.seat_number, "")
            if not machine_name:
                unresolved_seats.append(item.seat_number)
                continue
            was_manual = bool(existing)
            if existing:
                conn.execute(
                    "DELETE FROM hall_day_seat WHERE hall_name=? AND report_date=? AND seat_number=?",
                    (body.hall_name, body.report_date, item.seat_number),
                )
            conn.execute(
                """INSERT INTO hall_day_seat
                   (hall_name,report_date,machine_name,seat_number,diff_coins,games,source,source_url)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    body.hall_name,
                    body.report_date,
                    machine_name,
                    item.seat_number,
                    item.diff_coins,
                    item.games,
                    "manual_csv" if len(body.rows) > 1 else "field_entry",
                    body.source_url,
                ),
            )
            if was_manual:
                updated += 1
            else:
                inserted += 1
        if unresolved_seats:
            conn.rollback()
            raise HTTPException(
                400,
                "機種名が未入力で、店内マップからも解決できない台があります: "
                + ", ".join(map(str, unresolved_seats[:20])),
            )
        conn.execute(
            """INSERT INTO seat_result_import
               (hall_name,report_date,source_label,source_url,submitted_rows,inserted_rows,updated_rows,skipped_rows)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                body.hall_name,
                body.report_date,
                body.source_label.strip() or "現地入力",
                body.source_url,
                len(body.rows),
                inserted,
                updated,
                skipped,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "hall_name": body.hall_name,
        "report_date": body.report_date,
        "submitted": len(body.rows),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "skipped_seats": skipped_seats,
        "message": f"{inserted}件追加・{updated}件更新" + (f"・{skipped}件は公開データを優先" if skipped else ""),
    }


@router.get("/api/layouts/seat_results", tags=["layout"])
def list_seat_results(
    hall_name: str = Query(..., min_length=1),
    report_date: str = Query(...),
) -> dict:
    """指定日の台番号結果を、現地での確認用に返す。"""
    _parse_date(report_date, "report_date")
    conn = init_layout_db()
    conn.row_factory = sqlite3.Row
    _ensure_seat_result_tables(conn)
    try:
        rows = conn.execute(
            """SELECT seat_number,machine_name,diff_coins,games,source,source_url
               FROM hall_day_seat WHERE hall_name=? AND report_date=? AND seat_number>0
               ORDER BY seat_number""",
            (hall_name, report_date),
        ).fetchall()
    finally:
        conn.close()
    return {"hall_name": hall_name, "report_date": report_date, "count": len(rows), "rows": [dict(row) for row in rows]}


def _generated_layout(seat_rows: list[sqlite3.Row], hall_name: str, valid_from: str) -> dict:
    latest_by_seat = {}
    for row in seat_rows:
        latest_by_seat[int(row["seat_number"])] = row["machine_name"]
    grouped: dict[str, list[int]] = defaultdict(list)
    for seat_number, machine_name in sorted(latest_by_seat.items()):
        grouped[machine_name].append(seat_number)
    seats = []
    row_index = 0
    for machine_name, numbers in grouped.items():
        for chunk_start in range(0, len(numbers), 14):
            chunk = numbers[chunk_start:chunk_start + 14]
            y = 70 + row_index * 100
            for column, seat_number in enumerate(chunk):
                seats.append({"seat_number": seat_number, "machine_name": machine_name, "island_name": machine_name,
                              "x": 55 + column * 64, "y": y, "width": 52, "height": 44, "rotation": 0})
            row_index += 1
    return {
        "id": None, "hall_name": hall_name, "floor_name": "自動配置（要確認）", "valid_from": valid_from,
        "valid_to": None, "width": 1000, "height": max(420, 120 + row_index * 100), "source_url": "",
        "source_label": "台番号別公開データから自動整列", "source_kind": "derived", "verification_status": "要確認",
        "notes": "実際の店内位置ではありません。公式マップまたは現地確認で座席位置を修正してください。",
        "generated": True, "seats": seats,
    }


def _seat_level(score: int | None) -> tuple[str, str]:
    if score is None:
        return "データ不足", "#64748b"
    if score >= 75:
        return "かなり熱い", "#f43f5e"
    if score >= 62:
        return "狙い目", "#f97316"
    if score >= 52:
        return "注目", "#eab308"
    if score >= 42:
        return "慎重", "#38bdf8"
    return "弱め", "#475569"


@router.get("/api/layouts/seat_heat", tags=["layout"])
def get_seat_heat(
    hall_name: str = Query(..., min_length=1),
    visit_date: str = Query(...),
    days: int = Query(90, ge=14, le=365),
) -> dict:
    """予測日時点で利用可能だったデータだけを使い、座席ごとの熱量を返す。"""
    target = _parse_date(visit_date)
    reference = min(target - timedelta(days=1), date.today())
    start = reference - timedelta(days=days - 1)
    conn = init_layout_db()
    floor_map_sources: list[dict] = []
    try:
        if _table_exists(conn, "hall_floor_map_snapshot"):
            latest_map_date = conn.execute(
                "SELECT MAX(snapshot_date) FROM hall_floor_map_snapshot WHERE hall_name=?",
                (hall_name,),
            ).fetchone()[0]
            if latest_map_date:
                floor_map_sources = [
                    dict(row) for row in conn.execute(
                        """SELECT floor_index,image_url,page_url,snapshot_date
                             FROM hall_floor_map_snapshot
                            WHERE hall_name=? AND snapshot_date=? ORDER BY floor_index""",
                        (hall_name, latest_map_date),
                    ).fetchall()
                ]
        layout_row = conn.execute(
            """SELECT * FROM hall_layout WHERE hall_name=? AND valid_from<=?
               AND (valid_to IS NULL OR valid_to>=?) ORDER BY valid_from DESC LIMIT 1""",
            (hall_name, target.isoformat(), target.isoformat()),
        ).fetchone()
        if _table_exists(conn, "hall_day_seat"):
            history_rows = conn.execute(
                """SELECT report_date,machine_name,seat_number,diff_coins,games,source_url
                   FROM hall_day_seat WHERE hall_name=? AND report_date BETWEEN ? AND ?
                     AND seat_number>0 AND machine_name!='_NODATA_' AND diff_coins IS NOT NULL
                   ORDER BY report_date""",
                (hall_name, start.isoformat(), reference.isoformat()),
            ).fetchall()
            exact_rows = conn.execute(
                """SELECT seat_number,diff_coins,games FROM hall_day_seat
                   WHERE hall_name=? AND report_date=? AND seat_number>0 AND diff_coins IS NOT NULL""",
                (hall_name, target.isoformat()),
            ).fetchall()
        else:
            history_rows = []
            exact_rows = []
        if layout_row:
            layout = _serialize_layout(conn, layout_row)
        elif history_rows:
            layout = _generated_layout(history_rows, hall_name, reference.isoformat())
        else:
            layout = {
                "id": None, "hall_name": hall_name, "floor_name": "スロットフロア", "valid_from": reference.isoformat(),
                "valid_to": None, "width": 1000, "height": 600, "source_url": "", "source_label": "",
                "source_kind": "manual", "verification_status": "未確認", "notes": "", "generated": False, "seats": [],
            }
    finally:
        conn.close()

    by_seat: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in history_rows:
        by_seat[int(row["seat_number"])].append(row)
    actual_by_seat = {int(row["seat_number"]): {"diff": row["diff_coins"], "games": row["games"]} for row in exact_rows}
    weekday = target.weekday()
    digit = target.day % 10
    raw = {}
    for seat_number, rows in by_seat.items():
        values = [float(row["diff_coins"] or 0) for row in rows]
        recent = [float(row["diff_coins"] or 0) for row in rows if date.fromisoformat(row["report_date"]) >= reference - timedelta(days=13)]
        same_weekday = [float(row["diff_coins"] or 0) for row in rows if date.fromisoformat(row["report_date"]).weekday() == weekday]
        same_digit = [float(row["diff_coins"] or 0) for row in rows if date.fromisoformat(row["report_date"]).day % 10 == digit]
        weights = [(values, .35), (recent, .25), (same_weekday, .25), (same_digit, .15)]
        available = [(items, weight) for items, weight in weights if items]
        estimate = sum(_average(items) * weight for items, weight in available) / sum(weight for _, weight in available)
        positive_rate = sum(value > 0 for value in values) / len(values) * 100
        raw_score = max(0, min(100, 50 + estimate / 28 + (positive_rate - 50) * .18))
        shrink = min(1.0, len({row["report_date"] for row in rows}) / 10)
        score = round(50 + (raw_score - 50) * shrink)
        raw[seat_number] = {
            "score": score, "estimate": round(estimate), "positive_rate": round(positive_rate, 1),
            "sample_days": len({row["report_date"] for row in rows}),
            "weekday_days": len({row["report_date"] for row in rows if date.fromisoformat(row["report_date"]).weekday() == weekday}),
            "recent_avg": round(_average(recent)) if recent else None,
            "machine_name": rows[-1]["machine_name"],
        }

    layout_seats_by_number = {int(item["seat_number"]): item for item in layout["seats"]}
    machine_scores: dict[str, list[int]] = defaultdict(list)
    for item in raw.values():
        machine_scores[item["machine_name"]].append(item["score"])
    output = []
    for seat_number, seat in layout_seats_by_number.items():
        metric = raw.get(seat_number)
        if metric:
            adjacent = [raw[number]["score"] for number in (seat_number - 1, seat_number + 1) if number in raw]
            machine_avg = _average(machine_scores.get(metric["machine_name"], [metric["score"]]))
            final_score = round(metric["score"] * .75 + (_average(adjacent) if adjacent else metric["score"]) * .15 + machine_avg * .10)
            level, color = _seat_level(final_score)
            reasons = [f"過去{metric['sample_days']}日・推定{metric['estimate']:+,}枚", f"プラス率{metric['positive_rate']}%"]
            if metric["weekday_days"]:
                reasons.append(f"同曜日{metric['weekday_days']}日を反映")
        else:
            final_score, level, color, reasons = None, *_seat_level(None), ["台番号別実績がまだありません"]
        output.append({
            **seat,
            "machine_name": metric["machine_name"] if metric else seat.get("machine_name", ""),
            "score": final_score, "heat_level": level, "color": color, "reasons": reasons,
            "estimate": metric["estimate"] if metric else None,
            "positive_rate": metric["positive_rate"] if metric else None,
            "sample_days": metric["sample_days"] if metric else 0,
            "actual": actual_by_seat.get(seat_number),
        })
    output.sort(key=lambda item: (item["score"] is not None, item["score"] or 0), reverse=True)
    return {
        "hall_name": hall_name, "visit_date": visit_date, "reference_date": reference.isoformat(), "analysis_days": days,
        "layout": {key: value for key, value in layout.items() if key != "seats"}, "seats": output,
        "floor_map_sources": floor_map_sources,
        "data_coverage": {"history_rows": len(history_rows), "seat_count": len(by_seat), "exact_result_count": len(exact_rows)},
        "status": "分析済み" if output and history_rows else "座席データ待ち" if layout["seats"] else "レイアウト未登録",
        "notice": layout["notes"] or "予測日より前の公開実績だけで計算しています。",
    }
