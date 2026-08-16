"""P-WORLD の店舗ページからスマスロ設置機種を日次保存する。"""
from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

from hall.machine_scope import is_smartslot_machine

try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"


# 店舗自身が公開しているページだけを登録する。未確認URLは推測で追加しない。
HALL_URLS = {
    "キコーナ四條畷店": "https://www.p-world.co.jp/osaka/kicona-shijonawate.htm",
    "ひま・わり四條畷店": "https://www.p-world.co.jp/osaka/himawarisijounawate.htm",
    "キコーナ野崎店": "https://www.p-world.co.jp/osaka/kicona-nozaki.htm",
    "スーパーコスモプレミアム大東店": "https://www.p-world.co.jp/osaka/scpdaitou.htm",
    "ラッシュMATSUMOTO#59": "https://52572.p-world.jp",
    "チャンピオンOZ": "https://24133.p-world.jp",
    "マルハン松本店": "https://76679.p-world.jp",
    "チャンピオンANNEX": "https://80562.p-world.jp",
    "KEIZ松本店": "https://48363.p-world.jp",
    "ABC松本白板店": "https://41620.p-world.jp",
    "No.1松本筑摩店": "https://22527.p-world.jp",
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
    conn.commit()
    return conn


def parse_machine_links(html: str) -> list[dict[str, str]]:
    """店舗ページ内の機種DBリンクから、スマスロだけを重複なく返す。"""
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, dict[str, str]] = {}
    for anchor in soup.find_all("a", href=True):
        match = re.search(r"/machine/database/(\d+)", anchor.get("href", ""))
        if not match:
            continue
        name = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        if not name or not is_smartslot_machine(name):
            continue
        found.setdefault(name, {"machine_name": name, "machine_id": match.group(1)})
    return list(found.values())


def fetch_page(url: str) -> str:
    """P-WORLD の EUC-JP ページをブラウザ相当のTLS設定で取得する。"""
    try:
        from curl_cffi import requests as cf_requests
    except ImportError as exc:  # pragma: no cover - 実環境の依存不足時だけ
        raise RuntimeError("curl-cffi が必要です") from exc

    response = cf_requests.get(url, impersonate="chrome120", timeout=25)
    response.raise_for_status()
    # P-WORLD は x-euc-jp を返す。宣言が欠けたページにも安全に対応する。
    return response.content.decode("euc_jp", errors="replace")


def scrape_snapshot(
    hall_name: str,
    url: str | None = None,
    snapshot_date: str | None = None,
    html: str | None = None,
) -> int:
    """1店舗の本日の設置スマスロ一覧を保存し、保存機種数を返す。"""
    source_url = url or HALL_URLS.get(hall_name)
    if not source_url:
        return 0
    target_date = snapshot_date or date.today().isoformat()
    page_html = html if html is not None else fetch_page(source_url)
    machines = parse_machine_links(page_html)
    if not machines:
        # 取得失敗を「設置0台」と誤認しないよう空スナップショットは保存しない。
        return 0

    conn = init_db()
    try:
        for machine in machines:
            conn.execute(
                """
                INSERT OR REPLACE INTO hall_machine_snapshot
                    (hall_name, snapshot_date, machine_name, machine_id, source_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    hall_name,
                    target_date,
                    machine["machine_name"],
                    machine["machine_id"],
                    source_url,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return len(machines)


def scrape_all(halls: list[dict] | list[str]) -> dict[str, int]:
    """URLを確認済みの対象店だけ収集する。"""
    results: dict[str, int] = {}
    for hall in halls:
        hall_name = hall.get("hall_name", "") if isinstance(hall, dict) else hall
        if hall_name not in HALL_URLS:
            continue
        try:
            results[hall_name] = scrape_snapshot(hall_name)
        except Exception:
            results[hall_name] = 0
    return results


if __name__ == "__main__":
    for name, count in scrape_all(list(HALL_URLS)).items():
        print(f"{name}: {count}機種")
