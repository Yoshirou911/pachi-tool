"""評論計画の公開月別・日別ページから機種別実績を保存する。

ログイン不要のHTMLだけを低頻度で参照し、公開画面に表示される
月と日付だけを取得対象にする。
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from hall.machine_scope import is_supported_analysis_machine

try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"


BASE_URL = "https://pachireview.com"
REQUEST_DELAY = 1.5
HEADERS = {
    "User-Agent": "PACHI-TOOL/3.5 public-data-collector",
    "Accept-Language": "ja,en;q=0.8",
}
HALL_SOURCES = {
    "マルハン大東店": "https://pachireview.com/shops/osaka/daitoushi/4340/data/",
}


def _number(text: str, *, as_float: bool = False) -> int | float | None:
    match = re.search(r"[+-]?\d[\d,]*(?:\.\d+)?", (text or "").replace("−", "-"))
    if not match:
        return None
    value = float(match.group(0).replace(",", ""))
    return value if as_float else round(value)


def discover_months(html: str) -> list[str]:
    """公開ページの月切替ボタンから YYYYMM を返す。"""
    values = {
        f"{int(year):04d}{int(month):02d}"
        for year, month in re.findall(r"(20\d{2})年\s*(\d{1,2})月", html or "")
        if 1 <= int(month) <= 12
    }
    return sorted(values, reverse=True)


def discover_daily_links(html: str, hall_url: str) -> list[str]:
    """一覧に実際に掲載された日別リンクだけを返す。"""
    soup = BeautifulSoup(html or "", "lxml")
    prefix = hall_url.rstrip("/") + "/"
    links: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(BASE_URL, anchor["href"])
        if absolute.startswith(prefix) and re.fullmatch(
            re.escape(prefix) + r"20\d{6}/", absolute
        ):
            links.add(absolute)
    return sorted(links)


def parse_daily_page(html: str, source_url: str) -> tuple[str, list[dict]]:
    """日別ページの機種集計グリッドを解析する。"""
    date_match = re.search(r"/data/(20\d{6})/", source_url)
    if not date_match:
        raise ValueError("日付URLを確認できません")
    raw_date = date_match.group(1)
    report_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
    soup = BeautifulSoup(html or "", "lxml")
    rows: list[dict] = []
    for grid in soup.select(".shop-machine-grid"):
        cells = grid.find_all(recursive=False)
        if len(cells) != 6:
            continue
        machine_name = re.sub(r"\s+", " ", cells[0].get_text(" ", strip=True)).strip()
        if not machine_name or "MODEL /" in machine_name or not is_supported_analysis_machine(machine_name):
            continue
        win_text = cells[3].get_text(" ", strip=True)
        unit_match = re.search(r"\d+\s*/\s*(\d+)", win_text)
        payout = _number(cells[4].get_text(" ", strip=True), as_float=True)
        rows.append({
            "report_date": report_date,
            "machine_name": machine_name,
            "unit_count": int(unit_match.group(1)) if unit_match else None,
            "avg_diff_coins": _number(cells[1].get_text(" ", strip=True)),
            "total_diff_coins": _number(cells[2].get_text(" ", strip=True)),
            "win_rate_pct": _number(win_text, as_float=True),
            "ev_pct": payout,
            "avg_games": _number(cells[5].get_text(" ", strip=True)),
            "source_url": source_url,
        })
    if not rows:
        raise ValueError("機種別公開データを確認できません")
    return report_date, rows


def _connect() -> sqlite3.Connection:
    from scraper.anoslot_public import init_db

    conn = init_db(DB_PATH)
    return conn


def is_refresh_due(max_age_hours: int = 18) -> bool:
    """公開元の負荷を抑えるため、最後の完了時刻で更新を間引く。"""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT MAX(scraped_at) FROM hall_source_machine_daily WHERE source='pachireview'"
            ).fetchone()
    except sqlite3.Error:
        return True
    if not row or not row[0]:
        return True
    try:
        return datetime.fromisoformat(row[0]) < datetime.now() - timedelta(hours=max_age_hours)
    except ValueError:
        return True


def save_rows(hall_name: str, rows: list[dict]) -> tuple[int, int]:
    source_saved = analysis_saved = 0
    with _connect() as conn:
        for row in rows:
            before = conn.total_changes
            conn.execute(
                """INSERT INTO hall_source_machine_daily
                   (source,hall_name,report_date,machine_name,unit_count,total_diff_coins,
                    avg_diff_coins,avg_games,win_rate_pct,source_url)
                   VALUES ('pachireview',?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source,hall_name,report_date,machine_name) DO UPDATE SET
                    unit_count=excluded.unit_count,total_diff_coins=excluded.total_diff_coins,
                    avg_diff_coins=excluded.avg_diff_coins,avg_games=excluded.avg_games,
                    win_rate_pct=excluded.win_rate_pct,source_url=excluded.source_url,
                    scraped_at=datetime('now','localtime')""",
                (
                    hall_name, row["report_date"], row["machine_name"], row["unit_count"],
                    row["total_diff_coins"], row["avg_diff_coins"], row["avg_games"],
                    row["win_rate_pct"], row["source_url"],
                ),
            )
            source_saved += int(conn.total_changes > before)
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO hall_day_machine
                   (hall_name,report_date,machine_name,unit_count,avg_diff_coins,
                    avg_games,win_rate_pct,ev_pct,source_url)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    hall_name, row["report_date"], row["machine_name"], row["unit_count"],
                    row["avg_diff_coins"], row["avg_games"], row["win_rate_pct"],
                    row["ev_pct"], row["source_url"],
                ),
            )
            analysis_saved += conn.total_changes - before
    return source_saved, analysis_saved


def _get(session: requests.Session, url: str) -> str:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=25)
            response.raise_for_status()
            if len(response.text) < 1000:
                raise RuntimeError("公開ページの応答が不完全です")
            return response.text
        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(str(last_error or "取得できません"))


def scrape_hall(hall_name: str, session: requests.Session | None = None) -> dict:
    hall_url = HALL_SOURCES.get(hall_name)
    if not hall_url:
        return {"hall_name": hall_name, "status": "unsupported", "days": 0, "rows": 0}
    own_session = session or requests.Session()
    own_session.headers.update(HEADERS)
    base_html = _get(own_session, hall_url)
    months = discover_months(base_html)
    daily_links: set[str] = set()
    for month in months:
        daily_links.update(discover_daily_links(_get(own_session, f"{hall_url}?ym={month}"), hall_url))
        time.sleep(REQUEST_DELAY)
    source_saved = analysis_saved = failed = 0
    for link in sorted(daily_links):
        try:
            _, rows = parse_daily_page(_get(own_session, link), link)
            raw_count, analysis_count = save_rows(hall_name, rows)
            source_saved += raw_count
            analysis_saved += analysis_count
        except Exception:
            failed += 1
        time.sleep(REQUEST_DELAY)
    return {
        "hall_name": hall_name,
        "status": "ok" if daily_links and failed == 0 else "partial",
        "months": len(months),
        "days": len(daily_links),
        "failed_days": failed,
        "source_saved": source_saved,
        "analysis_saved": analysis_saved,
        "source_url": hall_url,
    }


def scrape_all() -> list[dict]:
    return [scrape_hall(hall_name) for hall_name in HALL_SOURCES]


if __name__ == "__main__":
    import json

    print(json.dumps(scrape_all(), ensure_ascii=False, indent=2))
