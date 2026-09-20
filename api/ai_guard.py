"""v3.46 deterministic evidence checks; no calls, learned judgments or DB writes."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import math
import re
import unicodedata
from urllib.parse import urlsplit
from hall.machine_scope import normalize_machine_key
from hall.names import canonical_hall_name

VERSION = "evidence-guard-1.0"
REASONS = {
    "integrity": "根拠の識別情報と内容が一致しません",
    "scope": "選択した店舗・機種・台番号・イベントと根拠が一致しません",
    "period": "対象日または集計期間が質問の範囲と一致しません",
    "metric": "数値・単位・指標の組合せを確認できません",
    "source": "出典URLの形式を確認できません",
    "instruction": "資料中に回答を誘導する指示が含まれています",
    "kind": "根拠の種類を確認できません",
    "unsupported_claim": "確定設定・勝率などの未検証の断定が資料に含まれています",
    "setting": "公開差枚から確定設定・高設定の的中率は回答できません",
    "profit": "本人の将来の勝率・収益を算出する根拠がありません",
    "layout": "島の集計や台番号だけでは角台・隣接を確認できません",
    "identity": "個別の台番号を説明する履歴がありません",
    "event": "質問したイベントの登録根拠がありません",
    "source_missing": "元の出典URLまたは取得時刻が未記録です",
    "knowledge": "専用資料の版・引用元・適用条件を確認できません",
}

# These are the metrics actually emitted by the server's statistical builders.
METRICS = {
    "平均差枚": {"枚", "枚/台日"}, "公表平均差枚": {"枚"},
    "記録日数": {"日"}, "記録件数": {"台日"}, "公開台のプラス割合": {"%"},
    "該当日数": {"日"}, "日別・機種均等平均差枚": {"枚"}, "その他の日数": {"日"},
    "その他の日との差": {"枚"}, "日別平均差枚": {"枚"}, "共通比較日数": {"日"},
    "同日の他機種との差": {"枚"}, "島の記録台数": {"台"}, "全台共通日数": {"日"},
    "島の全台共通日・平均差枚": {"枚/台日"}, "基準点": {"点"}, "品質点": {"点"},
    "対応実績日数": {"日"}, "通常日比": {"枚"}, "日別平均差枚の平均": {"枚"},
    "プラス日率": {"%"}, "既存カルテの補正平均差枚": {"枚"}, "店舗平均との差": {"枚"},
}
INPUT_METRICS = {"入力ゲーム数": {"G"}, "入力された期待値": {"枚/1000G"},
                 **{f"入力された設定{i}の推定確率": {"%"} for i in range(1, 7)}}


def normalized(text):
    return unicodedata.normalize("NFKC", str(text or ""))


def suspicious(text):
    return bool(re.search(
        r"(?:規則|指示|命令).{0,30}(?:無視|上書き)|(?:system|assistant|developer)\s*:|"
        r"ignore.{0,30}(?:instructions|rules)|(?:出力|回答)してください|<script\b",
        normalized(text), re.I | re.S))


def safe_url(value):
    if not isinstance(value, str) or re.search(r'[\s\x00-\x1f\x7f<>"\x27\\]', value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme in {"https", "http"} and parts.hostname and not parts.username and not parts.password:
            return value
    except ValueError:
        pass
    return None


def snapshot_hash(snapshot):
    keys = ["contract_version", "target_date", "scope", "evidence", "missing_information"]
    if "constraints" in snapshot:
        keys.append("constraints")
    body = {key: snapshot[key] for key in keys}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def requested_seats(question):
    return {int(a or b) for a, b in re.findall(r"(?:台番号\s*(\d+)|(\d+)\s*番(?:台)?)", normalized(question))}


def fact_reasons(fact, snapshot, question=""):
    reasons = []
    constraints = snapshot.get("constraints") or {}
    kind = fact.get("kind")
    if kind not in {"public_history", "hall_descriptive", "fixed_event_decision", "user_input", "knowledge_reference"}:
        reasons.append("kind")
    for key in ("hall_name", "machine_name", "event_name", "seat_number"):
        expected, actual = constraints.get(key), fact.get(key)
        if expected is not None and key in {"machine_name", "hall_name"}:
            normalizer = normalize_machine_key if key == "machine_name" else canonical_hall_name
            expected, actual = normalizer(expected), normalizer(actual or "")
        if expected is not None and expected != actual:
            reasons.append("scope")
    seats = requested_seats(question)
    if seats and fact.get("seat_number") not in seats:
        reasons.append("scope")
    number = fact.get("seat_number")
    if number is not None and (type(number) is not int or not 1 <= number <= 99999):
        reasons.append("scope")
    dimensions = constraints.get("dimensions")
    if dimensions and fact.get("dimension") not in dimensions:
        reasons.append("scope")
    try:
        target = date.fromisoformat(snapshot["target_date"])
        fact_target = date.fromisoformat(fact["target_date"]) if fact.get("target_date") else target
        upcoming = constraints.get("allow_upcoming_events") and fact.get("event_name")
        if fact_target != target and not (upcoming and fact_target >= target):
            reasons.append("period")
        period = fact.get("period") or {}
        start, end = period.get("start"), period.get("end")
        if bool(start) != bool(end):
            reasons.append("period")
        if start and end:
            if date.fromisoformat(start) > date.fromisoformat(end) or date.fromisoformat(end) >= target:
                reasons.append("period")
            if constraints.get("period_start") and start < constraints["period_start"]:
                reasons.append("period")
            if constraints.get("period_end") and end > constraints["period_end"]:
                reasons.append("period")
    except (KeyError, ValueError, TypeError):
        reasons.append("period")
    metrics = fact.get("metrics")
    allowed = INPUT_METRICS if kind == "user_input" else METRICS
    if kind == "knowledge_reference":
        reasons.extend(knowledge_reasons(fact, snapshot))
        if metrics:
            reasons.append("metric")
    elif not isinstance(metrics, list) or not metrics:
        reasons.append("metric")
    else:
        seen = set()
        for item in metrics:
            if not isinstance(item, dict) or not isinstance(item.get("label"), str) or not isinstance(item.get("unit"), str):
                reasons.append("metric")
                continue
            label, value, unit = item.get("label"), item.get("value"), item.get("unit")
            if label in seen or unit not in allowed.get(label, set()):
                reasons.append("metric")
            seen.add(label)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                reasons.append("metric")
            elif unit in {"%", "点"} and not 0 <= value <= 100:
                reasons.append("metric")
            elif unit in {"日", "台", "台日", "G"} and (value < 0 or int(value) != value):
                reasons.append("metric")
    sources = fact.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(s, dict) or (s.get("url") and not safe_url(s["url"])) for s in sources):
        reasons.append("source")
    # Free labels and source text must never become instructions or endorsed prose.
    text = normalized(json.dumps(fact, ensure_ascii=False))
    if suspicious(text):
        reasons.append("instruction")
    if re.search(r"勝率\s*[:：]?\s*\d|設定\s*[1-6]\s*(?:が)?確定|高設定確定|必ず勝て|絶対に勝", text):
        reasons.append("unsupported_claim")
    return list(dict.fromkeys(reasons))


def knowledge_reasons(fact, snapshot):
    """Text citations stay bound to a validated, versioned local document."""
    doc = fact.get("knowledge")
    if not isinstance(doc, dict):
        return ["knowledge"]
    try:
        body = {k: v for k, v in doc.items() if k != "content_hash"}
        hashed = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
        if hashed != doc["content_hash"] or not doc["document_id"] or not doc["revision"] or not doc["source_locator"]:
            return ["knowledge"]
        if not isinstance(doc["content"], str) or not 1 <= len(doc["content"]) <= 8000:
            return ["knowledge"]
        target = date.fromisoformat(snapshot["target_date"])
        available_by = min(target, date.today())
        if date.fromisoformat(doc["registered_on"]) > available_by or doc["status"] != "active":
            return ["knowledge"]
        if doc.get("reviewed_on") and date.fromisoformat(doc["reviewed_on"]) > available_by:
            return ["knowledge"]
        if (doc.get("valid_from") and date.fromisoformat(doc["valid_from"]) > target) or (doc.get("valid_to") and date.fromisoformat(doc["valid_to"]) < target):
            return ["knowledge"]
        if doc["category"] != (snapshot.get("constraints") or {}).get("knowledge_category"):
            return ["knowledge"]
        if doc["category"] == "expectation" and (not doc.get("reviewed_on") or not all(doc["conditions"].get(k) not in {None, "", "unknown"}
                for k in ("exchange_type", "funding_mode", "reset_status", "metric_name", "unit_label", "condition_label"))):
            return ["knowledge"]
        if doc["category"] in {"expectation", "hall"} and not doc["source_urls"]:
            return ["source"]
        if (doc.get("hall_name") or None) != fact.get("hall_name") or (doc.get("machine_name") or None) != fact.get("machine_name"):
            return ["scope"]
        if doc["source_urls"] != [s["url"] for s in fact["sources"]]:
            return ["source"]
    except (KeyError, ValueError, TypeError):
        return ["knowledge"]
    return []


def question_reasons(question, facts):
    q = normalized(question)
    reasons = []
    if re.search(r"設定\s*[1-6]|高設定|確定設定|設定.{0,8}的中", q):
        reasons.append("setting")
    if re.search(r"勝率|稼げ|稼ぐ|いくら勝|月.{0,8}(?:収益|利益|儲)|儲か|必ず勝|絶対勝", q):
        reasons.append("profit")
    if re.search(r"角台|隣接|隣の|隣り|並び投入|全台高設定", q):
        reasons.append("layout")
    if requested_seats(q) and not any(f.get("seat_number") in requested_seats(q) for f in facts):
        reasons.append("identity")
    if any(word in q for word in ("イベント", "旧イベ", "取材")) and not any(f.get("event_name") for f in facts):
        reasons.append("event")
    return reasons


def question_scope_reasons(fact, facts, question):
    """Match explicit entities already known to the input; do not invent aliases."""
    q = normalized(question)
    codes = []
    for field in ("hall_name", "machine_name", "event_name"):
        named = {f.get(field) for f in facts if f.get(field) and normalized(f[field]) in q}
        if named and fact.get(field) not in named:
            codes.append("scope")
    # Explicit full dates describe a range. A single date may be the visit date.
    dates = re.findall(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", q)
    if len(dates) == 2:
        period = fact.get("period") or {}
        if not period.get("start") or not period.get("end") or period["start"] < min(dates) or period["end"] > max(dates):
            codes.append("period")
    if fact.get("dimension") == "weekday":
        asked = {w + "曜日" for w in "月火水木金土日" if w + "曜" in q}
        if asked and not any(day in (fact.get("subject_label") or "") for day in asked):
            codes.append("scope")
    return codes


def inspect_snapshot(snapshot, question=""):
    rejected, warnings = [], []
    try:
        intact = snapshot_hash(snapshot) == snapshot.get("snapshot_id")
    except (KeyError, ValueError, TypeError):
        intact = False
    facts = snapshot.get("evidence") or []
    ids = [f.get("id") for f in facts]
    intact = intact and len(ids) == len(set(ids))
    accepted = []
    for fact in facts:
        reasons = (fact_reasons(fact, snapshot, question) + question_scope_reasons(fact, facts, question)) if intact else ["integrity"]
        if reasons:
            rejected.append({"evidence_id": fact.get("id"), "codes": reasons})
        else:
            accepted.append(fact["id"])
            if fact.get("kind") != "knowledge_reference" and (not fact.get("sources") or any(not s.get("url") or not s.get("retrieved_at") for s in fact["sources"])):
                warnings.append("source_missing")
    blocked = question_reasons(question, [f for f in facts if f.get("id") in accepted])
    if not intact:
        blocked.append("integrity")
    return {"version": VERSION, "allowed_ids": accepted, "rejected": rejected,
            "blocked_codes": list(dict.fromkeys(blocked)), "warning_codes": list(dict.fromkeys(warnings))}


def check_selection(ids, snapshot, question=""):
    report = inspect_snapshot(snapshot, question)
    # An abstention is allowed even when the question cannot be answered.
    rejected = bool(ids) and (bool(report["blocked_codes"]) or not set(ids) <= set(report["allowed_ids"]))
    return {**report, "passed": not rejected}


def display_report(report):
    codes = list(dict.fromkeys(report["blocked_codes"] + [c for r in report["rejected"] for c in r["codes"]]))
    return {"version": VERSION, "excluded_count": len(report["rejected"]),
            "reasons": [REASONS[c] for c in codes],
            "warnings": [REASONS[c] for c in report["warning_codes"]]}
