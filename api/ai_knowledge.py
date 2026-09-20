"""Versioned local retrieval. No network, model training, private DB or writes."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from api.ai_evidence import answer, evidence, freeze_evidence
from api.ai_guard import safe_url, suspicious
from config import ROOT
from hall.machine_scope import normalize_machine_key
from hall.names import canonical_hall_name
from hall.regions import SHIJONAWATE_AREA_HALLS

VERSION = "knowledge-retrieval-1.0"
DIRECTORY = ROOT / "data" / "knowledge"
REGISTERED_ON = "2026-09-19"
NOTICE = "登録資料の検索・引用です。追加学習、当日の着席許可、予測精度・利益の保証ではありません。外部通信・課金はありません。"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def compact(value):
    return re.sub(r"[\s・･!！?？:：、。\-]", "", normalize_machine_key(str(value)))


class Document(BaseModel):
    model_config = {"extra": "forbid"}
    document_id: str = Field(min_length=1, max_length=180, pattern=r"^[a-zA-Z0-9_.:-]+$")
    revision: str = Field(min_length=1, max_length=80)
    category: Literal["glossary", "machine", "expectation", "hall"]
    topic_key: str = Field(min_length=1, max_length=180)
    title: str = Field(min_length=1, max_length=220)
    content: str = Field(min_length=1, max_length=8000)
    keywords: list[str] = Field(min_length=1, max_length=40)
    machine_name: str = Field(default="", max_length=120)
    hall_name: str = Field(default="", max_length=120)
    conditions: dict[str, str] = Field(default_factory=dict)
    source_label: str = Field(min_length=1, max_length=200)
    source_locator: str = Field(min_length=1, max_length=300)
    source_urls: list[str] = Field(default_factory=list, max_length=20)
    reviewed_on: date | None = None
    registered_on: date
    valid_from: date | None = None
    valid_to: date | None = None
    status: Literal["active", "withdrawn"] = "active"
    verification: str = Field(min_length=1, max_length=160)


def read_json(path):
    if path.stat().st_size > 5_000_000:
        raise ValueError("knowledge input too large")
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_documents(catalog):
    """Quote shipped rules, not personal/custom DB profiles. Never run enrichment."""
    docs, machines = [], defaultdict(list)
    for profile in catalog.get("profiles", []):
        if not profile.get("active", True) or profile.get("confidence") not in {"verified", "official"}:
            continue
        name, key = profile["machine_name"], profile["catalog_key"]
        numbers = [profile.get("start_threshold"), profile.get("expected_value_yen")]
        if any(type(n) not in {int, float} or not math.isfinite(n) for n in numbers) or numbers[0] < 0:
            continue
        # The source's starting EV must be traceable to the same curve point.
        if not any(p.get("value") == numbers[0] and p.get("ev_yen") == numbers[1] for p in profile.get("curve_points", [])):
            continue
        conditions = {k: str(profile.get(k, "unknown")) for k in ("exchange_type", "funding_mode", "reset_status", "metric_name", "unit_label")}
        conditions["condition_label"] = profile["condition_label"]
        content = (f"保存済みカタログの開始値：{numbers[0]}{conditions['unit_label']}。"
                   f"その開始値に対応する登録期待値：{numbers[1]}円。"
                   f"やめ時の登録：{profile['stop_rule']}\n"
                   f"前提：{profile.get('notes', '')}\n差異・注意：{profile.get('discrepancy_note', '')}\n"
                   "表示値は登録時の資料です。現在値への補間、閉店欠損、資金・再プレイ上限の再計算はしていません。")
        urls = list(dict.fromkeys(([profile.get("source_url")] + profile.get("source_urls", []))))
        docs.append(dict(document_id="ev:" + key, revision=digest(profile), category="expectation", topic_key=key,
            title=f"{name}／{profile['condition_label']}", content=content, keywords=[name, "期待値", "ボーダー", "やめ時", "天井"],
            machine_name=name, conditions=conditions, source_label=profile.get("source_name") or "登録済み期待値表",
            source_locator=f"data/opportunity_catalog.json#profiles/{key}", source_urls=[url for url in urls if url],
            reviewed_on=profile.get("verified_on"), registered_on=REGISTERED_ON,
            verification="カタログの確認済み表示を継承・今回の出典再確認なし"))
        machines[name].append(key)
    for name, keys in sorted(machines.items()):
        docs.append(dict(document_id="machine:" + digest(name)[:16], revision=digest(sorted(keys)), category="machine",
            topic_key="registered-rules", title=name + "の登録資料", keywords=[name, "機種", "対応", "登録"], machine_name=name,
            content=f"この機種には{len(keys)}種類の期待値条件がカタログに登録されています。数値を見る場合は「期待値条件」で条件を指定してください。現在の設置、最新仕様、設定の確定を示す資料ではありません。",
            source_label="PACHI TOOL同梱カタログ", source_locator="data/opportunity_catalog.json",
            reviewed_on=None, registered_on=REGISTERED_ON, verification="同梱ファイルの収録状況・外部仕様の保証なし"))
    return docs


def hall_documents():
    from hall.collection_sources import collection_source_plan
    docs = []
    for hall in sorted(SHIJONAWATE_AREA_HALLS):
        plan = collection_source_plan(hall)
        sources = [s for s in plan["sources"] if s["supported"] and s["source_url"]]
        docs.append(dict(document_id="hall:" + digest(hall)[:16], revision=digest(sources), category="hall",
            topic_key="collection-references", title=hall + "の登録参照先", hall_name=hall,
            keywords=[hall, "資料", "参照先", "出典", "データ", "収集"],
            content="登録されている収集経路：\n" + "\n".join(f"・{s['label']}：{s['note']}" for s in sources)
                + "\n登録先の一覧であり、現在のアクセス成功・設置・イベント開催・出玉の強さは確認していません。実績傾向は店舗質問・分析画面で別に確認してください。",
            source_label="PACHI TOOLの店舗別収集経路設定", source_locator="hall/collection_sources.py#" + hall,
            source_urls=[s["source_url"] for s in sources], registered_on=REGISTERED_ON, reviewed_on=None,
            verification="コードに登録された参照先・今回のアクセス確認なし"))
    return docs


def load_index(*, directory=DIRECTORY, catalog_path=None, include_halls=True):
    """Bounded allowlist only. No filesystem crawling, .env, sessions, OCR or conversation."""
    rejected, raw_docs, errors = 0, [], []
    for path, kind in [(Path(directory) / "documents_v1.json", "knowledge"),
                       (Path(catalog_path or ROOT / "data" / "opportunity_catalog.json"), "catalog")]:
        try:
            data = read_json(path)
            raw_docs.extend(data["documents"] if kind == "knowledge" else catalog_documents(data))
        except (OSError, ValueError, KeyError, TypeError):
            errors.append("専用資料" if kind == "knowledge" else "期待値カタログ")
    if include_halls:
        try:
            raw_docs.extend(hall_documents())
        except (ImportError, ValueError, KeyError, TypeError):
            errors.append("店舗参照先")
    documents, quarantined = [], set()
    if len(raw_docs) > 2000:
        errors.append("資料件数上限（2,000件）超過")
    for raw in raw_docs[:2000]:
        try:
            doc = Document.model_validate(raw).model_dump(mode="json")
            if any(not safe_url(url) for url in doc["source_urls"]) or suspicious(json.dumps(doc, ensure_ascii=False)):
                raise ValueError("unsafe source")
            if any(not isinstance(x, str) or not x.strip() for x in doc["keywords"]):
                raise ValueError("invalid keyword")
            if doc["valid_from"] and doc["valid_to"] and doc["valid_from"] > doc["valid_to"]:
                raise ValueError("invalid validity")
            doc["content_hash"] = digest(doc)
            documents.append(doc)
        except (ValidationError, ValueError, TypeError):
            rejected += 1
            if isinstance(raw, dict) and isinstance(raw.get("document_id"), str):
                quarantined.add(raw["document_id"])
    documents = [d for d in documents if d["document_id"] not in quarantined]
    documents.sort(key=lambda d: (d["document_id"], d["registered_on"], d["revision"]))
    return {"version": VERSION, "index_hash": digest(documents), "documents": documents,
            "rejected_count": rejected, "load_errors": errors}


def current_documents(index, target_date, *, today=None):
    """Latest known revision first; expiry/withdrawal never resurrects older text."""
    by_id, warnings = defaultdict(list), []
    available_by = min(target_date, today or date.today().isoformat())
    for doc in index["documents"]:
        if doc["registered_on"] <= available_by:
            by_id[doc["document_id"]].append(doc)
    result = []
    for docs in by_id.values():
        latest = max(d["registered_on"] for d in docs)
        versions = {d["content_hash"]: d for d in docs if d["registered_on"] == latest}
        if len(versions) != 1:
            warnings.append("同じ資料の版が競合しているため除外しました")
            continue
        doc = next(iter(versions.values()))
        # Pick the latest registered revision before checking review eligibility.
        # A future review date must not make a superseded version reappear.
        if doc.get("reviewed_on") and doc["reviewed_on"] > available_by:
            continue
        if doc["status"] != "active" or (doc["valid_from"] and doc["valid_from"] > target_date) or (doc["valid_to"] and doc["valid_to"] < target_date):
            continue
        result.append(doc)
    groups = defaultdict(list)
    for doc in result:
        groups[(doc["category"], normalize_machine_key(doc["machine_name"]), canonical_hall_name(doc["hall_name"]),
                doc["topic_key"], json.dumps(doc["conditions"], sort_keys=True))].append(doc)
    safe = []
    for docs in groups.values():
        if len({d["content"] for d in docs}) > 1:
            warnings.append("同一条件の資料内容が競合しているため除外しました")
        else:
            safe.extend(docs)
    return safe, list(dict.fromkeys(warnings))


def index_status(index=None):
    index = index if index is not None else load_index()
    docs, warnings = current_documents(index, date.today().isoformat())
    return {"version": VERSION, "index_hash": index["index_hash"], "counts": dict(Counter(d["category"] for d in docs)),
        "machines": sorted({d["machine_name"] for d in docs if d["machine_name"]}),
        "halls": sorted({d["hall_name"] for d in docs if d["hall_name"]}),
        "conditions": [{"id": d["document_id"], "title": d["title"], "machine_name": d["machine_name"]} for d in docs if d["category"] == "expectation"],
        "rejected_count": index["rejected_count"], "load_errors": index["load_errors"], "warnings": warnings,
        "external_calls": 0, "training_enabled": False, "notice": NOTICE}


def knowledge_fact(doc, target_date):
    warnings = ["資料は現在保存されている版です。厳密な当時の情報到達・事前予測は再現しません"]
    if not doc["reviewed_on"]:
        warnings.append("元資料の確認日は未記録です")
    elif (date.fromisoformat(target_date) - date.fromisoformat(doc["reviewed_on"])).days > 30:
        warnings.append("確認日から30日超の資料です。現在の条件を再確認してください")
    fact = evidence(hall=doc["hall_name"] or None, machine=doc["machine_name"] or None,
        target=target_date, kind="knowledge_reference", source_label=doc["source_label"],
        sources=[{"url": u, "retrieved_at": None, "label": doc["source_label"]} for u in doc["source_urls"]])
    fact["missing_information"] = warnings
    fact["knowledge"] = doc
    return fact


def search_knowledge(*, question, category="glossary", target_date=None, machine_name="", hall_name="", condition_id="", index=None):
    target = target_date or date.today().isoformat()
    date.fromisoformat(target)
    if category not in {"glossary", "machine", "expectation", "hall"}:
        raise ValueError("資料の種類が不正です")
    index = index if index is not None else load_index()
    docs, gaps = current_documents(index, target)
    q = compact(question)
    machine = normalize_machine_key(machine_name)
    hall = canonical_hall_name(hall_name)
    # Explicit known entities in the question must agree with filter selections.
    named_machines = {d["machine_name"] for d in docs if d["machine_name"] and normalize_machine_key(d["machine_name"]) in q}
    named_halls = {d["hall_name"] for d in docs if d["hall_name"] and compact(d["hall_name"]) in q}
    if machine and named_machines and machine not in {normalize_machine_key(n) for n in named_machines}:
        gaps.append("質問と選択機種が一致しません")
        docs = []
    if hall and named_halls and hall not in named_halls:
        gaps.append("質問と選択店舗が一致しません")
        docs = []
    if not machine and len(named_machines) == 1:
        machine = normalize_machine_key(next(iter(named_machines)))
    if not hall and len(named_halls) == 1:
        hall = next(iter(named_halls))
    pool = [d for d in docs if d["category"] == category
            and (not machine or normalize_machine_key(d["machine_name"]) == machine)
            and (not hall or category != "hall" or canonical_hall_name(d["hall_name"]) == hall)]
    notice = ""
    if category == "expectation" and not condition_id:
        notice = "期待値は条件を選択してから引用します。等価・非等価、投資方法、リセット、当選後の状態を資料と照合してください。"
        selected = []
    elif category == "hall" and not hall:
        notice = "資料を探す店舗を1店選んでください。"
        selected = []
    elif category == "machine" and not machine:
        notice = "登録資料を探す機種を1つ選んでください。"
        selected = []
    elif category == "expectation":
        selected = [d for d in pool if d["document_id"] == condition_id]
        for doc in selected:
            c = doc["conditions"]
            required = ("exchange_type", "funding_mode", "reset_status", "metric_name", "unit_label", "condition_label")
            if not all(isinstance(c.get(key), str) and c[key].strip() not in {"", "unknown"} for key in required):
                selected = []
                notice = "選択した資料の期待値条件が不足しています。交換・投資・リセット・計数条件を補完せず、引用を見合わせます。"
                break
            conflicts = (("現金" in question and c["funding_mode"] == "medals")
                or (any(w in question for w in ("持ちメダル", "貯メダル")) and c["funding_mode"] == "cash")
                or ("非等価" in question and c["exchange_type"] == "equivalent")
                or (re.search(r"(?<!非)等価", question) and c["exchange_type"] != "equivalent")
                or ("リセット確定" in question and c["reset_status"] != "reset_confirmed")
                or ("据え置き" in question and c["reset_status"] == "reset_confirmed")
                or any(w in question and w not in c["metric_name"] for w in ("AT間", "CZ間", "ボーナス間")))
            if conflicts:
                selected = []
                notice = "質問と選択した交換・投資・リセット・計数条件が一致しません。条件を選び直してください。"
                break
    else:
        ranked = [(sum(len(compact(k)) for k in d["keywords"] if compact(k) and compact(k) in q), d) for d in pool]
        if category in {"hall", "machine"}:
            ranked = [(max(score, 1), d) for score, d in ranked]
        selected = [d for score, d in sorted(ranked, key=lambda pair: (-pair[0], pair[1]["document_id"])) if score > 0][:8]
    if not selected and not notice:
        notice = "選択条件・質問に合う登録資料がありません。別の資料や数値で補完しません。"
    if category != "glossary" and re.search(r"打てる|打って|座って|座れる|着席|何円勝|儲かる", question):
        selected = []
        notice = "専用資料の検索では現在の着席可否や利益は判断しません。現場判定に必要な条件を確認してください。"
    if index["load_errors"] or index["rejected_count"]:
        gaps.append("読めない、または形式・安全性を確認できない資料を除外しています")
    snapshot = freeze_evidence([knowledge_fact(d, target) for d in selected], target_date=target,
        scope=hall if category == "hall" else machine_name or "専用知識",
        constraints={"knowledge_category": category}, missing=gaps + [NOTICE])
    result = answer(snapshot, question=question, client=None, required_notice=notice)
    return {**result, "knowledge_version": VERSION, "index_hash": index["index_hash"], "category": category,
        "engine": "専用知識検索（外部AI不使用）", "external_calls": 0, "training_enabled": False,
        "condition_options": [{"id": d["document_id"], "title": d["title"]} for d in pool if category == "expectation"],
        "matched_documents": [f["knowledge"]["document_id"] for f in result["evidence"]],
        "notice": NOTICE}
