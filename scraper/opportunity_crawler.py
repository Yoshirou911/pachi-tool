"""期待値表の低頻度クローラーと承認キュー。

公開ページの利用規約・robots.txtを尊重し、登録済みURLだけを低頻度で確認する。
取得値は本番プロフィールへ直結させず SQLite の staging に保存し、明示承認後に
のみ反映する。Supabase は環境変数が設定されている場合だけサーバー側から UPSERT。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import date, datetime, timezone
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from config import OPPORTUNITIES_DB
from opportunity.models import get_profile, list_profiles


USER_AGENT = "PachiToolResearchBot/2.4 (+local personal research; low frequency)"
EXTRACTOR_VERSION = 2
ALLOWED_HOSTS = {
    "nana-press.com",
    "www.nana-press.com",
    "www.sanyobussan.co.jp",
    "sanyobussan.co.jp",
    "www.sankyo-fever.jp",
    "sankyo-fever.jp",
    "altema.jp", "www.altema.jp",
    "slobase.jp", "www.slobase.jp",
    "slotmethod.jp", "www.slotmethod.jp",
    "slot-toolbox.com", "www.slot-toolbox.com",
    "slot-diary.com", "www.slot-diary.com",
    "rakupachi.com", "www.rakupachi.com",
    "p-gabu.jp", "www.p-gabu.jp",
}
MIN_HOST_INTERVAL_SECONDS = 2.0
_last_request: dict[str, float] = {}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunity_crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    checked_count INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS opportunity_crawl_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES opportunity_crawl_runs(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL,
    catalog_key TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_title TEXT NOT NULL DEFAULT '',
    source_updated_on TEXT,
    content_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    extracted_json TEXT NOT NULL DEFAULT '{}',
    conflict_json TEXT NOT NULL DEFAULT '[]',
    reviewed_at TEXT,
    review_note TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_opp_crawl_content
ON opportunity_crawl_candidates(profile_id,source_url,content_hash);
CREATE INDEX IF NOT EXISTS idx_opp_crawl_status
ON opportunity_crawl_candidates(status,fetched_at);
"""


def _connect() -> sqlite3.Connection:
    OPPORTUNITIES_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(OPPORTUNITIES_DB))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(_SCHEMA)
    return con


def _allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS


def _robots_allows(url: str, session: requests.Session) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    robot = RobotFileParser()
    robot.set_url(robots_url)
    try:
        response = session.get(robots_url, timeout=12)
        # RFC 9309: robots.txt 自体の 4xx（429を除く）は unavailable であり、
        # リソース取得を許容できる。5xx/通信不能は安全側に停止する。
        if 400 <= response.status_code < 500:
            return response.status_code != 429
        if response.status_code >= 500:
            return False
        robot.parse(response.text.splitlines())
        return robot.can_fetch(USER_AGENT, url)
    except requests.RequestException:
        return False


