"""v3.48: paid AI privacy, budget and duplicate-charge guard.

The ledger intentionally stores hashes and usage metadata only. Prompts, answers,
API keys and personal play history are never persisted here.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Mapping

from api.ai_provider import AICompletion, PROVIDERS, comparison_unit_prices
from config import AI_USAGE_DB

PROTOCOL = "ai-governance-1.1.0"
SENSITIVE_PATTERNS = (
    ("email", re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")),
    # Domestic phone numbers have 10 or 11 digits. Shorter zero-leading
    # fragments can be normal dates or timestamp microseconds, not contacts.
    ("phone", re.compile(r"(?<!\d)0(?:\d[-ー ]?){8,9}\d(?!\d)")),
    ("secret", re.compile(r"(?i)(?:api[_ -]?key|secret|token|password|パスワード)\s*[:=：]\s*\S+")),
    ("card", re.compile(r"(?<!\d)(?:\d[ -]?){15,19}(?!\d)")),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_usage_requests (
  request_id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
  purpose TEXT NOT NULL, payload_hash TEXT NOT NULL, call_count INTEGER NOT NULL,
  reserved_usd REAL NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ai_usage_calls (
  request_id TEXT NOT NULL REFERENCES ai_usage_requests(request_id), call_index INTEGER NOT NULL,
  status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
  error_kind TEXT, recorded_at TEXT NOT NULL, PRIMARY KEY(request_id,call_index));
CREATE TABLE IF NOT EXISTS ai_usage_terms (
  request_id TEXT PRIMARY KEY REFERENCES ai_usage_requests(request_id),
  input_usd_per_million REAL NOT NULL, output_usd_per_million REAL NOT NULL,
  monthly_limit_usd REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ai_usage_slots (
  request_id TEXT NOT NULL REFERENCES ai_usage_requests(request_id), call_index INTEGER NOT NULL,
  messages_hash TEXT NOT NULL, max_tokens INTEGER NOT NULL, PRIMARY KEY(request_id,call_index));
CREATE TABLE IF NOT EXISTS ai_usage_claims (
  request_id TEXT NOT NULL, call_index INTEGER NOT NULL, claimed_at TEXT NOT NULL,
  PRIMARY KEY(request_id,call_index), FOREIGN KEY(request_id,call_index)
  REFERENCES ai_usage_slots(request_id,call_index));
"""

LEDGER_TABLES = ("ai_usage_requests", "ai_usage_calls", "ai_usage_terms", "ai_usage_slots", "ai_usage_claims")
PRIVATE_FIELDS = {"history", "personal_history", "conversation_history", "sessions", "personal_sessions",
    "api_key", "apikey", "secret", "token", "password", "email", "phone", "address", "収支", "実戦履歴"}
SAFE_ERROR_KINDS = {"disabled", "timeout", "network", "authentication", "rate_limit", "provider_unavailable",
    "request", "invalid_response", "provider_error", "internal_error", "payload_changed"}


class AIGovernanceError(ValueError):
    pass


class DuplicateAIRequest(AIGovernanceError):
    pass


def blocked_message(plan: dict) -> str:
    labels = {"personal_data_detected": "個人情報らしい文字列を検出",
        "price_not_configured": "単価が未設定", "monthly_limit_not_configured": "月上限が未設定",
        "request_call_limit_exceeded": "1回の呼出上限を超過", "monthly_limit_exceeded": "月上限を超過"}
    return "外部AIを停止しました：" + "、".join(labels.get(r, r) for r in plan["blocked_reasons"])


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive(env: Mapping[str, str], key: str) -> float | None:
    try:
        value = float(env.get(key, ""))
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def monthly_limit(provider: str, environ: Mapping[str, str] | None = None) -> float | None:
    env = os.environ if environ is None else environ
    key = f"PACHI_AI_{provider.upper()}_MONTHLY_LIMIT_USD"
    # An explicitly invalid provider override must not silently enable a fallback.
    return _positive(env, key if key in env else "PACHI_AI_MONTHLY_LIMIT_USD")


def validate_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", request_id):
        raise AIGovernanceError("外部AIの実行IDが不正です")
    return request_id


def _max_calls(env) -> int:
    value = env.get("PACHI_AI_MAX_CALLS_PER_REQUEST", "40")
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise AIGovernanceError("1回の呼出上限の設定が不正です")
    return int(value)


