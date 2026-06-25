"""
みんレポ (min-repo.com) スクレーパー

指定ホールの日次差枚データを取得し、SQLiteに保存する。
取得データ:
  - 日付, 機種名, 平均差枚, 平均G数, 出率, 台数
  - バラエティ台は台番別データも取得

使い方:
    python -m scraper.minrepo --hall "ベガスベガス大東店" --days 30
    python -m scraper.minrepo --hall "ベガスベガス大東店" --url-id 3187258  # 特定日
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional
import urllib.parse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
BASE_URL = "https://min-repo.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.9",
}
try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"
REQUEST_DELAY = 1.5  # 秒 (サーバー負荷軽減)


# ---------------------------------------------------------------------------
# DB 初期化
# ---------------------------------------------------------------------------

def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hall_day_machine (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name   TEXT NOT NULL,
            report_date TEXT NOT NULL,
            machine_name TEXT NOT NULL,
            unit_count  INTEGER,
            avg_diff_coins INTEGER,
            avg_games   INTEGER,
            win_rate_pct REAL,
            ev_pct      REAL,
            source_url  TEXT,
            scraped_at  TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(hall_name, report_date, machine_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hall_day_seat (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name   TEXT NOT NULL,
            report_date TEXT NOT NULL,
            machine_name TEXT NOT NULL,
            seat_number INTEGER,
            diff_coins  INTEGER,
            games       INTEGER,
            ev_pct      REAL,
            source_url  TEXT,
            scraped_at  TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(hall_name, report_date, machine_name, seat_number)
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# HTML パーサー
# ---------------------------------------------------------------------------

def _int(s: str) -> Optional[int]:
    """カンマ区切り整数を安全にパース"""
    try:
        return int(s.replace(",", "").replace("+", "").replace("−", "-").replace("▲", "-").strip())
    except Exception:
        return None


def _float(s: str) -> Optional[float]:
    try:
        return float(s.replace("%", "").strip())
    except Exception:
        return None


def parse_report_page(html: str, url: str) -> tuple[list[dict], list[dict]]:
    """
    1日のレポートページをパースして機種別・台別データを返す。
    Returns:
        machine_rows: list of dict (機種別集計)
        seat_rows:    list of dict (台別データ、バラエティのみ)
    """
    soup = BeautifulSoup(html, "lxml")
    machine_rows: list[dict] = []
    seat_rows: list[dict] = []

    tables = soup.find_all("table")

    for table in tables:
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if not headers:
            continue

        def _td(tds, col_name, idx_map, fallback=None):
            i = idx_map.get(col_name)
            if i is None or i >= len(tds):
                return fallback
            return tds[i].get_text(strip=True)

        # 機種別データテーブル（平均差枚・平均G数・勝率・出率 列）
        has_avg_diff = any("差枚" in h for h in headers)
        has_avg_g    = any("G数" in h for h in headers)
        has_seat     = any("台番" in h for h in headers)

        if has_avg_diff and has_avg_g and not has_seat:
            idx = {}
            for i, h in enumerate(headers):
                idx[h] = i
                # 短縮マッチング
                if "差枚" in h: idx.setdefault("__diff__", i)
                if "G数"  in h: idx.setdefault("__games__", i)
                if "出率"  in h: idx.setdefault("__ev__", i)
                if "勝率"  in h: idx.setdefault("__wr__", i)

            for tr in table.find_all("tr")[1:]:
                tds = tr.find_all("td")
                if len(tds) < 2:
                    continue
                machine_name = tds[0].get_text(strip=True)
                if not machine_name:
                    continue

                unit_count = None
                unit_match = re.search(r'[\(（](\d+)台[\)）]', machine_name)
                if unit_match:
                    unit_count = int(unit_match.group(1))
                    machine_name = re.sub(r'[\(（]\d+台[\)）]', '', machine_name).strip()

                avg_diff = _int(_td(tds, "__diff__", idx) or "")
                avg_g    = _int(_td(tds, "__games__", idx) or "")
                ev_pct   = _float(_td(tds, "__ev__", idx) or "")

                wr_pct = None
                wr_text = _td(tds, "__wr__", idx) or ""
                m2 = re.match(r'(\d+)/(\d+)', wr_text)
                if m2:
                    wr_pct = round(int(m2.group(1)) / int(m2.group(2)) * 100, 1)

                machine_rows.append({
                    "machine_name": machine_name,
                    "unit_count": unit_count,
                    "avg_diff_coins": avg_diff,
                    "avg_games": avg_g,
                    "win_rate_pct": wr_pct,
                    "ev_pct": ev_pct,
                    "source_url": url,
                })

        # 台別データテーブル（台番・差枚・G数・出率 列）
        elif has_seat and has_avg_diff:
            idx = {}
            for i, h in enumerate(headers):
                idx[h] = i
                if "台番" in h: idx.setdefault("__seat__", i)
                if "差枚" in h: idx.setdefault("__diff__", i)
                if "G数"  in h: idx.setdefault("__games__", i)
                if "出率"  in h: idx.setdefault("__ev__", i)

            machine_name = ""
            prev = table.find_previous(["h2", "h3", "caption", "strong", "p"])
            if prev:
                machine_name = prev.get_text(strip=True)

            for tr in table.find_all("tr")[1:]:
                tds = tr.find_all("td")
                if len(tds) < 2:
                    continue
                seat = _int(_td(tds, "__seat__", idx) or "")
                diff = _int(_td(tds, "__diff__", idx) or "")
                games = _int(_td(tds, "__games__", idx) or "")
                ev_pct = _float(_td(tds, "__ev__", idx) or "")

                if seat is None:
                    continue
                seat_rows.append({
                    "machine_name": machine_name,
                    "seat_number": seat,
                    "diff_coins": diff,
                    "games": games,
                    "ev_pct": ev_pct,
                    "source_url": url,
                })

    return machine_rows, seat_rows


# ---------------------------------------------------------------------------
# メインスクレーパー
# ---------------------------------------------------------------------------

def fetch_report_links(hall_tag_url: str, max_pages: int = 3) -> list[tuple[str, str]]:
    """
    ホールのタグページから (date_str, report_url) のリストを返す。
    """
    results = []
    url = hall_tag_url
    for page in range(max_pages):
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"  タグページ取得失敗: {resp.status_code} {url}")
            break
        soup = BeautifulSoup(resp.text, "lxml")

        # リンクを全部探す: <a href="/数字/">M/D(曜)</a>
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.match(r"^/?(\d+)/?$", href) or re.match(r"^https://min-repo\.com/\d+/?$", href):
                text = a.get_text(strip=True)
                # テキストが日付形式か確認 "6/25(木)" or "12/20(土)"
                if re.match(r"\d+/\d+[（(][月火水木金土日][）)]", text):
                    full_url = href if href.startswith("http") else BASE_URL + ("/" if not href.startswith("/") else "") + href
                    results.append((text, full_url))

        # 次ページ
        next_link = soup.find("a", string=re.compile(r"次|Next|›|>>"))
        if next_link and next_link.get("href"):
            url = next_link["href"]
            if not url.startswith("http"):
                url = BASE_URL + url
            time.sleep(REQUEST_DELAY)
        else:
            break

    # 重複除去
    seen = set()
    unique = []
    for item in results:
        if item[1] not in seen:
            seen.add(item[1])
            unique.append(item)
    return unique


def scrape_report(url: str, hall_name: str, report_date_str: str, conn: sqlite3.Connection) -> int:
    """1日分のレポートをスクレイプしてDBに保存。保存件数を返す"""
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        print(f"  レポート取得失敗: {resp.status_code} {url}")
        return 0

    machine_rows, seat_rows = parse_report_page(resp.text, url)

    saved = 0
    for row in machine_rows:
        try:
            conn.execute("""
                INSERT OR REPLACE INTO hall_day_machine
                (hall_name, report_date, machine_name, unit_count, avg_diff_coins,
                 avg_games, win_rate_pct, ev_pct, source_url)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (hall_name, report_date_str, row["machine_name"], row["unit_count"],
                  row["avg_diff_coins"], row["avg_games"], row["win_rate_pct"],
                  row["ev_pct"], row["source_url"]))
            saved += 1
        except Exception as e:
            print(f"  DB保存エラー: {e}")

    for row in seat_rows:
        try:
            conn.execute("""
                INSERT OR REPLACE INTO hall_day_seat
                (hall_name, report_date, machine_name, seat_number, diff_coins,
                 games, ev_pct, source_url)
                VALUES (?,?,?,?,?,?,?,?)
            """, (hall_name, report_date_str, row["machine_name"], row["seat_number"],
                  row["diff_coins"], row["games"], row["ev_pct"], row["source_url"]))
        except Exception:
            pass

    conn.commit()
    return saved