def _fetch(url: str, session: requests.Session) -> str:
    if not _allowed(url):
        raise ValueError("許可リスト外のURLです")
    if not _robots_allows(url, session):
        raise PermissionError("robots.txtで許可を確認できません")
    host = urlparse(url).hostname or ""
    wait = MIN_HOST_INTERVAL_SECONDS - (time.monotonic() - _last_request.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    response = session.get(url, timeout=25)
    _last_request[host] = time.monotonic()
    response.raise_for_status()
    if "text/html" not in response.headers.get("content-type", ""):
        raise ValueError("HTMLページではありません")
    if len(response.content) > 4_000_000:
        raise ValueError("ページサイズが上限を超えました")
    return response.text


def _number(text: str):
    cleaned = re.sub(r"[^0-9+\-]", "", text.replace(",", ""))
    if cleaned in {"", "+", "-"}:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _extract_nana_table_groups(soup: BeautifulSoup) -> list[dict]:
    """期待値表を見出し単位で分離する（通常/リセット表の混同防止）。"""
    groups: list[dict] = []
    for table in soup.select("table"):
        rows = []
        for tr in table.select("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.select("th,td")]
            if cells:
                rows.append(cells)
        if len(rows) < 2:
            continue
        header = " ".join(rows[0])
        if "打ち出し" not in header or "等価" not in header:
            continue
        points: list[dict] = []
        for cells in rows[1:]:
            if len(cells) < 2:
                continue
            value = _number(cells[0])
            equivalent = _number(cells[1])
            exchange_56 = _number(cells[2]) if len(cells) >= 3 else None
            if value is None or equivalent is None or value < 0 or value > 10000:
                continue
            points.append({"value": value, "equivalent_ev_yen": equivalent, "exchange_56_ev_yen": exchange_56})
        if points:
            heading = table.find_previous(["h2", "h3", "h4"])
            context = heading.get_text(" ", strip=True) if heading else ""
            unique = {item["value"]: item for item in points}
            groups.append({"context": context, "points": [unique[key] for key in sorted(unique)]})
    return groups


def _extract_nana_tables(soup: BeautifulSoup) -> list[dict]:
    """後方互換: 単一表ならその点列、複数表は重複値を混ぜず全点を返す。"""
    groups = _extract_nana_table_groups(soup)
    if len(groups) == 1:
        return groups[0]["points"]
    return [point for group in groups for point in group["points"]]


def _extract_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = soup.get_text(" ", strip=True)
    updated = None
    match = re.search(r"最終更新日\s*[:：]?\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if match:
        updated = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return {
        "title": title[:300],
        "source_updated_on": updated,
        "curve_tables": _extract_nana_table_groups(soup),
        "normalized_text": re.sub(r"\s+", " ", text)[:250_000],
    }


def _compare(profile: dict, extracted: dict) -> tuple[dict, list[str]]:
    exchange = str(profile.get("exchange_type") or "equivalent")
    value_key = "exchange_56_ev_yen" if exchange == "56" else "equivalent_ev_yen"
    tables = extracted.get("curve_tables") or []
    if not tables and extracted.get("curve_points"):
        tables = [{"context": "", "points": extracted.get("curve_points") or []}]
    selected_table = None
    if len(tables) == 1:
        selected_table = tables[0]
    elif tables:
        reset_status = str(profile.get("reset_status") or "")
        preferred = ("短縮あり", "設定変更", "リセット") if reset_status == "reset_confirmed" else ("短縮なし", "通常条件", "通常時")
        ranked = sorted(tables, key=lambda table: sum(word in str(table.get("context") or "") for word in preferred), reverse=True)
        if sum(word in str(ranked[0].get("context") or "") for word in preferred) > 0:
            selected_table = ranked[0]
    points = []
    for row in (selected_table or {}).get("points", []):
        ev = row.get(value_key)
        if ev is not None:
            points.append({"value": row["value"], "ev_yen": ev})
    by_value = {float(item["value"]): item for item in points}
    start = float(profile.get("start_threshold") or 0)
    start_point = by_value.get(start)
    conflicts: list[str] = []
    if len(tables) > 1 and selected_table is None:
        conflicts.append("同一ページに複数条件の期待値表があり、自動で条件を特定できません")
    if points and start_point is None:
        conflicts.append("登録開始ラインが公開表に見つかりません")
    if start_point and profile.get("expected_value_yen") is not None:
        if int(start_point["ev_yen"]) != int(profile["expected_value_yen"]):
            conflicts.append(
                f"開始ライン期待値が変更: DB {int(profile['expected_value_yen']):,}円 / 公開表 {int(start_point['ev_yen']):,}円"
            )
    return {
        "machine_name": profile.get("machine_name"),
        "condition_label": profile.get("condition_label"),
        "exchange_type": exchange,
        "start_threshold": start,
        "expected_value_yen": start_point.get("ev_yen") if start_point else None,
        "curve_points": points,
        "source_updated_on": extracted.get("source_updated_on"),
    }, conflicts


def run_crawl(max_profiles: int = 100) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as con:
        run_id = con.execute(
            "INSERT INTO opportunity_crawl_runs(started_at) VALUES(?)", (now,)
        ).lastrowid
        con.commit()
    checked = candidates = errors = 0
    messages: list[str] = []
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.5"})

    for profile in list_profiles()[:max_profiles]:
        urls = list(dict.fromkeys([profile.get("source_url")] + list(profile.get("source_urls") or [])))
        urls = [item for item in urls if item and _allowed(item)]
        if not urls:
            continue
        profile_succeeded = False
        profile_errors: list[str] = []
        for url in urls:
            checked += 1
            try:
                html = _fetch(url, session)
                digest = hashlib.sha256(
                    f"extractor:{EXTRACTOR_VERSION}\n".encode() + html.encode("utf-8", errors="ignore")
                ).hexdigest()
                page = _extract_page(html, url)
                extracted, conflicts = _compare(profile, page)
                status = "conflict" if conflicts else "pending"
                with _connect() as con:
                    before = con.total_changes
                    con.execute(
                        """INSERT OR IGNORE INTO opportunity_crawl_candidates
                           (run_id,profile_id,catalog_key,source_url,source_title,source_updated_on,
                            content_hash,fetched_at,status,extracted_json,conflict_json)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (run_id, profile["id"], profile.get("catalog_key") or "", url, page["title"],
                         page.get("source_updated_on"), digest, now, status,
                         json.dumps(extracted, ensure_ascii=False), json.dumps(conflicts, ensure_ascii=False)),
                    )
                    candidates += con.total_changes - before
                profile_succeeded = True
            except Exception as exc:  # 個別URLの失敗で全体を止めない
                profile_errors.append(f"{urlparse(url).netloc}: {exc}")
        if not profile_succeeded:
            errors += 1
            messages.append(f"{profile.get('catalog_key')}: {' / '.join(profile_errors)}")

    final_status = "completed" if errors == 0 else "completed_with_errors"
    with _connect() as con:
        con.execute(
            """UPDATE opportunity_crawl_runs SET finished_at=?,status=?,checked_count=?,
               candidate_count=?,error_count=?,message=? WHERE id=?""",
            (datetime.now(timezone.utc).isoformat(), final_status, checked, candidates, errors,
             "\n".join(messages[:20]), run_id),
        )
    return {"run_id": run_id, "status": final_status, "checked": checked, "candidates": candidates,
            "errors": errors, "messages": messages[:20]}


def crawler_status() -> dict:
    with _connect() as con:
        last = con.execute("SELECT * FROM opportunity_crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
        counts = {row["status"]: row["count"] for row in con.execute(
            "SELECT status,COUNT(*) AS count FROM opportunity_crawl_candidates GROUP BY status"
        ).fetchall()}
    return {"last_run": dict(last) if last else None, "candidate_counts": counts,
            "supabase_configured": bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))}


def list_candidates(status: str | None = None, limit: int = 100) -> list[dict]:
    query = "SELECT * FROM opportunity_crawl_candidates"
    params: list = []
    if status:
        query += " WHERE status=?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with _connect() as con:
        rows = con.execute(query, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["extracted"] = json.loads(item.pop("extracted_json") or "{}")
        item["conflicts"] = json.loads(item.pop("conflict_json") or "[]")
        result.append(item)
    return result


def _supabase_upsert(profile: dict) -> dict:
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    table = os.getenv("SUPABASE_OPPORTUNITY_TABLE", "opportunity_profiles")
    if not base or not key:
        return {"configured": False, "synced": False}
    payload = {key_name: profile.get(key_name) for key_name in (
        "catalog_key", "machine_name", "condition_label", "exchange_type", "funding_mode",
        "reset_status", "metric_name", "unit_label", "start_threshold", "ceiling_threshold",
        "expected_value_yen", "estimated_play_minutes", "safe_play_minutes", "verified_on", "confidence",
    )}
    payload["curve_points"] = profile.get("curve_points") or []
    response = requests.post(
        f"{base}/rest/v1/{table}?on_conflict=catalog_key",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=[payload], timeout=20,
    )
    response.raise_for_status()
    return {"configured": True, "synced": True, "status_code": response.status_code}


def approve_candidate(candidate_id: int, note: str = "") -> dict:
    with _connect() as con:
        row = con.execute("SELECT * FROM opportunity_crawl_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise KeyError(candidate_id)
        if row["status"] in {"approved", "rejected"}:
            raise ValueError("レビュー済みです")
        extracted = json.loads(row["extracted_json"] or "{}")
        profile = get_profile(int(row["profile_id"]))
        if not profile:
            raise KeyError(row["profile_id"])
        curve = extracted.get("curve_points") or []
        # 公開表に取れた数値だけ更新。必要資金/時間などローカル検証値は保持する。
        existing_curve = {float(item["value"]): dict(item) for item in profile.get("curve_points") or []}
        for item in curve:
            key = float(item["value"])
            existing_curve[key] = {**existing_curve.get(key, {}), **item}
        expected = extracted.get("expected_value_yen")
        con.execute(
            """UPDATE opportunity_profiles SET expected_value_yen=COALESCE(?,expected_value_yen),
               curve_json=?,verified_on=?,crawler_approved_at=?,crawler_source_hash=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (expected, json.dumps([existing_curve[key] for key in sorted(existing_curve)], ensure_ascii=False),
             date.today().isoformat(), datetime.now(timezone.utc).isoformat(), row["content_hash"], profile["id"]),
        )
        con.execute(
            "UPDATE opportunity_crawl_candidates SET status='approved',reviewed_at=?,review_note=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), note[:500], candidate_id),
        )
    updated = get_profile(int(row["profile_id"])) or profile
    return {"ok": True, "profile": updated, "supabase": _supabase_upsert(updated)}


def reject_candidate(candidate_id: int, note: str = "") -> dict:
    with _connect() as con:
        changed = con.execute(
            """UPDATE opportunity_crawl_candidates SET status='rejected',reviewed_at=?,review_note=?
               WHERE id=? AND status NOT IN ('approved','rejected')""",
            (datetime.now(timezone.utc).isoformat(), note[:500], candidate_id),
        ).rowcount
    if not changed:
        raise KeyError(candidate_id)
    return {"ok": True}
