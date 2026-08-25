"""anoslot.moe の公開店舗ページからスマスロ・ジャグラー日次集計を保存する。"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from hall.machine_scope import is_supported_analysis_machine

try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"


BASE_URL = "https://anoslot.moe"
REQUEST_DELAY = 2.0
HEADERS = {
    "User-Agent": "PACHI-TOOL/2.2 public-data-collector",
    "Accept-Language": "ja,en;q=0.8",
}

# サイト自身の公開検索APIで確認した店舗IDだけを登録する。
HALL_IDS = {
    "ひま・わり四條畷店": 8113,
    "キコーナ四條畷店": 7964,
    "キコーナ大東店": 7953,
    "キコーナ野崎店": 7963,
    "ニコニコ住道店": 7961,
    "ベガスベガス大東店": 7954,
    "マルハン大東店": 7950,
}


_MACHINE_PREFIXES = (
    "\u30b9\u30de\u30b9\u30ed",       # smart-slot label
    "l\u30d1\u30c1\u30b9\u30ed",    # L + pachislot label
    "\u30d1\u30c1\u30b9\u30ed",     # pachislot label
    "l",
)


def normalize_machine_name(machine_name: str) -> str:
    """Return a conservative key used only to prevent cross-source aliases."""
    name = unicodedata.normalize("NFKC", machine_name).lower().replace(" ", "").replace("\u3000", "")
    for prefix in _MACHINE_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return "".join(character for character in name if character.isalnum())


def init_db(path: Path | None = None) -> sqlite3.Connection:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hall_source_machine_daily (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            source           TEXT NOT NULL,
            hall_name        TEXT NOT NULL,
            report_date      TEXT NOT NULL,
            machine_name     TEXT NOT NULL,
            machine_id       TEXT,
            unit_count       INTEGER,
            total_diff_coins INTEGER,
            avg_diff_coins   INTEGER,
            avg_games        INTEGER,
            win_rate_pct     REAL,
            source_url       TEXT NOT NULL,
            scraped_at       TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(source, hall_name, report_date, machine_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hall_day_machine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name TEXT NOT NULL,
            report_date TEXT NOT NULL,
            machine_name TEXT NOT NULL,
            unit_count INTEGER,
            avg_diff_coins INTEGER,
            avg_games INTEGER,
            win_rate_pct REAL,
            ev_pct REAL,
            source_url TEXT,
            scraped_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(hall_name, report_date, machine_name)
        )
        """
    )
    conn.commit()
    return conn