def build_tag_url(hall_name: str) -> str:
    encoded = urllib.parse.quote(hall_name)
    return f"{BASE_URL}/tag/{encoded}/"


def parse_date_from_text(text: str, year: int) -> str:
    """'6/25(木)' → '2026-06-25'"""
    m = re.match(r"(\d+)/(\d+)", text)
    if not m:
        return ""
    month, day = int(m.group(1)), int(m.group(2))
    # 年をまたぐ場合の補正（12月が先に来て1月が後なら前年）
    today = date.today()
    if month > today.month + 1:
        year -= 1
    return f"{year:04d}-{month:02d}-{day:02d}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="みんレポ ホールデータスクレーパー")
    parser.add_argument("--hall", default="ベガスベガス大東店", help="ホール名")
    parser.add_argument("--days", type=int, default=30, help="最大取得日数")
    parser.add_argument("--url-id", type=int, default=None, help="特定レポートID (テスト用)")
    args = parser.parse_args()

    conn = init_db()
    hall = args.hall

    if args.url_id:
        # 単発テスト
        url = f"{BASE_URL}/{args.url_id}/"
        print(f"テスト取得: {url}")
        rows, seats = parse_report_page(requests.get(url, headers=HEADERS).text, url)
        print(f"  機種別: {len(rows)}件")
        for r in rows[:5]:
            print(f"    {r['machine_name']}: 差枚{r['avg_diff_coins']}, G{r['avg_games']}, 出率{r['ev_pct']}%")
        print(f"  台別: {len(seats)}件")
        return

    tag_url = build_tag_url(hall)
    print(f"タグページ: {tag_url}")
    links = fetch_report_links(tag_url, max_pages=3)
    print(f"  {len(links)} 件のレポートを発見")

    year = date.today().year
    total_saved = 0

    for i, (date_text, report_url) in enumerate(links[:args.days]):
        date_str = parse_date_from_text(date_text, year)
        if not date_str:
            continue

        # 既取得チェック
        existing = conn.execute(
            "SELECT COUNT(*) FROM hall_day_machine WHERE hall_name=? AND report_date=?",
            (hall, date_str)
        ).fetchone()[0]
        if existing > 0:
            print(f"  [{i+1}/{min(len(links), args.days)}] {date_str} スキップ（取得済み {existing}件）")
            continue

        print(f"  [{i+1}/{min(len(links), args.days)}] {date_str} 取得中... {report_url}")
        saved = scrape_report(report_url, hall, date_str, conn)
        print(f"    → {saved}件保存")
        total_saved += saved
        time.sleep(REQUEST_DELAY)

    print(f"\n完了: 合計 {total_saved} 件保存 → {DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
