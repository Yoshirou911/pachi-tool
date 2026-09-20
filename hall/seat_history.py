"""Observed seat tenures, not inferred physical-machine serial numbers.

An absent public row does not mean removal. A change is bounded by observations,
never presented as an exact installation date. Raw result rows are not modified.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date

from hall.machine_scope import normalize_machine_key
from hall.names import canonical_hall_name


def known_layouts(conn, cutoff):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "hall_layout" not in tables or "hall_layout_seat" not in tables:
        return []
    conn.row_factory = sqlite3.Row
    layouts = {}
    for r in conn.execute("SELECT * FROM hall_layout"):
        item = dict(r)
        item["seats"] = [dict(s) for s in conn.execute("SELECT * FROM hall_layout_seat WHERE layout_id=?", (r["id"],))]
        layouts[r["id"]] = item
    if "hall_layout_revision" in tables:
        # Once versioning starts, a later correction must not leak into an older
        # cutoff. Legacy snapshots become known when first archived, not retroactively.
        for layout_id in list(layouts):
            revisions = conn.execute("SELECT payload,recorded_at FROM hall_layout_revision WHERE layout_id=? ORDER BY id", (layout_id,)).fetchall()
            if revisions:
                known = [r for r in revisions if r["recorded_at"][:10] <= cutoff.isoformat()]
                if known:
                    layouts[layout_id] = json.loads(known[-1]["payload"])
                    layouts[layout_id]["known_at"] = known[-1]["recorded_at"]
                else:
                    del layouts[layout_id]
    result = []
    for item in layouts.values():
        if item.get("valid_from", "9999") > cutoff.isoformat():
            continue
        item.setdefault("known_at", item.get("updated_at") or item.get("created_at"))
        result.append(item)
    return result


def observations_from_layouts(layouts):
    observations = []
    for item in layouts:
        if item.get("verification_status") != "確認済み" or item.get("source_kind") == "derived":
            continue
        for seat in item["seats"]:
            observations.append({**seat, "hall_name": canonical_hall_name(item["hall_name"]),
                                 "report_date": item["valid_from"], "valid_to": item.get("valid_to"),
                                 "floor_name": item["floor_name"], "source_url": item.get("source_url", ""),
                                 "kind": "layout", "layout_id": item["id"]})
    return observations


def layout_observations(conn, cutoff):
    return observations_from_layouts(known_layouts(conn, cutoff))


def build_seat_history(rows, layouts=(), *, cutoff, gap_days=30):
    """Return training rows limited to the last unambiguous observed tenure."""
    events = defaultdict(lambda: defaultdict(list))
    originals = []
    originals_by_seat = defaultdict(list)
    for source, is_layout in ((rows, False), (layouts, True)):
        for original in source:
            r = dict(original)
            try:
                day = date.fromisoformat(str(r["report_date"]))
                number = int(r["seat_number"])
                if number < 1 or float(r["seat_number"]) != number or day > cutoff:
                    continue
                hall = canonical_hall_name(r.get("hall_name", ""))
                machine = normalize_machine_key(r.get("machine_name", ""))
                if not hall or not machine or str(r.get("machine_name", "")).startswith('_'):
                    continue
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
            r.update(hall_name=hall, seat_number=number, report_date=day.isoformat(), machine_key=machine)
            if is_layout:
                r["position"] = tuple(r.get(k) for k in ("floor_name", "island_name", "x", "y", "rotation"))
                if r.get("row_name") or r.get("row_order") is not None:
                    r["position"] += (r.get("row_name"), r.get("row_order"))
            else:
                originals.append(r)
                originals_by_seat[(hall, number)].append(r)
            events[(hall, number)][day].append(r)
    seats, kept = [], []
    for (hall, number), by_day in sorted(events.items()):
        periods = []
        current = None
        for day, observations in sorted(by_day.items()):
            machines = {r["machine_key"] for r in observations}
            positions = {r["position"] for r in observations if "position" in r}
            conflict = len(machines) != 1 or len(positions) > 1
            position = next(iter(positions)) if len(positions) == 1 else None
            key = next(iter(machines)) if not conflict else None
            previous_day = date.fromisoformat(current["last_observed"]) if current else None
            reason = ("conflict" if conflict else "first_observation" if current is None else
                      "after_conflict" if current["machine_key"] is None else
                      "machine_changed" if current["machine_key"] != key else
                      "layout_expired" if current["valid_to"] and current["valid_to"] < day.isoformat() else
                      "observation_gap" if (day - previous_day).days > gap_days else
                      "placement_changed" if position is not None and current["position"] != position else None)
            if reason:
                current = {"start_observed": day.isoformat(), "last_observed": day.isoformat(),
                           "machine_key": key, "machine_name": observations[0]["machine_name"] if key else "配置情報が矛盾",
                           "position": position, "reason": reason, "previous_observed": previous_day.isoformat() if previous_day else None,
                           "source_urls": [], "layout_id": None, "valid_to": None}
                periods.append(current)
            current["last_observed"] = day.isoformat()
            for r in observations:
                if r.get("source_url") and r["source_url"] not in current["source_urls"]:
                    current["source_urls"].append(r["source_url"])
                if r.get("layout_id"):
                    current["layout_id"], current["valid_to"] = r["layout_id"], r.get("valid_to")
        stale = (cutoff - date.fromisoformat(current["last_observed"])).days > gap_days
        expired = bool(current["valid_to"] and current["valid_to"] < cutoff.isoformat())
        usable = current["machine_key"] is not None and not stale and not expired
        selected = [r for r in originals_by_seat[(hall, number)]
                    if r["report_date"] >= current["start_observed"] and r["machine_key"] == current["machine_key"]] if usable else []
        kept.extend(selected)
        segment_id = hashlib.sha256(json.dumps([hall, number, current["start_observed"], current["machine_key"], current["position"]], ensure_ascii=False).encode()).hexdigest()[:20]
        seats.append({"hall_name": hall, "seat_number": number, "machine_name": current["machine_name"],
                      "segment_id": segment_id, "training_from": current["start_observed"],
                      "last_observed": current["last_observed"], "position": current["position"],
                      "usable": usable, "status": "情報矛盾" if current["machine_key"] is None else "配置期限切れ" if expired else "現状未確認" if stale else "観測期間を分離済み",
                      "periods": periods, "changes": len(periods) - 1, "training_records": len(selected)})
    return {"rows": kept, "seats": seats, "cutoff_date": cutoff.isoformat(),
            "excluded_records": len(originals) - len(kept), "gap_days": gap_days,
            "notice": f"日付は公開実績日または確認済みマップの適用開始日です。正確な入替日・実物の同一台は確定できません。欠測を撤去と解釈せず、{gap_days}日超の観測空白は別期間に分けます。台番号だけで隣接・角台を断定しません。"}
