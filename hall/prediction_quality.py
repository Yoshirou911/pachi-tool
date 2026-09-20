"""Non-destructive input audit. Missing values are never interpreted as zero.

Calendar coverage describes published days, NOT confirmed opening days.
Conflicting copies of a natural key are quarantined, not averaged together.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date

from hall.names import canonical_hall_name
from hall.machine_scope import normalize_machine_key


def audit_rows(rows, *, cutoff: date, scope: str = "machine") -> tuple[list[dict], dict]:
    diff_field, games_field = ("diff_coins", "games") if scope == "seat" else ("avg_diff_coins", "avg_games")
    grouped = defaultdict(list)
    reasons = Counter()
    raw_count = 0
    for original in rows:
        raw_count += 1
        row = dict(original)
        try:
            day = date.fromisoformat(str(row.get("report_date", "")))
            if day > cutoff:
                reasons["cutoff_or_future"] += 1
                continue
            row["report_date"] = day.isoformat()
            row["hall_name"] = canonical_hall_name(row.get("hall_name"))
            key = (row["hall_name"], row["report_date"], normalize_machine_key(row.get("machine_name")))
            if not key[0] or not key[2]:
                raise ValueError("name")
            if row.get(diff_field) is None:
                reasons["missing_diff"] += 1
                continue
            fields = (diff_field, games_field, "win_rate_pct", "unit_count")
            for field in fields:
                value = row.get(field)
                if value is None:
                    continue
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError("nonfinite")
                if field == games_field and not 0 <= number <= 30000:
                    raise ValueError("games")
                if field == "unit_count" and (number < 1 or number != int(number)):
                    raise ValueError("units")
                if field == "win_rate_pct" and not 0 <= number <= 100:
                    raise ValueError("rate")
                row[field] = number
            if scope == "machine" and row.get("unit_count") is None:
                reasons["missing_units"] += 1
                continue
            if scope == "seat":
                seat = float(row.get("seat_number", 0))
                if not math.isfinite(seat) or seat < 1 or seat != int(seat):
                    raise ValueError("seat")
                row["seat_number"] = int(seat)
                # A seat cannot contain two machines on the same day.
                key = (key[0], key[1], row["seat_number"])
            grouped[key].append(row)
        except (ValueError, TypeError, OverflowError):
            reasons["invalid_record"] += 1

    clean = []
    conflict_keys = 0
    duplicate_rows = 0
    for copies in grouped.values():
        compare = (diff_field, games_field, "unit_count", "win_rate_pct")
        conflict = any(len({r.get(f) for r in copies if r.get(f) is not None}) > 1 for f in compare)
        if scope == "seat":
            conflict |= len({normalize_machine_key(r["machine_name"]) for r in copies}) > 1
        if conflict:
            conflict_keys += 1
            reasons["conflicting_rows"] += len(copies)
            continue
        merged = dict(sorted(copies, key=lambda r: str(r.get("source_url", "")))[0])
        for field in compare:
            merged[field] = next((r[field] for r in copies if r.get(field) is not None), None)
        duplicate_rows += len(copies) - 1
        merged["source_urls"] = sorted({r["source_url"] for r in copies if r.get("source_url")})
        clean.append(merged)
    clean.sort(key=lambda r: (r["report_date"], r["hall_name"], normalize_machine_key(r["machine_name"]), r.get("seat_number", 0)))
    report = {
        "raw_rows": raw_count, "usable_rows": len(clean),
        "excluded_rows": sum(reasons.values()), "duplicate_rows": duplicate_rows,
        "conflict_keys": conflict_keys, "exclusion_reasons": dict(reasons),
        "cutoff_date": cutoff.isoformat(), "scope": scope,
        "input_sha256": hashlib.sha256(json.dumps(clean, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
    }
    return clean, report


def coverage(rows, *, cutoff: date, scope: str = "machine") -> dict:
    rows = list(rows)
    days = sorted({r["report_date"] for r in rows})
    games_field = "games" if scope == "seat" else "avg_games"
    span = (cutoff - date.fromisoformat(days[0])).days + 1 if days else 0
    freshness = (cutoff - date.fromisoformat(days[-1])).days if days else None
    games_pct = round(sum(r.get(games_field) is not None for r in rows) / len(rows) * 100) if rows else 0
    calendar_pct = round(len(days) / span * 100) if span else 0
    weekdays = Counter(date.fromisoformat(day).weekday() for day in days)
    zero_played = sum((r.get(games_field) or 0) > 0 and r.get("avg_diff_coins", r.get("diff_coins")) == 0 for r in rows)
    known_played = sum((r.get(games_field) or 0) > 0 for r in rows)
    blockers = []
    if len(days) < 14:
        blockers.append("実績14日未満")
    if calendar_pct < 40:
        blockers.append("公開日の偏り・欠測が多い")
    if games_pct < 60:
        blockers.append("G数確認率60%未満")
    if freshness is None or freshness > 14:
        blockers.append("直近14日以内の実績なし")
    if known_played >= 30 and zero_played / known_played >= .3:
        blockers.append("稼働あり差枚0が多く欠損の疑い")
    return {
        "sample_days": len(days), "first_date": days[0] if days else None,
        "last_date": days[-1] if days else None, "stale_days": freshness,
        "calendar_coverage_pct": calendar_pct, "unobserved_calendar_days": max(0, span - len(days)),
        "games_known_pct": games_pct, "weekday_days": {str(i): weekdays[i] for i in range(7)},
        "analysis_eligible": not blockers, "blockers": blockers,
        "label": "入力品質基準通過" if not blockers else "参考限定",
        "notice": "未観測日は休業・非公開も含みます。品質通過は予測的中や店舗全台の網羅を保証しません。",
    }