def privacy_check(messages: list[dict]) -> dict:
    """Classify without returning or storing the matched text."""
    found = set()
    byte_count = 0
    def scan(value, key=""):
        if isinstance(value, dict):
            for field, item in value.items():
                normalized = re.sub(r"[- ]", "_", str(field)).lower()
                if normalized in PRIVATE_FIELDS or (normalized == "kind" and item == "user_input"):
                    found.add("private_field")
                scan(item, str(field))
        elif isinstance(value, list):
            for item in value:
                scan(item, key)
        elif isinstance(value, str):
            # Only exact generated identifiers are exempt; labels, URLs and other
            # snapshot strings are untrusted and receive the same checks as questions.
            if key == "snapshot_id" and re.fullmatch(r"[a-f0-9]{64}", value):
                return
            for name, pattern in SENSITIVE_PATTERNS:
                if pattern.search(value):
                    found.add(name)

    for message in messages:
        text = message.get("content", "")
        byte_count += len(text.encode("utf-8"))
        try:
            structured = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            structured = text
        scan(structured)
    return {"passed": not found, "blocked_categories": sorted(found),
            "message_count": len(messages), "payload_bytes": byte_count}


class AIGovernanceService:
    def __init__(self, db_path: Path = AI_USAGE_DB):
        self.db_path = Path(db_path)
        self._guard = RLock()

    def _connect(self, *, write=False):
        if write:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            for table in LEDGER_TABLES:
                for operation in ("UPDATE", "DELETE"):
                    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{operation} BEFORE {operation} ON {table} "
                                 "BEGIN SELECT RAISE(ABORT,'AI usage records are immutable'); END")
            conn.commit()
        else:
            conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def month_used(self, provider: str, at: datetime | None = None) -> float:
        if not self.db_path.exists():
            return 0.0
        current = at or datetime.now(timezone.utc)
        with closing(self._connect()) as conn:
            return self._month_used(conn, provider, current)

    def _month_used(self, conn, provider, at=None):
        prefix = (at or datetime.now(timezone.utc)).strftime("%Y-%m") + "%"
        value = conn.execute("SELECT COALESCE(SUM(MAX(reserved_usd,COALESCE((SELECT SUM(cost_usd) "
            "FROM ai_usage_calls c WHERE c.request_id=r.request_id),0))),0) FROM ai_usage_requests r "
            "WHERE provider=? AND created_at LIKE ?", (provider, prefix)).fetchone()[0]
        return round(float(value or 0), 8)

    def plan(self, *, provider: str, call_messages: list[list[dict]], max_tokens: int,
             environ: Mapping[str, str] | None = None) -> dict:
        env = os.environ if environ is None else environ
        return self._plan(provider, call_messages, max_tokens, env, self.month_used(provider))

    def _plan(self, provider, call_messages, max_tokens, env, used):
        if provider not in PROVIDERS or type(max_tokens) is not int or not 1 <= max_tokens <= 1_000_000:
            raise AIGovernanceError("外部AI名・最大出力数が不正です")
        if not isinstance(call_messages, list) or not call_messages:
            raise AIGovernanceError("送信予定の呼出内容が必要です")
        if any(not isinstance(messages, list) or not messages or any(
                not isinstance(message, dict) or set(message) != {"role", "content"}
                or message["role"] not in {"system", "user", "assistant"}
                or not isinstance(message["content"], str) for message in messages) for messages in call_messages):
            raise AIGovernanceError("外部AIの送信形式が不正です")
        price_in, price_out = comparison_unit_prices(provider, env)
        limit = monthly_limit(provider, env)
        checks = [privacy_check(messages) for messages in call_messages]
        # Conservative local estimate includes framing overhead. This is not a
        # guarantee of the provider's tokenizer, hidden tokens or final invoice.
        input_cap = sum(len(_json(messages).encode("utf-8")) + 256 + 64 * len(messages)
                        for messages in call_messages)
        output_cap = max_tokens * len(call_messages)
        reserve = None if None in (price_in, price_out) else round(
            (input_cap * price_in + output_cap * price_out) / 1_000_000, 8)
        if reserve is not None and not math.isfinite(reserve):
            raise AIGovernanceError("料金の予約額を計算できません")
        max_calls = _max_calls(env)
        reasons = []
        if not all(item["passed"] for item in checks): reasons.append("personal_data_detected")
        if price_in is None or price_out is None: reasons.append("price_not_configured")
        if limit is None: reasons.append("monthly_limit_not_configured")
        if len(call_messages) > max_calls: reasons.append("request_call_limit_exceeded")
        if reserve is not None and limit is not None and used + reserve > limit: reasons.append("monthly_limit_exceeded")
        return {"provider": provider, "call_count": len(call_messages), "privacy_passed": not any(
            "personal_data_detected" == reason for reason in reasons),
            "blocked_categories": sorted({c for item in checks for c in item["blocked_categories"]}),
            "unit_prices_configured": price_in is not None and price_out is not None,
            "unit_prices_usd_per_million": [price_in, price_out],
            "monthly_limit_usd": limit, "month_reserved_usd": used,
            "request_reserve_usd": reserve, "remaining_after_reserve_usd":
                round(limit - used - reserve, 8) if limit is not None and reserve is not None else None,
            "ready": not reasons, "blocked_reasons": reasons}

    def authorize(self, *, request_id: str, provider: str, model: str, purpose: str,
                  call_messages: list[list[dict]], max_tokens: int, confirmed: bool,
                  environ: Mapping[str, str] | None = None) -> dict:
        return self.authorize_many(requests=[dict(request_id=request_id, provider=provider, model=model,
            purpose=purpose, call_messages=call_messages, max_tokens=max_tokens, confirmed=confirmed)],
            environ=environ)[0]

    def authorize_many(self, *, requests: list[dict], environ: Mapping[str, str] | None = None) -> list[dict]:
        """Reserve every provider or none, serialized across processes by SQLite."""
        env = dict(os.environ if environ is None else environ)
        frozen = deepcopy(requests)
        if not frozen:
            return []
        for item in frozen:
            validate_request_id(item["request_id"])
            if item["confirmed"] is not True:
                raise AIGovernanceError("外部送信する内容と料金上限の確認が必要です")
            if not isinstance(item["model"], str) or not item["model"].strip() or not isinstance(item["purpose"], str):
                raise AIGovernanceError("外部AIのモデル・用途が不正です")
            plan = self.plan(provider=item["provider"], call_messages=item["call_messages"],
                max_tokens=item["max_tokens"], environ=env)
            if not plan["ready"]:
                raise AIGovernanceError(f'{PROVIDERS[item["provider"]].label}：{blocked_message(plan)}')
        if sum(len(item["call_messages"]) for item in frozen) > _max_calls(env):
            raise AIGovernanceError("外部AIを停止しました：1回の呼出上限を超過")
        plans = []
        with self._guard, closing(self._connect(write=True)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            for item in frozen:
                request_id, provider = item["request_id"], item["provider"]
                if conn.execute("SELECT 1 FROM ai_usage_requests WHERE request_id=?", (request_id,)).fetchone():
                    raise DuplicateAIRequest("同じ実行IDはすでに受け付け済みです。重複課金防止のため再送しません")
                plan = self._plan(provider, item["call_messages"], item["max_tokens"], env, self._month_used(conn, provider))
                if not plan["ready"]:
                    raise AIGovernanceError(blocked_message(plan))
                identity = {key: item[key] for key in ("provider", "model", "purpose", "max_tokens")}
                identity["payload_hashes"] = [_hash(messages) for messages in item["call_messages"]]
                payload_hash = _hash(identity)
                conn.execute("INSERT INTO ai_usage_requests VALUES(?,?,?,?,?,?,?,?)", (
                    request_id, provider, item["model"], item["purpose"], payload_hash,
                    len(item["call_messages"]), plan["request_reserve_usd"], _now()))
                conn.execute("INSERT INTO ai_usage_terms VALUES(?,?,?,?)", (request_id,
                    *plan["unit_prices_usd_per_million"], plan["monthly_limit_usd"]))
                conn.executemany("INSERT INTO ai_usage_slots VALUES(?,?,?,?)", [(request_id, index,
                    value, item["max_tokens"]) for index, value in enumerate(identity["payload_hashes"], 1)])
                plans.append({**plan, "request_id": request_id, "payload_hash": payload_hash})
        return plans

    def claim_call(self, *, request_id: str, call_index: int, messages: list[dict], max_tokens: int):
        """Durably consume a slot BEFORE any transport; unknown outcomes never retry."""
        if type(call_index) is not int or call_index < 1 or type(max_tokens) is not int:
            raise AIGovernanceError("外部AIの呼出番号・出力上限が不正です")
        with self._guard, closing(self._connect(write=True)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            slot = conn.execute("SELECT messages_hash,max_tokens FROM ai_usage_slots WHERE request_id=? AND call_index=?",
                (request_id, call_index)).fetchone()
            if slot is None:
                raise AIGovernanceError("利用許可記録がありません")
            if slot["messages_hash"] != _hash(messages) or slot["max_tokens"] != max_tokens:
                raise AIGovernanceError("確認後に送信内容が変わったため外部AIを停止しました")
            if conn.execute("SELECT 1 FROM ai_usage_claims WHERE request_id=? AND call_index=? UNION ALL "
                    "SELECT 1 FROM ai_usage_calls WHERE request_id=? AND call_index=?",
                    (request_id, call_index, request_id, call_index)).fetchone():
                raise DuplicateAIRequest("同じ呼出はすでに受け付け済みです。重複課金防止のため再送しません")
            conn.execute("INSERT INTO ai_usage_claims VALUES(?,?,?)", (request_id, call_index, _now()))

    def complete_call(self, client, *, request_id: str, call_index: int, messages: list[dict],
                      max_tokens: int, environ=None) -> AICompletion:
        frozen = deepcopy(messages)
        self.claim_call(request_id=request_id, call_index=call_index, messages=frozen, max_tokens=max_tokens)
        try:
            completion = (client.complete_detailed(frozen, max_tokens=max_tokens) if hasattr(client, "complete_detailed")
                          else AICompletion(client.complete(frozen, max_tokens=max_tokens)))
        except Exception as exc:
            self.record_call(request_id=request_id, call_index=call_index,
                error_kind=getattr(exc, "kind", "provider_error"))
            raise
        # A ledger failure is not a provider failure and must not cause a second insert.
        self.record_call(request_id=request_id, call_index=call_index, completion=completion)
        return completion

    def record_call(self, *, request_id: str, call_index: int, completion: AICompletion | None = None,
                    error_kind: str | None = None, environ: Mapping[str, str] | None = None):
        if type(call_index) is not int or call_index < 1:
            raise AIGovernanceError("外部AIの呼出番号が不正です")
        with self._guard, closing(self._connect(write=True)) as conn, conn:
            request = conn.execute("SELECT t.* FROM ai_usage_terms t JOIN ai_usage_slots s USING(request_id) "
                "WHERE t.request_id=? AND s.call_index=?", (request_id, call_index)).fetchone()
            if request is None: raise AIGovernanceError("利用許可記録がありません")
            inp, out = (value if type(value) is int and 0 <= value <= 2**63 - 1 else None
                for value in (getattr(completion, "input_tokens", None), getattr(completion, "output_tokens", None)))
            prices = request["input_usd_per_million"], request["output_usd_per_million"]
            cost = None
            if inp is not None and out is not None:
                amount = (inp * prices[0] + out * prices[1]) / 1_000_000
                cost = round(amount, 8) if math.isfinite(amount) else None
            if error_kind is not None and error_kind not in SAFE_ERROR_KINDS:
                error_kind = "provider_error"
            conn.execute("INSERT INTO ai_usage_calls VALUES(?,?,?,?,?,?,?,?)", (
                request_id, call_index, "failed" if error_kind else "completed", inp, out, cost,
                error_kind, _now()))

    def status(self, environ: Mapping[str, str] | None = None) -> dict:
        env = os.environ if environ is None else environ
        from api.ai_provider import PROVIDERS
        rows = []
        for name, definition in PROVIDERS.items():
            prices = comparison_unit_prices(name, env)
            limit = monthly_limit(name, env)
            used = self.month_used(name)
            rows.append({"provider": name, "label": definition.label,
                "price_configured": None not in prices, "monthly_limit_usd": limit,
                "month_reserved_usd": used,
                "remaining_usd": round(limit - used, 8) if limit is not None else None,
                "ready_for_paid_calls": None not in prices and limit is not None and used < limit})
        return {"protocol": PROTOCOL, "providers": rows, "stores_prompts": False,
            "stores_answers": False, "stores_api_keys": False, "personal_history_allowed": False,
            "duplicate_charge_guard": True,
            "reservation_is_estimate": True, "unknown_usage_keeps_reservation": True,
            "notice": "外部AIは毎回確認が必要です。単価・月上限が不明な場合は送信しません。"}


class GovernedClient:
    """One-authorized-call adapter for the evidence answer contract."""
    def __init__(self, client, governance: AIGovernanceService, request_id: str,
                 environ: Mapping[str, str] | None = None, expected_messages: list[dict] | None = None):
        self.client, self.governance, self.request_id, self.environ = client, governance, request_id, environ
        self.display_name = client.display_name
        self.expected_messages_hash = _hash(expected_messages) if expected_messages is not None else None

    def complete(self, messages, *, max_tokens):
        if self.expected_messages_hash is not None and _hash(messages) != self.expected_messages_hash:
            raise AIGovernanceError("確認後に送信内容が変わったため外部AIを停止しました")
        return self.governance.complete_call(self.client, request_id=self.request_id,
            call_index=1, messages=messages, max_tokens=max_tokens, environ=self.environ).content


governance = AIGovernanceService()
