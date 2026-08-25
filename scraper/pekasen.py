"""ペカセンの公開機種ページからジャグラー台別BB/RB実績を保存する。

各ページは直近30営業日を公開しているため、低頻度の日次取得で履歴を積み上げる。
公開HTMLだけを参照し、非公開APIやアクセス制限の回避は行わない。
"""
from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"


BASE_URL = "https://pekasen.com"
REQUEST_DELAY = 4.0
HEADERS = {
    "User-Agent": "PACHI-TOOL/3.0 public-reference-collector",
    "Accept-Language": "ja,en;q=0.8",
}

MACHINE_PATHS = {
    "myjuggler-v": "マイジャグラーV",
    "funky-juggler-2": "ファンキージャグラー2",
    "im-juggler-ex": "アイムジャグラーEX",
    "neo-im-juggler-ex": "ネオアイムジャグラーEX",
    "gogo-juggler-3": "ゴーゴージャグラー3",
    "happy-juggler-viii": "ハッピージャグラーV III",
    "juggler-girls-ss": "ジャグラーガールズSS",
    "ultra-miracle-juggler": "ウルトラミラクルジャグラー",
    "mr-juggler": "ミスタージャグラー",
}

# 公開一覧とsitemapで存在を確認した四條畷駅周辺の店舗・機種だけを登録する。
HALL_SOURCES = {
    "ニコニコ住道店": {
        "store_path": "nikoniko-suminodou",
        "published_names": ["ニコニコ住道店"],
        "machines": [
            "myjuggler-v", "funky-juggler-2", "im-juggler-ex",
            "gogo-juggler-3", "happy-juggler-viii",
            "ultra-miracle-juggler", "mr-juggler",
        ],
    },
    "スーパーコスモプレミアム大東店": {
        "store_path": "osaka-732102",
        "published_names": ["ＳＵＰＥＲ ＣＯＳＭＯ ＰＲＥＭＩＵＭ大東店"],
        "machines": [
            "myjuggler-v", "funky-juggler-2", "neo-im-juggler-ex",
            "gogo-juggler-3", "juggler-girls-ss",
            "ultra-miracle-juggler", "mr-juggler",
        ],
    },
    "ベガスベガス大東店": {
        "store_path": "osaka-732922",
        "published_names": ["ベガスベガス大東店"],
        "machines": list(MACHINE_PATHS),
    },
}


def _compact(value: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKC", value or "").lower()
        if character.isalnum()
    )


def _number(value: str) -> int | None:
    text = unicodedata.normalize("NFKC", value or "").strip()
    if text in {"", "-", "−", "―", "ー"}:
        return None
    text = text.replace(",", "").replace("+", "").replace("−", "-")
    match = re.fullmatch(r"-?\d+", text)
    return int(text) if match else None


def _report_date(month: int, day: int, as_of: date) -> date | None:
    try:
        candidate = date(as_of.year, month, day)
        # 1月のページに含まれる前年12月を正しく扱う。
        if candidate > as_of + timedelta(days=7):
            candidate = date(as_of.year - 1, month, day)
        return candidate
    except ValueError:
        return None


