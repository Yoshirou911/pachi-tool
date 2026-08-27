"""DMMぱちタウンの公開店舗ページから設置台数とフロアマップを日次保存する。"""
from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

from hall.machine_scope import is_supported_analysis_machine
from scraper.http_support import curl_ca_bundle

try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"


# 検索で確認した実在ページだけを登録する。店舗IDを推測して追加しない。
HALL_URLS = {
    "キコーナ四條畷店": "https://p-town.dmm.com/shops/osaka/7964",
}


def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hall_machine_snapshot (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name     TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            machine_name  TEXT NOT NULL,
            machine_id    TEXT,
            source_url    TEXT NOT NULL,
            scraped_at    TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(hall_name, snapshot_date, machine_name)
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hall_machine_snapshot)")}
    if "unit_count" not in columns:
        conn.execute("ALTER TABLE hall_machine_snapshot ADD COLUMN unit_count INTEGER")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hall_floor_map_snapshot (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name     TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            floor_index   INTEGER NOT NULL,
            image_url     TEXT NOT NULL,
            page_url      TEXT NOT NULL,
            scraped_at    TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(hall_name, snapshot_date, floor_index)
        )
        """
    )
    conn.commit()
    return conn


def parse_shop_page(html: str) -> dict:
    """20円スロットの分析対象機種と、公式掲載フロアマップURLを抽出する。"""
    soup = BeautifulSoup(html, "lxml")
    machines: dict[str, dict] = {}
    for unit in soup.select("li.unit"):
        title = unit.select_one("h4.title")
        title_text = title.get_text(" ", strip=True) if title else ""
        if "スロ" not in title_text or "[20]" not in title_text:
            continue
        for item in unit.select("li.item"):
            anchor = item.select_one('a[href*="/machines/"]')
            if not anchor:
                continue
            name = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
            if not name or not is_supported_analysis_machine(name):
                continue
            id_match = re.search(r"/machines/(\d+)", anchor.get("href", ""))
            number = item.select_one(".number")
            count_match = re.search(r"(\d+)\s*台", number.get_text(" ", strip=True) if number else "")
            machines[name] = {
                "machine_name": name,
                "machine_id": id_match.group(1) if id_match else None,
                "unit_count": int(count_match.group(1)) if count_match else None,
            }

    floor_maps = []
    for anchor in soup.select('a[href*="shop_floor_maps"]'):
        url = anchor.get("href", "").strip()
        if url.startswith("https://") and url not in floor_maps:
            floor_maps.append(url)
    return {"machines": list(machines.values()), "floor_maps": floor_maps}


def fetch_page(url: str) -> str:
    try:
        from curl_cffi import requests as cf_requests
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("curl-cffi が必要です") from exc
    response = cf_requests.get(
        url,
        impersonate="chrome120",
        timeout=25,
        verify=curl_ca_bundle(),
    )
    response.raise_for_status()
    return response.text


def scrape_snapshot(
    hall_name: str,
    url: str | None = None,
    snapshot_date: str | None = None,
    html: str | None = None,
) -> dict:
    source_url = url or HALL_URLS.get(hall_name)
    if not source_url:
        return {"machines": 0, "floor_maps": 0}
    target_date = snapshot_date or date.today().isoformat()
    parsed = parse_shop_page(html if html is not None else fetch_page(source_url))
    if not parsed["machines"] and not parsed["floor_maps"]:
        return {"machines": 0, "floor_maps": 0}

    conn = init_db()
    try:
        for machine in parsed["machines"]:
            conn.execute(
                """
                INSERT INTO hall_machine_snapshot
                    (hall_name,snapshot_date,machine_name,machine_id,source_url,unit_count)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(hall_name,snapshot_date,machine_name) DO UPDATE SET
                    machine_id=excluded.machine_id,
                    source_url=excluded.source_url,
                    unit_count=COALESCE(excluded.unit_count,hall_machine_snapshot.unit_count),
                    scraped_at=datetime('now','localtime')
                """,
                (
                    hall_name,
                    target_date,
                    machine["machine_name"],
                    machine["machine_id"],
                    source_url,
                    machine["unit_count"],
                ),
            )
        for index, image_url in enumerate(parsed["floor_maps"], 1):
            conn.execute(
                """
                INSERT INTO hall_floor_map_snapshot
                    (hall_name,snapshot_date,floor_index,image_url,page_url)
                VALUES (?,?,?,?,?)
                ON CONFLICT(hall_name,snapshot_date,floor_index) DO UPDATE SET
                    image_url=excluded.image_url,page_url=excluded.page_url,
                    scraped_at=datetime('now','localtime')
                """,
                (hall_name, target_date, index, image_url, source_url),
            )
        conn.commit()
    finally:
        conn.close()
    return {"machines": len(parsed["machines"]), "floor_maps": len(parsed["floor_maps"])}


def scrape_all() -> dict[str, dict]:
    results = {}
    for hall_name in HALL_URLS:
        try:
            results[hall_name] = {"status": "ok", **scrape_snapshot(hall_name)}
        except Exception as exc:  # 個別店舗の失敗で全体を止めない
            results[hall_name] = {"status": "failed", "machines": 0, "floor_maps": 0, "error": str(exc)}
    return results


if __name__ == "__main__":
    for name, result in scrape_all().items():
        print(f"{name}: {result}")
