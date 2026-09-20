"""v3.42: read-only, question-scoped hall descriptions, not new predictions."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
import math
from pathlib import Path
import re
import sqlite3
from statistics import mean
import unicodedata

from api.ai_evidence import evidence, freeze_evidence, metric
from hall.machine_scope import normalize_machine_key
from hall.names import canonical_hall_name
from hall.seat_history import build_seat_history, known_layouts, observations_from_layouts

TOPICS = {"overview": "店舗の要点", "weekday": "曜日の傾向", "digit": "日付末尾の傾向",
          "machine": "強く扱う機種", "event": "登録イベント", "seat": "台番号の履歴",
          "layout": "島・配置の傾向", "comparison": "他店舗との比較"}
MIN_DAYS = 5  # A display rule for small descriptive samples, never a confidence probability.
WEEKDAYS = "月火水木金土日"
GLOBAL_GAPS = ["取得できた公開データの集計です。未公開台・未公開日と設置全台の偏りは未補正",
               "過去の傾向であり、確定設定・将来の勝率・着席推薦ではありません",
               "元データの改訂履歴が不十分なため、当時の情報だけによる事前予測とは扱いません"]


def resolve_topics(question: str, topic: str = "auto") -> list[str]:
    if topic != "auto":
        return [topic] if topic in TOPICS else ["overview"]
    q = unicodedata.normalize("NFKC", question)
    found = []
    for key, words in (
        ("comparison", ("他店", "他の店", "別の店", "優良店", "回収店")),
        ("layout", ("島", "配置", "並び", "角台", "ホールマップ")),
        ("seat", ("台番", "何番", "番台")),
        ("event", ("イベント", "特定日", "旧イベ", "取材")),
        ("machine", ("機種", "何を打", "強く扱", "モンキー", "北斗", "ジャグ")),
        ("weekday", ("曜日", "何曜", "月曜", "火曜", "水曜", "木曜", "金曜", "土曜", "日曜", "週末")),
        ("digit", ("末尾", "のつく日", "の付く日")),
    ):
        if any(word in q for word in words):
            found.append(key)
    if not found and any(word in q for word in ("いつ", "行く日", "熱い日")):
        return ["weekday", "digit", "event"]
    return found or ["overview"]


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _load(conn, table, hall, start, end):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    names = [r[0] for r in conn.execute(f"SELECT DISTINCT hall_name FROM {table}")
             if canonical_hall_name(r[0]) == hall]
    if not names:
        return []
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM {table} WHERE hall_name IN ({','.join('?' for _ in names)}) AND report_date BETWEEN ? AND ?",
        (*names, start, end))]


def _source_rows(rows):
    unique = {}
    for row in rows:
        source = {"url": row.get("source_url"), "retrieved_at": row.get("scraped_at"),
                  "label": row.get("source") or "保存済み公開実績"}
        unique[(source["url"], source["retrieved_at"], source["label"])] = source
    # Include all missing metadata before the excerpt, so omission isn't hidden.
    sources = sorted(unique.values(), key=lambda s: (s["url"] is not None and s["retrieved_at"] is not None,
                                                    str(s["retrieved_at"]), str(s["url"])))
    return sources[:10], ["出典はこの根拠に使った記録の10件までを抜粋"] if len(sources) > 10 else []


def _record(hall, target, rows, *, dimension, label, values, machine=None, seat=None,
            comparison_scope="店舗内", interpretation="", missing=()):
    sources, source_gaps = _source_rows(rows)
    dates = sorted({r["report_date"] for r in rows})
    item = evidence(hall=hall, machine=machine, seat=seat, target=target, metrics=values,
        start=dates[0] if dates else None, end=dates[-1] if dates else None, sources=sources,
        kind="hall_descriptive", source_label="店舗特化・公開実績集計",
        missing=[*missing, *source_gaps])
    item.update(dimension=dimension, subject_label=label, comparison_scope=comparison_scope,
                interpretation=interpretation)
    return item


def _daily_points(machine_rows, seat_rows, gaps):
    groups = defaultdict(list)
    fallback = defaultdict(list)
    for row in machine_rows:
        key = normalize_machine_key(row.get("machine_name") or "")
        if key and not row["machine_name"].startswith("_") and _finite(row.get("avg_diff_coins")):
            groups[(row["report_date"], key)].append(row)
    for row in seat_rows:
        key = normalize_machine_key(row.get("machine_name") or "")
        number = row.get("seat_number")
        if key and not row["machine_name"].startswith("_") and isinstance(number, int) and number > 0 and _finite(row.get("diff_coins")):
            fallback[(row["report_date"], key)].append(row)
    points = []
    for key in sorted(groups.keys() | fallback.keys()):
        if key in groups:
            rows = groups[key]
            if len({r["avg_diff_coins"] for r in rows}) > 1:
                gaps.append("同日・同機種の公表値が矛盾する区分を除外")
                continue
            value = rows[0]["avg_diff_coins"]
        else:
            rows = fallback[key]
            by_number = defaultdict(list)
            for row in rows:
                by_number[row["seat_number"]].append(row["diff_coins"])
            if any(len(set(values)) > 1 for values in by_number.values()):
                gaps.append("同日・同台番号の差枚が矛盾する区分を除外")
                continue
            value = mean(values[0] for values in by_number.values())
            gaps.append("機種集計のない日・機種は公開台だけの平均で補完。全設置台平均とは限りません")
        points.append({"date": key[0], "key": key[1], "machine": rows[0]["machine_name"],
                       "value": value, "rows": rows})
    return points


def _description(n, difference=None, compared=0):
    if n < MIN_DAYS or difference is not None and compared < MIN_DAYS:
        return "少数日・参考止まり。強い傾向とはまだ判定しません"
    if difference is None:
        return "記述統計。強さを比較できる相手側のデータがありません"
    if difference > 0:
        return "取得範囲では比較対象より平均差枚が高め。継続性・設定投入は未確認"
    if difference < 0:
        return "取得範囲では比較対象より平均差枚が低め。回収営業とは断定できません"
    return "取得範囲では比較対象との平均差枚の差がありません"


def _temporal_facts(hall, target, points, topics, question):
    daily = defaultdict(list)
    for point in points:
        daily[point["date"]].append(point)
    values = {day: mean(p["value"] for p in ps) for day, ps in daily.items()}
    facts = []
    q = unicodedata.normalize("NFKC", question)
    for dimension in ("weekday", "digit"):
        if dimension not in topics:
            continue
        groups = defaultdict(list)
        for day in daily:
            d = date.fromisoformat(day)
            groups[d.weekday() if dimension == "weekday" else d.day % 10].append(day)
        requested_weekdays = {i for i, w in enumerate(WEEKDAYS) if w + "曜" in q}
        digits = re.findall(r"(?:末尾\s*([0-9])|([0-9])\s*の(?:つく|付く)日)", q)
        requested_digits = {int(a or b) for a, b in digits}
        for key, days in sorted(groups.items()):
            if dimension == "weekday" and requested_weekdays and key not in requested_weekdays:
                continue
            if dimension == "digit" and requested_digits and key not in requested_digits:
                continue
            other = [value for day, value in values.items() if day not in days]
            avg = mean(values[day] for day in days)
            diff = avg - mean(other) if other else None
            # The comparison needs the other days' provenance as well.
            rows = [r for ps in daily.values() for p in ps for r in p["rows"]]
            label = WEEKDAYS[key] + "曜日" if dimension == "weekday" else f"日付末尾{key}（イベント認定ではありません）"
            facts.append(_record(hall, target, rows, dimension=dimension, label=label,
                values=[metric("該当日数", len(days), "日"), metric("日別・機種均等平均差枚", round(avg, 1), "枚"),
                        metric("その他の日数", len(other), "日"),
                        metric("その他の日との差", round(diff, 1) if diff is not None else None, "枚")],
                comparison_scope="同一店舗・該当日とその他の日（公開機種構成の差は未補正）",
                interpretation=_description(len(days), diff, len(other)),
                missing=["公表機種の構成やイベントの重なりを補正していないため、曜日・日付が原因とは断定できません"]))
    return sorted(facts, key=lambda f: (
        not f["interpretation"].startswith("少数"),
        next((m["value"] for m in f["metrics"] if m["label"] == "その他の日との差"), -1e9)), reverse=True)


def _machine_facts(hall, target, points, machine_filter):
    grouped, by_date = defaultdict(list), defaultdict(list)
    for point in points:
        grouped[point["key"]].append(point)
        by_date[point["date"]].append(point)
    facts = []
    for key, entries in sorted(grouped.items()):
        if machine_filter and key != normalize_machine_key(machine_filter):
            continue
        pairs = [(p["value"], mean(o["value"] for o in by_date[p["date"]] if o["key"] != key))
                 for p in entries if any(o["key"] != key for o in by_date[p["date"]])]
        diff = mean(a - b for a, b in pairs) if pairs else None
        n = len(entries)
        source_points = [p for entry in entries for p in by_date[entry["date"]]]
        facts.append(_record(hall, target, [r for p in source_points for r in p["rows"]],
            dimension="machine", label="同じ日の他機種と比較", machine=entries[-1]["machine"],
            values=[metric("記録日数", n, "日"), metric("日別平均差枚", round(mean(p["value"] for p in entries), 1), "枚"),
                    metric("共通比較日数", len(pairs), "日"), metric("同日の他機種との差", round(diff, 1) if diff is not None else None, "枚")],
            comparison_scope="同一店舗・同じ日に公表された他機種の均等平均。機種特性・稼働差は未補正",
            interpretation=_description(n, diff, len(pairs)), missing=["設定投入率や機械割の差を示すものではありません"]))
    return sorted(facts, key=lambda f: (
        not f["interpretation"].startswith("少数") and not f["interpretation"].startswith("記述"),
        next((m["value"] for m in f["metrics"] if m["label"] == "同日の他機種との差"), -1e9)), reverse=True)


def _active_layouts(layouts, target, cutoff, gaps):
    floors = defaultdict(list)
    for layout in layouts:
        known = str(layout.get("known_at") or "")[:10]
        if not known or known > cutoff:
            gaps.append("取得・記録時点が不明または対象期間後の配置は使っていません")
            continue
        floors[layout.get("floor_name", "")].append(layout)
    active = []
    for items in floors.values():
        latest = max(item["valid_from"] for item in items)
        candidates = [item for item in items if item["valid_from"] == latest]
        if len(candidates) != 1:
            gaps.append("配置が同じ適用日で重複しているフロアは除外")
            continue
        item = candidates[0]
        if item.get("verification_status") != "確認済み" or item.get("source_kind") not in {"official", "pworld", "manual", "uploaded"}:
            gaps.append("最新の配置が未確認・自動生成のフロアは除外")
            continue
        if item.get("valid_to") and item["valid_to"] < target:
            gaps.append("対象日に有効期限が切れる配置は除外")
            continue
        active.append(item)
    return active


def _seat_layout_facts(hall, target, cutoff, seat_rows, layouts, topics, machine_filter, question, gaps):
    active = _active_layouts(layouts, target, cutoff, gaps)
    history = build_seat_history(seat_rows, observations_from_layouts(active), cutoff=date.fromisoformat(cutoff))
    kept = defaultdict(list)
    for row in history["rows"]:
        if _finite(row.get("diff_coins")):
            kept[row["seat_number"]].append(row)
    facts = []
    requested = re.search(r"(?:台番号\s*([0-9]{1,5})|([0-9]{1,5})\s*番台)", unicodedata.normalize("NFKC", question))
    number_filter = int(requested.group(1) or requested.group(2)) if requested else None
    for identity in history["seats"]:
        if "seat" not in topics or not identity["usable"]:
            continue
        if number_filter and identity["seat_number"] != number_filter:
            continue
        if machine_filter and normalize_machine_key(identity["machine_name"]) != normalize_machine_key(machine_filter):
            continue
        rows = kept[identity["seat_number"]]
        if not rows:
            continue
        by_day = defaultdict(list)
        for row in rows:
            by_day[row["report_date"]].append(row)
        values = [r[0]["diff_coins"] for r in by_day.values() if len({x["diff_coins"] for x in r}) == 1]
        if len(values) != len(by_day):
            gaps.append("同日・同台の差枚が矛盾する台番号は除外")
            continue
        facts.append(_record(hall, target, rows, dimension="seat", label="同一観測期間の実績（着席推薦なし）",
            machine=identity["machine_name"], seat=identity["seat_number"],
            values=[metric("記録日数", len(values), "日"), metric("平均差枚", round(mean(values), 1), "枚/台日")],
            interpretation="観測期間の入替・空白を分離した台別履歴です。行く日の配置・空席は現地確認が必要",
            missing=["台番号だけで隣接・角台・現在の着席候補とは判定しません"] + (["台別実績が5日未満"] if len(values) < MIN_DAYS else [])))
    if "layout" in topics:
        for layout in active:
            groups = defaultdict(list)
            for seat in layout["seats"]:
                if seat.get("island_name"):
                    groups[seat["island_name"]].append(seat)
            for name, members in groups.items():
                if len(members) < 2 or len({s["seat_number"] for s in members}) != len(members):
                    continue
                if machine_filter and any(normalize_machine_key(s["machine_name"]) != normalize_machine_key(machine_filter) for s in members):
                    continue
                groups_by_day = []
                for member in members:
                    rows = [r for r in kept[member["seat_number"]]
                            if normalize_machine_key(r["machine_name"]) == normalize_machine_key(member["machine_name"])
                            and r["report_date"] >= max(layout["valid_from"], str(layout["known_at"])[:10])]
                    day_rows = defaultdict(list)
                    for row in rows:
                        day_rows[row["report_date"]].append(row)
                    groups_by_day.append({day: rs[0] for day, rs in day_rows.items() if len({r["diff_coins"] for r in rs}) == 1})
                common = set.intersection(*(set(d) for d in groups_by_day)) if groups_by_day else set()
                rows = [d[day] for day in sorted(common) for d in groups_by_day]
                if not common:
                    gaps.append("確認済み配置でも、島の全台実績が同日に揃わない区分があります")
                    continue
                fact = _record(hall, target, rows, dimension="layout", label=f"{layout['floor_name']} / {name}（登録済みの島）",
                    values=[metric("島の記録台数", len(members), "台"), metric("全台共通日数", len(common), "日"),
                            metric("島の全台共通日・平均差枚", round(mean(r["diff_coins"] for r in rows), 1), "枚/台日")],
                    comparison_scope="確認済みの島内・同じ日に全台の結果が揃う日だけ。隣接は推測しません",
                    interpretation="公開された島の実績です。並び投入・全台高設定の証明ではありません",
                    missing=["現在の空席・設定は確認していません"] + (["共通日数が5日未満"] if len(common) < MIN_DAYS else []))
                fact["sources"].append({"url": layout.get("source_url"), "retrieved_at": None,
                    "label": "確認済み配置（記録時刻 " + str(layout["known_at"]) + "、元取得時刻は未記録）"})
                fact["missing_information"].append("配置の元取得時刻は未記録。配置の記録時刻とは区別します")
                facts.append(fact)
        if not active:
            gaps.append("対象期間に使える確認済み配置がありません。台番号から島や並びを作りません")
    gaps.append("台番号は同じ観測期間だけを集計し、30日超の空白・確認された入替を分離しています")
    return facts


def build_hall_snapshot(db_path, *, hall_name, target_date, days=90, question="", topic="auto", machine_name="", event_facts=()):
    hall = canonical_hall_name(hall_name)
    target = date.fromisoformat(target_date)
    cutoff = min(target - timedelta(days=1), date.today())
    start = cutoff - timedelta(days=days - 1)
    topics = resolve_topics(question, topic)
    effective = ["weekday", "digit", "machine"] if topics == ["overview"] else topics
    gaps, facts = list(GLOBAL_GAPS), []
    if cutoff == date.today():
        gaps.append("本日分の公開値が含まれる場合は途中経過の可能性があります")
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            machine_rows = _load(conn, "hall_day_machine", hall, start.isoformat(), cutoff.isoformat())
            seat_rows = _load(conn, "hall_day_seat", hall, start.isoformat(), cutoff.isoformat())
            points = _daily_points(machine_rows, seat_rows, gaps)
            matching_names = {p["machine"] for p in points if normalize_machine_key(p["machine"]) in normalize_machine_key(question)}
            requested_family = next((word for word in ("モンキーターン", "モンキー", "北斗", "ジャグラー", "からくり", "かぐや", "ゴッドイーター") if word in question), None)
            family_keys = {p["key"] for p in points if requested_family and normalize_machine_key(requested_family) in p["key"]}
            if not machine_name and len(matching_names) == 1:
                machine_name = matching_names.pop()
            if not machine_name and len(family_keys) == 1:
                machine_name = next(p["machine"] for p in points if p["key"] in family_keys)
            temporal_points = [p for p in points if not machine_name or p["key"] == normalize_machine_key(machine_name)]
            if requested_family and not machine_name:
                temporal_points = []
                gaps.append("機種系列の曜日・台番号・配置は、機種名を限定して確認してください")
            temporal = _temporal_facts(hall, target_date, temporal_points, effective, question)
            if machine_name:
                for fact in temporal:
                    fact["machine_name"] = machine_name
            facts += temporal
            if "machine" in effective:
                machine_facts = _machine_facts(hall, target_date, points, machine_name)
                if requested_family and not machine_name:
                    machine_facts = [f for f in machine_facts if normalize_machine_key(f["machine_name"]) in family_keys]
                facts += machine_facts
            if "seat" in effective or "layout" in effective:
                layouts = [m for m in known_layouts(conn, cutoff) if canonical_hall_name(m["hall_name"]) == hall]
                if not requested_family or machine_name:
                    facts += _seat_layout_facts(hall, target_date, cutoff.isoformat(), seat_rows, layouts,
                                               effective, machine_name, question, gaps)
    except (sqlite3.Error, OSError):
        gaps.append("店舗の公開実績を読み取れませんでした")
    if "event" in effective:
        for fact in event_facts:
            if canonical_hall_name(fact.get("hall_name")) != hall:
                continue
            if (fact.get("period", {}).get("end") or "9999") > cutoff.isoformat():
                gaps.append("対象日以後の実績を含むイベント根拠を除外")
                continue
            if machine_name and fact.get("machine_name") and normalize_machine_key(fact["machine_name"]) != normalize_machine_key(machine_name):
                continue
            fact = dict(fact)
            fact.update(dimension="event", comparison_scope="登録済みイベントの既存判定", subject_label="登録イベントの根拠")
            facts.append(fact)
    if "comparison" in effective:
        gaps.append("他店舗との同一日・同一機種・同一公開範囲の比較根拠がありません。店内比較で優良店・回収店を断定しません")
    for key in effective:
        if not any(f.get("dimension") == key for f in facts):
            gaps.append(TOPICS[key] + "：質問条件に合う根拠がありません")
    # Overview interleaves dimensions; no large category crowds out all others.
    if topics == ["overview"]:
        grouped = [[f for f in facts if f["dimension"] == key] for key in effective]
        facts = [items[i] for i in range(max((len(x) for x in grouped), default=0)) for items in grouped if i < len(items)]
    if len(facts) > 30:
        gaps.append("根拠は各区分の表示順で30件まで。差枚順の着席ランキングではありません")
    snapshot = freeze_evidence(facts[:30], target_date=target_date, scope=hall, missing=list(dict.fromkeys(gaps)),
        constraints={"hall_name": hall, "machine_name": machine_name or None,
                     "period_end": cutoff.isoformat(), "dimensions": effective,
                     "allow_upcoming_events": "event" in effective})
    snapshot.update(topics=topics, topic_labels=[TOPICS[key] for key in topics], machine_filter=machine_name,
                    analysis_window={"start": start.isoformat(), "end": cutoff.isoformat()}, comparison_scope="店舗内の公開実績")
    return snapshot
