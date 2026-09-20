"""v3.29 research: explicitly recorded groups and consecutive-day observations.

No adjacency is inferred from seat numbers/coordinates. Replays reconstruct the
known map and seat tenure at each cutoff. This module never changes live ranks.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import mean

from hall.machine_scope import is_smartslot_machine, normalize_machine_key
from hall.names import canonical_hall_name
from hall.prediction_quality import audit_rows
from hall.seat_history import build_seat_history, observations_from_layouts

POLICY = "verified-layout-peer-groups-v1"
WINDOW = 56
MIN_PAIRS = 10
MIN_VALIDATION = 20
MAX_REPLAY_DAYS = 60


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _key(row):
    return normalize_machine_key(row.get("machine_name", ""))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


def _trusted(layouts, hall, cutoff):
    available = [m for m in layouts if canonical_hall_name(m.get("hall_name")) == hall
            and _day(m.get("known_at")) is not None and _day(m["known_at"]) <= cutoff
            and _day(m.get("valid_from")) is not None and _day(m["valid_from"]) <= cutoff]
    # A newer unverified or derived map must invalidate an older verified map.
    # Keep it in this list as an obstruction; only verified maps become groups.
    return available


def _groups(layouts, identities, target):
    floors = defaultdict(list)
    for m in layouts:
        floors[m.get("floor_name", "")].append(m)
    active, issues = [], Counter()
    for maps in floors.values():
        latest = max(m["valid_from"] for m in maps)
        newest = [m for m in maps if m["valid_from"] == latest]
        if len(newest) != 1:
            issues["同じフロアの配置が重複"] += 1
            continue
        m = newest[0]
        if m.get("verification_status") != "確認済み" or m.get("source_kind") not in {"official", "pworld", "manual", "uploaded"}:
            issues["最新の配置が未確認・自動生成"] += 1
            continue
        if m.get("valid_to") and (_day(m["valid_to"]) is None or _day(m["valid_to"]) < target):
            issues["配置の有効期限切れ"] += 1
            continue  # Do not resurrect an older map after the newest expires.
        active.append(m)
    numbers = Counter(s.get("seat_number") for m in active for s in m.get("seats", []))
    result = []
    for m in active:
        islands, rows = defaultdict(list), defaultdict(list)
        for s in m.get("seats", []):
            island = str(s.get("island_name") or "").strip()
            if island:
                islands[island].append(s)
                if s.get("row_name") and s.get("row_order") is not None:
                    rows[(island, s["row_name"])].append(s)
        proposals = [("island", name, [s for s in seats if is_smartslot_machine(s.get("machine_name"))])
                     for name, seats in sorted(islands.items())]
        for (island, row), seats in sorted(rows.items()):
            positions = [s["row_order"] for s in seats]
            if any(not isinstance(p, int) or isinstance(p, bool) or p < 1 for p in positions) or len(set(positions)) != len(positions):
                issues["列内の順番が不正・重複"] += 1
                continue
            seats = sorted(seats, key=lambda s: s["row_order"])
            for i in range(len(seats)-2):
                group = seats[i:i+3]
                if group[-1]["row_order"] - group[0]["row_order"] != 2:
                    continue
                if all(is_smartslot_machine(s.get("machine_name")) for s in group):
                    proposals.append(("row3", f"{island} / {row} / {group[0]['row_order']}〜{group[-1]['row_order']}", group))
        for kind, name, seats in proposals:
            if len(seats) < 2:
                continue
            blocked = []
            members = []
            for s in seats:
                number = s.get("seat_number")
                identity = identities.get(number)
                if numbers[number] != 1:
                    blocked.append("複数の配置に同じ台番号があります")
                if not identity or not identity["usable"] or _key(identity) != _key(s):
                    blocked.append("台の機種・観測期間が配置と一致しません")
                members.append({"seat_number": number, "machine_name": s.get("machine_name"),
                                "segment_id": identity["segment_id"] if identity else None,
                                "position": [s.get(k) for k in ("x", "y", "rotation", "row_name", "row_order")]})
            if kind == "island":
                members.sort(key=lambda s: s["seat_number"])
            signature = [m.get("id"), m["valid_from"], m["known_at"], m.get("floor_name"), kind, name, members]
            result.append({"group_id": _digest(signature), "kind": kind, "name": name, "floor_name": m.get("floor_name"),
                           "members": members, "seat_numbers": [s["seat_number"] for s in members],
                           "layout_valid_from": m["valid_from"], "layout_known_at": m["known_at"],
                           "source_url": m.get("source_url", ""), "blockers": sorted(set(blocked))})
    if not layouts:
        issues["入力期限までに確認・記録されたマップがありません"] += 1
    elif not result:
        issues["島名または列名・列内順が未登録、もしくは対象台が不足"] += 1
    return result, dict(issues)


def _candidate(points, target, cutoff):
    selected = [p for p in points if target-timedelta(days=WINDOW) <= _day(p["date"]) <= cutoff]
    blockers = []
    if len(selected) < MIN_PAIRS:
        blockers.append("同じ配置・観測期間で全台と他の同機種2台以上を比較できた日が10日未満")
    if not selected or (cutoff-_day(selected[-1]["date"])).days > 14:
        blockers.append("直近14日以内の比較実績なし")
    candidate = None
    if not blockers:
        weekday = [p for p in selected if _day(p["date"]).weekday() == target.weekday()]
        controls = weekday if len(weekday) >= 3 else selected
        peer = mean(p["peer_coins"] for p in controls)
        lift = mean(p["actual_coins"]-p["peer_coins"] for p in selected)
        adjustment = max(-500, min(500, lift*len(selected)/(len(selected)+20)))
        candidate = {"predicted_coins": round(peer+adjustment, 1),
                     "baseline_coins": round(mean(p["actual_coins"] for p in selected), 1),
                     "adjustment_coins": round(adjustment, 1), "training_days": len(selected),
                     "training_from": selected[0]["date"], "training_to": selected[-1]["date"],
                     "input_cutoff_date": cutoff.isoformat()}
    return selected, candidate, blockers


def _validation(trials):
    answered = [t for t in trials if t["actual_coins"] is not None]
    return {"days": len(answered), "unresolved_days": len(trials)-len(answered),
            "candidate_mae_coins": round(mean(abs(t["predicted_coins"]-t["actual_coins"]) for t in answered), 1) if answered else None,
            "baseline_mae_coins": round(mean(abs(t["baseline_coins"]-t["actual_coins"]) for t in answered), 1) if answered else None,
            "status": "比較20日以上・未採用" if len(answered) >= MIN_VALIDATION else "検証不足", "trials": trials}


def analyze_hall_placement(rows, *, hall_name, target, as_of, layouts_by_cutoff=None, setting_records=()):
    hall = canonical_hall_name(hall_name)
    cutoff = min(as_of-timedelta(days=1), target-timedelta(days=2))
    raw = [dict(r) for r in rows if canonical_hall_name(dict(r).get("hall_name")) == hall
           and _day(dict(r).get("report_date")) is not None and _day(dict(r)["report_date"]) <= cutoff]
    clean, audit = audit_rows(raw, cutoff=cutoff, scope="seat")
    maps = layouts_by_cutoff or {}
    by_day = defaultdict(dict)
    for r in clean:
        if is_smartslot_machine(r["machine_name"]) and r.get("games") is not None and r["games"] >= 1000:
            by_day[r["report_date"]][r["seat_number"]] = r
    snapshots = {}

    def snapshot(day, visit=None):
        visit = visit or day
        cache_key = (day, visit)
        if cache_key not in snapshots:
            known = _trusted(maps.get(day.isoformat(), []), hall, day)
            history = build_seat_history(raw, observations_from_layouts(known), cutoff=day)
            identities = {s["seat_number"]: s for s in history["seats"]}
            groups, issues = _groups(known, identities, visit)
            data = {n: r for n, r in by_day[day.isoformat()].items()
                    if n in identities and identities[n]["usable"] and _key(identities[n]) == _key(r)}
            snapshots[cache_key] = {"groups": groups, "issues": issues, "identities": identities, "data": data}
        return snapshots[cache_key]

    current = snapshot(cutoff, target)
    days = sorted({_day(r["report_date"]) for r in raw})
    points, all_groups = defaultdict(list), {}
    # Layouts and group membership are selected before reading each day's result.
    has_maps = any(_trusted(items, hall, _day(day)) for day, items in maps.items() if _day(day) and _day(day) <= cutoff)
    if has_maps:
        for day in days:
            snap = snapshot(day)
            data = snap["data"]
            for group in snap["groups"]:
                all_groups[group["group_id"]] = group
                if group["blockers"] or any(n not in data for n in group["seat_numbers"]):
                    continue
                peers = []
                for number in group["seat_numbers"]:
                    controls = [r["diff_coins"] for n, r in data.items() if n not in group["seat_numbers"] and _key(r) == _key(data[number])]
                    if len(controls) < 2:
                        break
                    peers.append(mean(controls))
                if len(peers) != len(group["seat_numbers"]):
                    continue
                actual = [data[n]["diff_coins"] for n in group["seat_numbers"]]
                sources = sorted({u for r in data.values() for u in r.get("source_urls", [])})
                points[group["group_id"]].append({"date": day.isoformat(), "actual_coins": mean(actual),
                    "peer_coins": mean(peers), "all_positive": all(v > 0 for v in actual), "source_urls": sources})

    trials = defaultdict(list)
    if has_maps:
        for day in days[-MAX_REPLAY_DAYS:]:
            past_cutoff = day-timedelta(days=2)
            past, outcome = snapshot(past_cutoff, day), snapshot(day)
            outcome_ids = {g["group_id"] for g in outcome["groups"] if not g["blockers"]}
            for group in past["groups"]:
                if group["blockers"]:
                    continue
                _, candidate, _ = _candidate(points[group["group_id"]], day, past_cutoff)
                if candidate is None:
                    continue
                all_groups[group["group_id"]] = group
                missing = [n for n in group["seat_numbers"] if n not in outcome["data"]]
                reason = "配置・入替・観測期間の不一致" if group["group_id"] not in outcome_ids else "全台の実績・稼働が揃わない" if missing else None
                trials[group["group_id"]].append({**candidate, "date": day.isoformat(),
                    "seat_numbers": group["seat_numbers"], "unresolved_reason": reason,
                    "actual_coins": mean(outcome["data"][n]["diff_coins"] for n in group["seat_numbers"]) if reason is None else None})

    profiles = []
    current_ids = {g["group_id"] for g in current["groups"]}
    for group in current["groups"]:
        selected, candidate, blockers = _candidate(points[group["group_id"]], target, cutoff)
        profiles.append({**group, "candidate": None if group["blockers"] else candidate,
            "blockers": group["blockers"]+blockers, "paired_days": len(selected),
            "relative_diff_coins": round(mean(p["actual_coins"]-p["peer_coins"] for p in selected), 1) if selected else None,
            "all_positive_days": sum(p["all_positive"] for p in selected),
            "all_positive_rate_pct": round(sum(p["all_positive"] for p in selected)/len(selected)*100, 1) if selected else None,
            "daily_points": selected, "validation": _validation(trials[group["group_id"]]),
            "model_influence_eligible": False})

    # Descriptive consecutive-calendar-day transitions; never used as tomorrow's
    # feature when the previous day's result is not yet known.
    transitions, excluded = defaultdict(list), Counter()
    for day in days:
        if not target-timedelta(days=WINDOW) <= day <= cutoff:
            continue
        now, before = snapshot(day), snapshot(day-timedelta(days=1))
        for n, r in now["data"].items():
            previous = before["data"].get(n)
            if not previous:
                excluded["前日の実績・稼働がない"] += 1
                continue
            if before["identities"][n]["segment_id"] != now["identities"][n]["segment_id"]:
                excluded["入替・配置変更・観測期間の不一致"] += 1
                continue
            state = "negative" if previous["diff_coins"] < 0 else "positive" if previous["diff_coins"] > 0 else "zero"
            transitions[(_key(r), state)].append({"date": day.isoformat(), "seat_number": n,
                "machine_name": r["machine_name"], "previous_coins": previous["diff_coins"], "actual_coins": r["diff_coins"],
                "source_urls": sorted(set(previous.get("source_urls", [])+r.get("source_urls", [])))})
    transition_profiles = []
    for (machine, state), pairs in sorted(transitions.items()):
        count_days = len({p["date"] for p in pairs})
        transition_profiles.append({"machine_key": machine, "machine_name": pairs[0]["machine_name"], "previous_state": state,
            "pairs": len(pairs), "days": count_days, "positive_rate_pct": round(sum(p["actual_coins"]>0 for p in pairs)/len(pairs)*100,1),
            "average_next_coins": round(mean(p["actual_coins"] for p in pairs),1),
            "status": "記述統計・未採用" if count_days >= MIN_VALIDATION else "検証不足", "observations": pairs})

    exact = defaultdict(set)
    for r in setting_records:
        day, known = _day(r.get("date")), _day(r.get("known_at"))
        if canonical_hall_name(r.get("hall_name")) != hall or not day or not known or day > cutoff or known > cutoff:
            continue
        if r.get("setting_evidence_level") != "confirmed_exact" or not is_smartslot_machine(r.get("machine_name")):
            continue
        value, number = r.get("confirmed_setting"), r.get("seat_number")
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 6 and isinstance(number, int) and number > 0:
            exact[(day, number, _key(r))].add(value)
    evidence_pairs = []
    for (day, n, machine), values in sorted(exact.items()):
        previous = exact.get((day-timedelta(days=1), n, machine), set())
        if len(values) != 1 or len(previous) != 1 or day < target-timedelta(days=WINDOW):
            continue
        now, before = snapshot(day)["identities"].get(n), snapshot(day-timedelta(days=1))["identities"].get(n)
        if not now or not before or not now["usable"] or not before["usable"] or _key(now) != machine or now["segment_id"] != before["segment_id"]:
            continue
        current_value, previous_value = next(iter(values)), next(iter(previous))
        evidence_pairs.append({"date": day.isoformat(), "seat_number": n, "machine_key": machine,
            "previous_setting": previous_value, "current_setting": current_value,
            "change": "higher" if current_value>previous_value else "lower" if current_value<previous_value else "same_value"})
    evidence_counts = Counter(p["change"] for p in evidence_pairs)
    return {"hall_name": hall, "target_date": target.isoformat(), "input_cutoff_date": cutoff.isoformat(), "policy": POLICY,
        "profiles": profiles, "layout_issues": current["issues"], "input_audit": audit,
        "candidate_groups": sum(p["candidate"] is not None for p in profiles),
        "retired_group_validations": [{**all_groups[k], "validation": _validation(v)} for k,v in sorted(trials.items()) if k not in current_ids],
        "transitions": transition_profiles, "transition_exclusions": dict(excluded),
        "setting_evidence": {"pairs": len(evidence_pairs), "days": len({p["date"] for p in evidence_pairs}),
            "higher": evidence_counts["higher"], "lower": evidence_counts["lower"], "same_value": evidence_counts["same_value"],
            "conflicts": sum(len(v)>1 for v in exact.values()), "observations": evidence_pairs,
            "notice": "両日の設定値が確定した本人記録だけの比較。同じ値でも据え置きと同設定への変更は区別できません。"},
        "model_influence_eligible": False,
        "notice": "配置上の並びは確認済みの列名・列内順で連続する3台。島は登録されたスマスロの集合です。各台の同機種を組の外で2台以上比較し、全台1000G以上・現在の配置と観測期間・前56日で10日以上、鮮度14日以内が必要です。予測は対象日の2日前までの情報で固定し、組平均と同じ日で誤差を比較。重複する3台組や同日の実績は独立標本ではありません。差枚の翌日傾向は設定変更率・本人勝率ではなく、現在の着席判定への反映は3.30の採用審査後です。"}