def is_refresh_due(path: Path | None = None, max_age_hours: int = 6) -> bool:
    """Throttle startup refreshes while still allowing a daily collector run."""
    conn = init_db(path)
    try:
        row = conn.execute(
            "SELECT MAX(scraped_at) FROM hall_source_machine_daily WHERE source='anoslot'"
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return True
    try:
        last_scraped = datetime.fromisoformat(row[0])
    except ValueError:
        return True
    return last_scraped < datetime.now() - timedelta(hours=max_age_hours)


def _decode_next_flight(html: str) -> str:
    chunks: list[str] = []
    prefix = "self.__next_f.push("
    for script in BeautifulSoup(html, "lxml").find_all("script"):
        raw = script.string or ""
        if not raw.startswith(prefix) or not raw.endswith(")"):
            continue
        try:
            value = json.loads(raw[len(prefix):-1])
        except json.JSONDecodeError:
            continue
        if len(value) > 1 and isinstance(value[1], str):
            chunks.append(value[1])
    return "".join(chunks)


def parse_store_page(html: str, expected_hall_name: str, source_url: str) -> list[dict]:
    """Next.js が公開HTMLへ埋め込んだ機種別日次集計だけを読む。"""
    text = _decode_next_flight(html)
    if not text:
        return []
    published = re.search(r'"storeName":"([^"]+)"', text)
    if not published or published.group(1) != expected_hall_name:
        return []

    decoder = json.JSONDecoder()
    found: dict[tuple[str, str], dict] = {}
    # The public page has used both names for this object across revisions.
    for match in re.finditer(r'\{(?:"name"|"machineName"):', text):
        try:
            machine, _ = decoder.raw_decode(text, match.start())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(machine, dict) or not isinstance(machine.get("dailyData"), list):
            continue
        machine_name = str(machine.get("machineName") or machine.get("name") or "").strip()
        if not is_supported_analysis_machine(machine_name):
            continue
        machine_id = str(machine.get("machineId") or "")
        for daily in machine["dailyData"]:
            if not isinstance(daily, dict):
                continue
            report_date = str(daily.get("date") or "")[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
                continue
            active_units = int(daily.get("activeUnits") or daily.get("units") or 0)
            total_diff = daily.get("diff")
            if active_units <= 0 or not isinstance(total_diff, (int, float)):
                continue
            positive_units = int(daily.get("positiveUnits") or 0)
            row = {
                "source": "anoslot",
                "hall_name": expected_hall_name,
                "report_date": report_date,
                "machine_name": machine_name,
                "machine_id": machine_id,
                "unit_count": active_units,
                "total_diff_coins": round(total_diff),
                "avg_diff_coins": round(total_diff / active_units),
                "avg_games": round(float(daily.get("avgG") or 0)),
                "win_rate_pct": round(positive_units / active_units * 100, 1),
                "source_url": source_url,
            }
            found[(report_date, machine_name)] = row
    rows = list(found.values())
    # Some stores publish play counts but mask every payout field as zero.
    # Treating that sentinel as real performance would create a false 0-coin
    # trend, so reject only the clearly impossible hall-wide pattern.
    if (
        len(rows) >= 20
        and all(row["total_diff_coins"] == 0 for row in rows)
        and all(row["win_rate_pct"] == 0 for row in rows)
    ):
        return []
    return rows


def purge_masked_zero_rows(conn: sqlite3.Connection, hall_name: str) -> int:
    """Remove previously saved rows when a hall masks every result as zero."""
    summary = conn.execute(
        """SELECT COUNT(*), MAX(ABS(total_diff_coins)), MAX(win_rate_pct)
           FROM hall_source_machine_daily
           WHERE source='anoslot' AND hall_name=?""",
        (hall_name,),
    ).fetchone()
    if not summary or summary[0] < 20 or summary[1] != 0 or summary[2] != 0:
        return 0
    count = int(summary[0])
    conn.execute(
        """DELETE FROM hall_day_machine
           WHERE hall_name=? AND source_url LIKE 'https://anoslot.moe/stores/%'""",
        (hall_name,),
    )
    conn.execute(
        """DELETE FROM hall_source_machine_daily
           WHERE source='anoslot' AND hall_name=?""",
        (hall_name,),
    )
    conn.commit()
    return count


def save_rows(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    """専用テーブルへ保存し、既存実績が空の行だけ主分析表へ補完する。"""
    source_saved = 0
    analysis_saved = 0
    for row in rows:
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR REPLACE INTO hall_source_machine_daily
              (source,hall_name,report_date,machine_name,machine_id,unit_count,
               total_diff_coins,avg_diff_coins,avg_games,win_rate_pct,source_url,scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["source"], row["hall_name"], row["report_date"], row["machine_name"],
                row["machine_id"], row["unit_count"], row["total_diff_coins"],
                row["avg_diff_coins"], row["avg_games"], row["win_rate_pct"],
                row["source_url"], datetime.now().isoformat(timespec="seconds"),
            ),
        )
        source_saved += conn.total_changes - before

        existing = conn.execute(
            """SELECT avg_diff_coins FROM hall_day_machine
               WHERE hall_name=? AND report_date=? AND machine_name=?""",
            (row["hall_name"], row["report_date"], row["machine_name"]),
        ).fetchone()
        if existing is not None and existing[0] is not None:
            continue
        normalized_name = normalize_machine_name(row["machine_name"])
        aliases = conn.execute(
            """SELECT machine_name FROM hall_day_machine
               WHERE hall_name=? AND report_date=? AND avg_diff_coins IS NOT NULL
                 AND (source_url IS NULL OR source_url NOT LIKE 'https://anoslot.moe/stores/%')""",
            (row["hall_name"], row["report_date"]),
        ).fetchall()
        if any(normalize_machine_name(alias[0]) == normalized_name for alias in aliases):
            continue
        before = conn.total_changes
        conn.execute(
            """
            INSERT INTO hall_day_machine
              (hall_name,report_date,machine_name,unit_count,avg_diff_coins,
               avg_games,win_rate_pct,ev_pct,source_url)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(hall_name,report_date,machine_name) DO UPDATE SET
              unit_count=excluded.unit_count,
              avg_diff_coins=excluded.avg_diff_coins,
              avg_games=excluded.avg_games,
              win_rate_pct=excluded.win_rate_pct,
              source_url=excluded.source_url
            WHERE hall_day_machine.avg_diff_coins IS NULL
            """,
            (
                row["hall_name"], row["report_date"], row["machine_name"],
                row["unit_count"], row["avg_diff_coins"], row["avg_games"],
                row["win_rate_pct"], None, row["source_url"],
            ),
        )
        analysis_saved += conn.total_changes - before
    conn.commit()
    return source_saved, analysis_saved


def purge_cross_source_aliases(conn: sqlite3.Connection, hall_name: str | None = None) -> int:
    """Remove alternate-source analysis rows duplicated under another label."""
    parameters: tuple[str, ...] = ()
    hall_filter = ""
    if hall_name is not None:
        hall_filter = " AND hall_name=?"
        parameters = (hall_name,)
    external_rows = conn.execute(
        """SELECT id,hall_name,report_date,machine_name FROM hall_day_machine
           WHERE source_url LIKE 'https://anoslot.moe/stores/%'""" + hall_filter,
        parameters,
    ).fetchall()
    duplicate_ids: list[int] = []
    for row_id, row_hall, report_date, machine_name in external_rows:
        candidates = conn.execute(
            """SELECT machine_name FROM hall_day_machine
               WHERE hall_name=? AND report_date=? AND avg_diff_coins IS NOT NULL
                 AND (source_url IS NULL OR source_url NOT LIKE 'https://anoslot.moe/stores/%')""",
            (row_hall, report_date),
        ).fetchall()
        normalized = normalize_machine_name(machine_name)
        if any(normalize_machine_name(candidate[0]) == normalized for candidate in candidates):
            duplicate_ids.append(row_id)
    if duplicate_ids:
        conn.executemany("DELETE FROM hall_day_machine WHERE id=?", [(row_id,) for row_id in duplicate_ids])
        conn.commit()
    return len(duplicate_ids)


def scrape_hall(hall_name: str, store_id: int | None = None) -> dict:
    target_id = store_id or HALL_IDS.get(hall_name)
    if not target_id:
        return {"hall_name": hall_name, "status": "not_configured", "rows": 0}
    url = f"{BASE_URL}/stores/{target_id}"
    response = requests.get(url, headers=HEADERS, timeout=35)
    response.raise_for_status()
    rows = parse_store_page(response.text, hall_name, url)
    conn = init_db()
    try:
        source_saved, analysis_saved = save_rows(conn, rows)
        purged_masked_rows = purge_masked_zero_rows(conn, hall_name)
        purged_alias_rows = purge_cross_source_aliases(conn, hall_name)
    finally:
        conn.close()
    return {
        "hall_name": hall_name,
        "status": "masked_not_disclosed" if purged_masked_rows else ("ok" if rows else "no_public_data"),
        "rows": len(rows),
        "source_saved": source_saved,
        "analysis_saved": analysis_saved,
        "purged_masked_rows": purged_masked_rows,
        "purged_alias_rows": purged_alias_rows,
        "source_url": url,
    }


def scrape_all() -> list[dict]:
    results = []
    for index, (hall_name, store_id) in enumerate(HALL_IDS.items()):
        if index:
            time.sleep(REQUEST_DELAY)
        try:
            results.append(scrape_hall(hall_name, store_id))
        except Exception as exc:
            results.append({"hall_name": hall_name, "status": "error", "rows": 0, "error": str(exc)})
    return results


if __name__ == "__main__":
    print(json.dumps(scrape_all(), ensure_ascii=False, indent=2))
