"""Versioned, append-only evaluation of evidence selection, independent of forecasts."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from itertools import combinations
import json
import math
import os
from pathlib import Path
import sqlite3
from threading import RLock, Thread
from time import perf_counter
from uuid import UUID, uuid4

from api.ai_evidence import evidence, evidence_messages, freeze_evidence, metric, validate_answer
from api.ai_provider import AICompletion, AIProviderClient, AIProviderError, comparison_unit_prices, get_comparison_provider_config
from api.ai_guard import check_selection, display_report, VERSION as GUARD_VERSION
from api.ai_governance import AIGovernanceError, AIGovernanceService, governance
from app_version import APP_VERSION
from config import DATA_DIR, ROOT

PROTOCOL = "evidence-selection-eval-2.0.0"
SUITE_PATH = ROOT / "data" / "ai_eval" / "suite_v2.json"
NOTICE = "固定の検証問題の成績です。店舗予測の的中率・高設定率・本人の勝率ではありません。"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("評価時刻にはタイムゾーンが必要です")
    return result


def load_suite(path=SUITE_PATH):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cutoff = timestamp(raw["cutoff"])
    if cutoff.date().isoformat() >= raw["target_date"]:
        raise ValueError("入力期限は対象日より前にしてください")
    cases, seen = [], set()
    for definition in raw["cases"]:
        if definition["id"] in seen:
            raise ValueError("評価問題IDが重複しています")
        seen.add(definition["id"])
        facts, keys, excluded = [], [], []
        if len(set(definition["input_keys"])) != len(definition["input_keys"]):
            raise ValueError("入力根拠が重複しています")
        for key in definition["input_keys"]:
            source = raw["facts"][key]
            start, end = source.get("start", "2026-09-01"), source.get("end", "2026-09-08")
            available_at = source.get("available_at", "2026-09-09T01:00:00+09:00")
            if end > cutoff.date().isoformat() or timestamp(available_at) > cutoff:
                excluded.append(key)
                continue
            facts.append(evidence(hall=source["hall"], machine=source.get("machine"),
                seat=source.get("seat"), event=source.get("event"), target=raw["target_date"],
                start=start, end=end, metrics=[metric("公表平均差枚", source["value"], "枚")],
                sources=[] if source.get("missing_source") else [{"url": "https://example.invalid/" + key,
                    "retrieved_at": available_at, "label": "検証用架空資料"}],
                missing=source.get("missing", []), decision=source.get("decision"), source_label="検証用架空資料"))
            facts[-1]["subject_label"] = source["label"]
            keys.append(key)
        snapshot = freeze_evidence(facts, target_date=raw["target_date"], scope="固定評価用・架空店舗",
                                   missing=[raw["notice"]], constraints=definition.get("constraints"))
        # Stable across runs, never the machine's current clock.
        snapshot["generated_at"] = raw["cutoff"]
        mapping = {key: fact["id"] for key, fact in zip(keys, snapshot["evidence"])}
        expected, forbidden = set(definition["expected_keys"]), set(definition["forbidden_keys"])
        if expected & forbidden or expected | forbidden != set(keys) or len(expected) > 8:
            raise ValueError("正解と禁止根拠の定義が入力根拠と一致しません")
        outcome = definition.get("outcome")
        if outcome and (timestamp(outcome["available_at"]) <= cutoff or outcome["report_date"] < raw["target_date"]):
            raise ValueError("後日の結果が入力期限と矛盾しています")
        messages = evidence_messages(snapshot, definition["question"])
        cases.append({"id": definition["id"], "category": definition["category"],
            "question": definition["question"], "snapshot": snapshot,
            "expected_ids": sorted(mapping[k] for k in expected),
            "forbidden_ids": sorted(mapping[k] for k in forbidden), "reason": definition["reason"],
            "excluded_inputs": excluded, "outcome": outcome,
            "messages": messages, "prompt_hash": digest(messages)})
    return {"version": raw["version"], "name": raw["name"], "data_kind": raw["data_kind"],
            "notice": raw["notice"], "suite_hash": digest(raw), "target_date": raw["target_date"],
            "cutoff": raw["cutoff"], "cases": cases}


def score_response(case, raw):
    """Gold labels are explicit, never derived from response or first N facts."""
    try:
        ids = validate_answer(raw, case["snapshot"], allow_abstention=True)
    except (ValueError, TypeError):
        return {"schema_valid": False, "selected_ids": [], "exact_match": False,
            "abstained": False, "missing_ids": case["expected_ids"], "unnecessary_ids": [],
            "forbidden_ids": [], "precision_pct": None, "recall_pct": None, "error_kind": "invalid_contract"}
    expected, selected = set(case["expected_ids"]), set(ids)
    guard = check_selection(ids, case["snapshot"], case["question"])
    return {"schema_valid": True, "selected_ids": ids, "exact_match": selected == expected,
        "guard_passed": guard["passed"], "answer_guard": display_report(guard),
        "abstained": not selected, "missing_ids": sorted(expected - selected),
        "unnecessary_ids": sorted(selected - expected),
        "forbidden_ids": sorted(selected & set(case["forbidden_ids"])),
        "precision_pct": round(100 * len(selected & expected) / len(selected), 1) if selected else None,
        "recall_pct": round(100 * len(selected & expected) / len(expected), 1) if expected else None,
        "error_kind": None}


def pct(numerator, denominator):
    return round(100 * numerator / denominator, 1) if denominator else None


def summarize(cases, samples, repeats):
    total = len(cases) * repeats
    by_case = defaultdict(list)
    for sample in samples:
        by_case[sample["case_id"]].append(sample)
    abstention = {case["id"] for case in cases if not case["expected_ids"]}
    valid = [s for s in samples if s["schema_valid"]]
    selected_count = sum(len(s["selected_ids"]) for s in valid)
    relevant_count = sum(len(s["selected_ids"]) - len(s["unnecessary_ids"]) for s in valid)
    expected_count = sum(len(c["expected_ids"]) for c in cases) * repeats
    pairs = [pair for rows in by_case.values() for pair in combinations(rows, 2)
             if all(s["schema_valid"] for s in pair)]
    equal_pairs = sum(a["selected_ids"] == b["selected_ids"] for a, b in pairs)
    guarded = [s for s in samples if "guard_passed" in s]
    tokens = {}
    for key in ("input_tokens", "output_tokens", "estimated_cost_usd"):
        recorded = [s[key] for s in samples if s[key] is not None]
        tokens[key] = sum(recorded) if recorded and len(recorded) == len(samples) else None
    return {"total": total, "completed": len(samples), "valid_count": len(valid),
        "guard_version": GUARD_VERSION, "guard_evaluated_count": len(guarded),
        "guard_rejected_count": sum(not s["guard_passed"] for s in guarded),
        "guard_pass_pct": pct(sum(s["guard_passed"] for s in guarded), len(guarded)),
        "exact_count": sum(s["exact_match"] for s in samples),
        "exact_match_pct": pct(sum(s["exact_match"] for s in samples), len(samples)),
        "schema_pass_pct": pct(len(valid), len(samples)),
        "precision_pct": pct(relevant_count, selected_count),
        "recall_pct": pct(relevant_count, expected_count) if len(samples) == total else None,
        "unnecessary_count": selected_count - relevant_count,
        "forbidden_count": sum(len(s["forbidden_ids"]) for s in valid),
        "abstention_pass_pct": pct(sum(s["exact_match"] for s in samples if s["case_id"] in abstention),
                                    sum(s["case_id"] in abstention for s in samples)),
        "consistency_pct": pct(equal_pairs, len(pairs)), "consistency_pairs": len(pairs),
        "planned_pairs": len(cases) * repeats * (repeats - 1) // 2,
        "external_calls": sum(s["external_call"] for s in samples), **tokens}


SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_runs (
  id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, request_json TEXT NOT NULL,
  created_at TEXT NOT NULL, manifest_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS eval_samples (
  run_id TEXT NOT NULL REFERENCES eval_runs(id), case_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  sample_json TEXT NOT NULL, PRIMARY KEY(run_id,case_id,attempt));
CREATE TABLE IF NOT EXISTS eval_finishes (
  run_id TEXT PRIMARY KEY REFERENCES eval_runs(id), status TEXT NOT NULL,
  finished_at TEXT NOT NULL, error_kind TEXT, summary_json TEXT NOT NULL);
"""