def init_db(path: Path | None = None) -> sqlite3.Connection:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hall_source_juggler_daily (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source       TEXT NOT NULL,
            hall_name    TEXT NOT NULL,
            report_date  TEXT NOT NULL,
            machine_name TEXT NOT NULL,
            seat_number  INTEGER NOT NULL,
            games        INTEGER NOT NULL,
            bb_count     INTEGER NOT NULL,
            rb_count     INTEGER NOT NULL,
            diff_coins   INTEGER,
            source_url   TEXT NOT NULL,
            scraped_at   TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(source,hall_name,report_date,machine_name,seat_number)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hall_day_seat (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name    TEXT NOT NULL,
            report_date  TEXT NOT NULL,
            machine_name TEXT NOT NULL,
            seat_number  INTEGER,
            diff_coins   INTEGER,
            games        INTEGER,
            ev_pct       REAL,
            bb_prob      REAL,
            rb_prob      REAL,
            source       TEXT DEFAULT 'unknown',
            source_url   TEXT,
            scraped_at   TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(hall_name,report_date,machine_name,seat_number)
        )
        """
    )
    conn.commit()
    return conn


def parse_machine_page(
    html: str,
    hall_name: str,
    machine_name: str,
    source_url: str,
    published_names: list[str] | None = None,
    as_of: date | None = None,
) -> list[dict]:
    """機種別ページの30日分の台別表を検証して標準形式へ変換する。"""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    valid_names = published_names or [hall_name]
    if not any(_compact(name) in _compact(title) for name in valid_names):
        return []
    if _compact(machine_name) not in _compact(title):
        return []

    current = as_of or date.today()
    found: dict[tuple[str, int], dict] = {}
    for block in soup.select(".dayblock"):
        head = block.select_one(".day-head")
        table = block.select_one("table")
        if head is None or table is None:
            continue
        date_match = re.search(r"(\d{1,2})/(\d{1,2})", head.get_text(" ", strip=True))
        if not date_match:
            continue
        report_day = _report_date(int(date_match.group(1)), int(date_match.group(2)), current)
        if report_day is None or report_day > current:
            continue

        table_rows = table.select("tr")
        if not table_rows:
            continue
        headers = [cell.get_text(" ", strip=True) for cell in table_rows[0].select("th,td")]
        required = {"台番", "G数", "BIG", "REG", "差枚"}
        if not required.issubset(headers):
            continue
        indexes = {name: headers.index(name) for name in required}
        for table_row in table_rows[1:]:
            cells = [cell.get_text(" ", strip=True) for cell in table_row.select("th,td")]
            if len(cells) < len(headers):
                continue
            seat = _number(cells[indexes["台番"]])
            games = _number(cells[indexes["G数"]])
            bb_count = _number(cells[indexes["BIG"]])
            rb_count = _number(cells[indexes["REG"]])
            diff_coins = _number(cells[indexes["差枚"]])
            if seat is None or games is None or games <= 0 or bb_count is None or rb_count is None:
                continue
            if bb_count < 0 or rb_count < 0 or bb_count + rb_count > games:
                continue
            found[(report_day.isoformat(), seat)] = {
                "source": "pekasen",
                "hall_name": hall_name,
                "report_date": report_day.isoformat(),
                "machine_name": machine_name,
                "seat_number": seat,
                "games": games,
                "bb_count": bb_count,
                "rb_count": rb_count,
                "diff_coins": diff_coins,
                "source_url": source_url,
            }
    return list(found.values())


def save_rows(rows: list[dict], path: Path | None = None) -> int:
    """原本を保持し、BB/RBがない既存行だけを検証済みデータで補完する。"""
    if not rows:
        return 0
    conn = init_db(path)
    saved = 0
    try:
        for row in rows:
            conn.execute(
                """
                INSERT INTO hall_source_juggler_daily
                    (source,hall_name,report_date,machine_name,seat_number,games,
                     bb_count,rb_count,diff_coins,source_url,scraped_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
                ON CONFLICT(source,hall_name,report_date,machine_name,seat_number)
                DO UPDATE SET games=excluded.games,bb_count=excluded.bb_count,
                    rb_count=excluded.rb_count,diff_coins=excluded.diff_coins,
                    source_url=excluded.source_url,scraped_at=datetime('now','localtime')
                """,
                (
                    row["source"], row["hall_name"], row["report_date"], row["machine_name"],
                    row["seat_number"], row["games"], row["bb_count"], row["rb_count"],
                    row["diff_coins"], row["source_url"],
                ),
            )
            existing = conn.execute(
                """SELECT source,bb_prob,rb_prob FROM hall_day_seat
                   WHERE hall_name=? AND report_date=? AND machine_name=? AND seat_number=?""",
                (row["hall_name"], row["report_date"], row["machine_name"], row["seat_number"]),
            ).fetchone()
            # 手入力や別取得元のBB/RBが既にある場合は上書きしない。
            if existing and existing["source"] != "pekasen" and (
                existing["source"] == "manual"
                or (existing["bb_prob"] is not None and existing["rb_prob"] is not None)
            ):
                continue
            bb_prob = row["bb_count"] / row["games"]
            rb_prob = row["rb_count"] / row["games"]
            if existing:
                conn.execute(
                    """UPDATE hall_day_seat
                          SET diff_coins=?,games=?,bb_prob=?,rb_prob=?,source='pekasen',
                              source_url=?,scraped_at=datetime('now','localtime')
                        WHERE hall_name=? AND report_date=? AND machine_name=? AND seat_number=?""",
                    (
                        row["diff_coins"], row["games"], bb_prob, rb_prob, row["source_url"],
                        row["hall_name"], row["report_date"], row["machine_name"], row["seat_number"],
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO hall_day_seat
                           (hall_name,report_date,machine_name,seat_number,diff_coins,games,
                            bb_prob,rb_prob,source,source_url)
                         VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["hall_name"], row["report_date"], row["machine_name"],
                        row["seat_number"], row["diff_coins"], row["games"], bb_prob,
                        rb_prob, "pekasen", row["source_url"],
                    ),
                )
            saved += 1
        conn.commit()
    finally:
        conn.close()
    return saved


def is_refresh_due(path: Path | None = None, max_age_hours: int = 18) -> bool:
    conn = init_db(path)
    try:
        row = conn.execute(
            "SELECT MAX(scraped_at) FROM hall_source_juggler_daily WHERE source='pekasen'"
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


def scrape_all(session: requests.Session | None = None) -> list[dict]:
    """登録済み3店舗の現行ジャグラーを低頻度で順番に取得する。"""
    client = session or requests.Session()
    client.headers.update(HEADERS)
    results: list[dict] = []
    request_count = 0
    for hall_name, config in HALL_SOURCES.items():
        for machine_path in config["machines"]:
            if request_count:
                time.sleep(REQUEST_DELAY)
            request_count += 1
            source_url = f"{BASE_URL}/store/{config['store_path']}/{machine_path}"
            machine_name = MACHINE_PATHS[machine_path]
            try:
                response = client.get(source_url, timeout=45)
                response.raise_for_status()
                rows = parse_machine_page(
                    response.text,
                    hall_name,
                    machine_name,
                    source_url,
                    published_names=config["published_names"],
                )
                saved = save_rows(rows)
                results.append({
                    "status": "ok" if rows else "no_public_data",
                    "hall_name": hall_name,
                    "machine_name": machine_name,
                    "rows": saved,
                    "published_rows": len(rows),
                    "source_url": source_url,
                })
            except requests.RequestException as exc:
                results.append({
                    "status": "error",
                    "hall_name": hall_name,
                    "machine_name": machine_name,
                    "rows": 0,
                    "error": str(exc)[:300],
                    "source_url": source_url,
                })
    return results


if __name__ == "__main__":
    for item in scrape_all():
        print(item)