class EvaluationConflict(ValueError):
    pass


class EvaluationService:
    """One worker per server, immutable inputs/results, no automatic paid retries."""
    def __init__(self, db_path=DATA_DIR / "ai_evaluations.db", suite_path=SUITE_PATH,
                 client_factory=AIProviderClient, governance_service: AIGovernanceService | None = None):
        self.db_path, self.suite_path = Path(db_path), Path(suite_path)
        self.client_factory = client_factory
        self.governance_service = governance_service
        self._guard, self._active = RLock(), None

    def _connect(self, *, write=False):
        if write:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            for table in ("eval_runs", "eval_samples", "eval_finishes"):
                for operation in ("UPDATE", "DELETE"):
                    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{operation} BEFORE {operation} ON {table} "
                                 "BEGIN SELECT RAISE(ABORT,'evaluation records are immutable'); END")
            conn.commit()
        else:
            conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def catalog(self):
        suite = load_suite(self.suite_path)
        runs = []
        if self.db_path.exists():
            with closing(self._connect()) as conn:
                ids = [row[0] for row in conn.execute("SELECT id FROM eval_runs ORDER BY rowid DESC LIMIT 10")]
            runs = [self.report(run_id, details=False) for run_id in ids]
        with self._guard:
            active = self._active
        return {"protocol": PROTOCOL, "suite_hash": suite["suite_hash"], "suite_version": suite["version"],
            "suite_name": suite["name"], "data_kind": suite["data_kind"], "notice": suite["notice"] + NOTICE,
            "cases": [{"id": c["id"], "category": c["category"], "question": c["question"]} for c in suite["cases"]],
            "runs": runs, "active_run_id": active, "default_mode": "offline", "max_repeats": 3}

    def start(self, *, request_id, suite_hash, mode="offline", provider=None, repeats=2,
              confirm_external=False, confirm_public_data_only=False, environ=None, launch=None):
        request_id = str(UUID(str(request_id)))
        if mode not in {"offline", "external"} or isinstance(repeats, bool) or repeats not in (1, 2, 3):
            raise ValueError("評価モード・反復回数が不正です")
        if mode == "offline" and provider is not None:
            raise ValueError("ローカル点検に外部AI名を指定できません")
        request = {"suite_hash": suite_hash, "mode": mode, "provider": provider, "repeats": repeats,
                   "confirm_external": confirm_external,
                   "confirm_public_data_only": confirm_public_data_only}
        with self._guard:
            # Idempotency survives a lost HTTP response or a server restart.
            if self.db_path.exists():
                with closing(self._connect()) as conn:
                    old = conn.execute("SELECT id,request_json FROM eval_runs WHERE request_id=?", (request_id,)).fetchone()
                if old:
                    if old["request_json"] != encoded(request):
                        raise EvaluationConflict("同じ実行IDで評価条件を変更できません")
                    return self.report(old["id"])
            if self._active:
                raise EvaluationConflict("評価を実行中です。進捗を確認してください")
            suite = load_suite(self.suite_path)
            if suite_hash != suite["suite_hash"]:
                raise EvaluationConflict("評価問題が更新されています。画面を更新してください")
            env = dict(os.environ if environ is None else environ)
            config = None
            if mode == "external":
                if self.governance_service is None:
                    raise AIGovernanceError("外部AIの費用・送信管理が設定されていません")
                config = get_comparison_provider_config(provider or "", env)
                if not confirm_external or not config or not config.available:
                    raise ValueError("外部AIの設定・比較許可・実行確認が必要です")
                if not confirm_public_data_only:
                    raise ValueError("固定評価問題だけを外部送信する確認が必要です")
            prices = comparison_unit_prices(provider or "", env) if config else (None, None)
            run_id = str(uuid4())
            manifest = {"protocol": PROTOCOL, "app_version": APP_VERSION, "suite": suite,
                "mode": mode, "provider": provider, "model": config.model if config else "first-eight-reference-v1",
                "label": config.definition.label if config else "ローカル参考方式（AI未使用）",
                "endpoint_hash": digest(config.base_url) if config else None,
                "repeats": repeats, "max_tokens": 600, "unit_prices_usd_per_million": prices,
                "prompt_hash": digest([c["messages"] for c in suite["cases"]]),
                "notice": NOTICE, "changes_live_prediction": False}
            if config and self.governance_service:
                calls = [deepcopy(case["messages"]) for case in suite["cases"] for _ in range(repeats)]
                manifest["governance"] = self.governance_service.authorize(
                    request_id=request_id, provider=config.definition.name, model=config.model,
                    purpose="evidence_evaluation", call_messages=calls,
                    max_tokens=manifest["max_tokens"], confirmed=confirm_external and confirm_public_data_only,
                    environ=env)
            with closing(self._connect(write=True)) as conn, conn:
                conn.execute("INSERT INTO eval_runs VALUES(?,?,?,?,?)", (run_id, request_id, encoded(request), now(), encoded(manifest)))
            self._active = run_id
            try:
                worker = lambda: self._execute(run_id, manifest, config, env)
                if launch is None:
                    Thread(target=worker, daemon=True, name="ai-evaluation").start()
                else:
                    launch(worker)
            except Exception:
                self._finish(run_id, "failed", "start_failed")
                self._active = None
                raise
        return self.report(run_id)

    def _finish(self, run_id, status, error=None):
        with closing(self._connect(write=True)) as conn, conn:
            manifest = json.loads(conn.execute("SELECT manifest_json FROM eval_runs WHERE id=?", (run_id,)).fetchone()[0])
            samples = [json.loads(r[0]) for r in conn.execute("SELECT sample_json FROM eval_samples WHERE run_id=?", (run_id,))]
            summary = summarize(manifest["suite"]["cases"], samples, manifest["repeats"])
            conn.execute("INSERT INTO eval_finishes VALUES(?,?,?,?,?)", (run_id, status, now(), error, encoded(summary)))

    def _execute(self, run_id, manifest, config, environ=None):
        try:
            client = self.client_factory(config) if config else None
            call_index = 0
            for case in manifest["suite"]["cases"]:
                for attempt in range(1, manifest["repeats"] + 1):
                    started, completion, error = perf_counter(), None, None
                    if client:
                        call_index += 1
                    try:
                        if client:
                            # Only frozen input, never the gold labels or later outcomes.
                            completion = self.governance_service.complete_call(client,
                                request_id=manifest["governance"]["request_id"], call_index=call_index,
                                messages=case["messages"], max_tokens=manifest["max_tokens"], environ=environ)
                        else:
                            completion = AICompletion(encoded({"snapshot_id": case["snapshot"]["snapshot_id"],
                                "claims": [{"evidence_id": f["id"]} for f in case["snapshot"]["evidence"][:8]]}))
                        score = score_response(case, completion.content)
                    except AIGovernanceError:
                        raise
                    except Exception as exc:
                        error = exc.kind if isinstance(exc, AIProviderError) and exc.kind in {
                            "disabled", "timeout", "network", "authentication", "rate_limit", "provider_unavailable", "request", "invalid_response"
                        } else "provider_error"
                        score = score_response(case, "")
                        score["error_kind"] = error
                    usage = {}
                    for key in ("input_tokens", "output_tokens"):
                        value = getattr(completion, key, None)
                        usage[key] = value if client and type(value) is int and value >= 0 else None
                    price_in, price_out = manifest["unit_prices_usd_per_million"]
                    cost = None
                    if all(v is not None for v in (*usage.values(), price_in, price_out)):
                        amount = (usage["input_tokens"] * price_in + usage["output_tokens"] * price_out) / 1_000_000
                        cost = round(amount, 8) if math.isfinite(amount) else None
                    sample = {**score, **usage, "case_id": case["id"], "attempt": attempt,
                        "latency_ms": round((perf_counter() - started) * 1000) if client else None,
                        "estimated_cost_usd": cost, "external_call": bool(client),
                        "completed_at": now(), "prompt_hash": case["prompt_hash"]}
                    with closing(self._connect(write=True)) as conn, conn:
                        conn.execute("INSERT INTO eval_samples VALUES(?,?,?,?)", (run_id, case["id"], attempt, encoded(sample)))
            self._finish(run_id, "completed")
        except Exception:
            self._finish(run_id, "failed", "evaluation_failed")
        finally:
            with self._guard:
                self._active = None

    def report(self, run_id, *, details=True):
        with self._guard:
            return self._report(run_id, details=details)

    def _report(self, run_id, *, details=True):
        if not self.db_path.exists():
            raise KeyError(run_id)
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM eval_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            finish = conn.execute("SELECT * FROM eval_finishes WHERE run_id=?", (run_id,)).fetchone()
            samples = [json.loads(r[0]) for r in conn.execute(
                "SELECT sample_json FROM eval_samples WHERE run_id=? ORDER BY case_id,attempt", (run_id,))]
        manifest = json.loads(row["manifest_json"])
        suite = manifest["suite"]
        with self._guard:
            status = finish["status"] if finish else ("running" if self._active == run_id else "interrupted")
        summary = json.loads(finish["summary_json"]) if finish else summarize(suite["cases"], samples, manifest["repeats"])
        result = {"id": run_id, "status": status, "created_at": row["created_at"],
            "finished_at": finish["finished_at"] if finish else None,
            "mode": manifest["mode"], "label": manifest["label"], "model": manifest["model"],
            "provider": manifest["provider"], "protocol": manifest["protocol"],
            "suite_version": suite["version"], "suite_hash": suite["suite_hash"],
            "prompt_hash": manifest["prompt_hash"], "app_version": manifest["app_version"],
            "data_kind": suite["data_kind"], "repeats": manifest["repeats"],
            "summary": summary, "notice": suite["notice"] + manifest["notice"],
            "changes_live_prediction": False,
            "progress_pct": 100 if status == "completed" else min(99, int(100 * len(samples) / summary["total"]))}
        if details:
            result.update(samples=samples, cases=[{k: v for k, v in c.items() if k != "messages"} for c in suite["cases"]])
        return result


evaluations = EvaluationService(governance_service=governance)
