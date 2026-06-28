"""
アナスロ (ana-slo.com) スクレーパー - curl_cffi版

台番別データ（差枚・G数・BB確率・RB確率）を取得してSQLiteに保存する。
curl_cffi で Chrome の TLS フィンガープリントを模倣し、Cloudflare を自動突破。
cf_clearance Cookie の手動更新は不要。

必要パッケージ:
    pip install curl-cffi beautifulsoup4 lxml

使い方:
    # 大阪府の店舗一覧確認
    python -m scraper.anaslo --explore --pref 大阪府

    # データ取得
    python -m scraper.anaslo --hall "キコーナ四條畷店" --days 30
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

# curl_cffi が利用可能なら使う（Cloudflare 自動突破）、なければ cloudscraper にフォールバック
try:
    from curl_cffi import requests as cf_requests
    _USE_CURL_CFFI = True
except ImportError:
    try:
        import cloudscraper as _cs_mod
        _USE_CURL_CFFI = False
    except ImportError:
        _cs_mod = None
        _USE_CURL_CFFI = False

try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"

BASE_URL = "https://ana-slo.com"
DELAY = 60.0       # Cloudflare対策: 60秒待機
MAX_PER_RUN = 10   # 手動実行の1回あたり上限（夜間バッチは無制限）

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "upgrade-insecure-requests": "1",
    "Referer": BASE_URL + "/",
}


def _make_session(cookie_str: str = ""):
    """
    curl_cffi セッション（CF自動突破）を返す。
    curl_cffi が未インストールなら cloudscraper にフォールバック。
    cookie_str が指定された場合はそのCookieも追加注入する。
    """
    if _USE_CURL_CFFI:
        # curl_cffi: Chrome120 の TLS フィンガープリントで CF を自動突破
        session = cf_requests.Session(impersonate="chrome120")
        session.headers.update(HEADERS)
        # 追加Cookieがあれば注入（後方互換用）
        if cookie_str:
            for part in cookie_str.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    session.cookies.set(k.strip(), v.strip(), domain=".ana-slo.com")
            print("  [curl_cffi + Cookie注入済み]")
        else:
            print("  [curl_cffi: CF自動突破モード]")
        return session

    # フォールバック: cloudscraper
    if _cs_mod is None:
        raise RuntimeError("curl_cffi も cloudscraper もインストールされていません。pip install curl-cffi を実行してください。")
    scraper = _cs_mod.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    # DB保存のCookieを使用
    if not cookie_str:
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute(
                "SELECT value FROM scrape_settings WHERE key='cf_cookie_str'"
            ).fetchone()
            conn.close()
            if row and row[0]:
                cookie_str = row[0]
                print("  [cloudscraper + DB保存Cookie使用]")
        except Exception:
            pass
    if cookie_str:
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                scraper.cookies.set(k.strip(), v.strip(), domain=".ana-slo.com")
    return scraper


# 後方互換エイリアス
def _make_scraper(cookie_str: str = ""):
    return _make_session(cookie_str)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
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
            source       TEXT DEFAULT 'anaslo',
            source_url   TEXT,
            scraped_at   TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(hall_name, report_date, machine_name, seat_number)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scrape_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scrape_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name  TEXT NOT NULL,
            started_at TEXT DEFAULT (datetime('now','localtime')),
            finished_at TEXT,
            days_fetched INTEGER DEFAULT 0,
            rows_saved   INTEGER DEFAULT 0,
            status     TEXT DEFAULT 'running',
            error_msg  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scrape_hall_config (
            hall_name    TEXT PRIMARY KEY,
            prefecture   TEXT NOT NULL DEFAULT '大阪府',
            url_override TEXT,
            enabled      INTEGER NOT NULL DEFAULT 1,
            added_at     TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    for col, typ in [("bb_prob", "REAL"), ("rb_prob", "REAL"), ("source", "TEXT DEFAULT 'anaslo'")]:
        try:
            conn.execute(f"ALTER TABLE hall_day_seat ADD COLUMN {col} {typ}")
        except Exception:
            pass
    conn.commit()
    return conn


def save_cookie(cookie_str: str) -> None:
    """Cookieをscrape_settingsテーブルに保存"""
    conn = init_db()
    conn.execute(
        "INSERT OR REPLACE INTO scrape_settings (key, value, updated_at) VALUES (?, ?, datetime('now','localtime'))",
        ("cf_cookie_str", cookie_str)
    )
    conn.commit()
    conn.close()


def get_cookie() -> str:
    """保存済みCookieを取得"""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM scrape_settings WHERE key='cf_cookie_str'").fetchone()
        conn.close()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""


def get_scrape_logs(limit: int = 50) -> list[dict]:
    """スクレイプ実行ログを返す"""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """SELECT hall_name, started_at, finished_at, days_fetched, rows_saved, status, error_msg
               FROM scrape_log ORDER BY id DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        conn.close()
        return [
            {"hall_name": r[0], "started_at": r[1], "finished_at": r[2],
             "days_fetched": r[3], "rows_saved": r[4], "status": r[5], "error_msg": r[6]}
            for r in rows
        ]
    except Exception:
        return []


def get_hall_configs(enabled_only: bool = False) -> list[dict]:
    """スクレイプ対象ホール一覧をDBから取得"""
    try:
        conn = init_db()
        q = "SELECT hall_name, prefecture, url_override, enabled, added_at FROM scrape_hall_config"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY added_at"
        rows = conn.execute(q).fetchall()
        conn.close()
        return [
            {"hall_name": r[0], "prefecture": r[1], "url_override": r[2],
             "enabled": bool(r[3]), "added_at": r[4]}
            for r in rows
        ]
    except Exception:
        return []


def upsert_hall_config(hall_name: str, prefecture: str = "大阪府",
                       url_override: str = "", enabled: bool = True) -> None:
    """ホール設定を追加または更新"""
    conn = init_db()
    conn.execute(
        """INSERT INTO scrape_hall_config (hall_name, prefecture, url_override, enabled)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(hall_name) DO UPDATE SET
             prefecture=excluded.prefecture,
             url_override=excluded.url_override,
             enabled=excluded.enabled""",
        (hall_name, prefecture, url_override or None, 1 if enabled else 0)
    )
    conn.commit()
    conn.close()


def delete_hall_config(hall_name: str) -> bool:
    """ホール設定を削除。削除できれば True"""
    try:
        conn = init_db()
        cur = conn.execute("DELETE FROM scrape_hall_config WHERE hall_name=?", (hall_name,))
        conn.commit()
        deleted = cur.rowcount > 0
        conn.close()
        return deleted
    except Exception:
        return False


def seed_hall_configs(default_halls: list[dict]) -> None:
    """DBが空の場合のみデフォルトホールを投入"""
    conn = init_db()
    count = conn.execute("SELECT COUNT(*) FROM scrape_hall_config").fetchone()[0]
    if count == 0:
        for h in default_halls:
            conn.execute(
                "INSERT OR IGNORE INTO scrape_hall_config (hall_name, prefecture) VALUES (?, ?)",
                (h["hall_name"], h.get("prefecture", "大阪府"))
            )
        conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# パーサー
# ---------------------------------------------------------------------------

def _int(s: str) -> Optional[int]:
    if not s:
        return None
    try:
        cleaned = re.sub(r'[^\d\-]', '', s.replace('−', '-').replace('▲', '-').replace(',', ''))
        return int(cleaned) if cleaned and cleaned != '-' else None
    except Exception:
        return None


def _float(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        # 分数形式: "1/289.8" → 確率に変換
        m = re.match(r'^(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)$', s.strip())
        if m:
            num, den = float(m.group(1)), float(m.group(2))
            return round(num / den, 6) if den != 0 else None
        cleaned = re.sub(r'[^\d\.\-]', '', s)
        return float(cleaned) if cleaned else None
    except Exception:
        return None


def _normalize_machine_name(name: str) -> str:
    """機種名の表記ゆれ正規化: 全角スペース→半角、前後スペース除去、連続スペース圧縮"""
    if not name:
        return name
    name = name.replace('　', ' ').strip()
    name = re.sub(r'\s+', ' ', name)
    # 記号の全角→半角
    name = name.replace('　', ' ').replace('（', '(').replace('）', ')')
    return name


def _parse_date(text: str, year: int) -> str:
    """'6/25(木)' '2026/6/25' '06月25日' → '2026-06-25'"""
    m = re.search(r'(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})', text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r'(\d{1,2})/(\d{1,2})', text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        today = date.today()
        y = year if month <= today.month + 1 else year - 1
        return f"{y}-{month:02d}-{day:02d}"
    m = re.search(r'(\d{1,2})月(\d{1,2})日', text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        today = date.today()
        y = year if month <= today.month + 1 else year - 1
        return f"{y}-{month:02d}-{day:02d}"
    return ""


class CloudflareBlockedError(Exception):
    pass


def _get(session, url: str, retry: int = 2) -> Optional[BeautifulSoup]:
    """GETしてBeautifulSoupを返す。CF検知時は CloudflareBlockedError を raise。"""
    for attempt in range(retry + 1):
        try:
            if _USE_CURL_CFFI:
                resp = session.get(url, timeout=30)
            else:
                resp = session.get(url, headers=HEADERS, timeout=30)

            # Cloudflare challenge/block 検知
            cf_blocked = (
                resp.status_code in (403, 503) or
                ("Just a moment" in resp.text and "cf_clearance" in resp.text) or
                "cf-browser-verification" in resp.text or
                "Enable JavaScript and cookies to continue" in resp.text
            )
            if cf_blocked:
                if _USE_CURL_CFFI and attempt < retry:
                    print(f"  [CF検知 リトライ {attempt+1}/{retry}]")
                    time.sleep(5)
                    continue
                raise CloudflareBlockedError(f"Cloudflare blocked (HTTP {resp.status_code})")
            if resp.status_code != 200:
                print(f"  HTTP {resp.status_code}: {url}")
                return None
            return BeautifulSoup(resp.text, "lxml")
        except CloudflareBlockedError:
            raise
        except Exception as e:
            if attempt < retry:
                time.sleep(3)
                continue
            print(f"  取得エラー: {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# 店舗一覧
# ---------------------------------------------------------------------------

def explore_halls(prefecture: str):
    """アナスロの指定都道府県の店舗一覧を表示"""
    list_url = f"{BASE_URL}/ホールデータ/{prefecture}/"
    print(f"\n--- {prefecture} の店舗一覧 ---")
    print(f"URL: {list_url}\n")

    scraper = _make_session()
    soup = _get(scraper, list_url)
    if soup is None:
        print("ページ取得失敗")
        return

    # データ一覧リンクを探す
    links = soup.find_all("a", href=True)
    halls = []
    for a in links:
        href = a["href"]
        text = a.get_text(strip=True)
        if "データ一覧" in href or "データ一覧" in text:
            full_url = href if href.startswith("http") else BASE_URL + href
            halls.append((text, full_url))

    if not halls:
        print("店舗リンクが見つかりません。")
        print(f"ページ内容（先頭800文字）:\n{soup.get_text()[:800]}")
        return

    print(f"{len(halls)} 店舗見つかりました:\n")
    for name, url in halls:
        print(f"  {name}")
        print(f"    {url}")


# ---------------------------------------------------------------------------
# 日付リンク収集
# ---------------------------------------------------------------------------

def _collect_date_links(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    """店舗ページから (date_str, url) リストを収集"""
    year = date.today().year
    results = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        full_url = href if href.startswith("http") else BASE_URL + href

        date_str = ""

        # アナスロ日付URLパターン: /2026-06-24-店名-data/
        m = re.search(r'/(\d{4})-(\d{2})-(\d{2})-', href)
        if m:
            date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        # テキストから日付パース: "2026/06/24(火)" など
        if not date_str:
            date_str = _parse_date(text, year)

        # URLから /YYYY/MM/DD/ パターン
        if not date_str:
            m2 = re.search(r'/(\d{4})/(\d{2})/(\d{2})/?', href)
            if m2:
                date_str = f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"

        if date_str and full_url.startswith("http") and full_url != base_url:
            results.append((date_str, full_url))

    # 重複除去・日付降順
    seen: set[str] = set()
    unique = []
    for item in results:
        if item[0] not in seen:
            seen.add(item[0])
            unique.append(item)
    unique.sort(key=lambda x: x[0], reverse=True)
    return unique


# ---------------------------------------------------------------------------
# 台番別データ解析
# ---------------------------------------------------------------------------

def _parse_seat_tables(soup: BeautifulSoup, url: str) -> list[dict]:
    """HTMLから台番別データを抽出"""
    rows: list[dict] = []

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if not headers:
            continue

        has_seat = any(re.search(r'台番|台No|番号|No\.', h) for h in headers)
        has_diff = any("差枚" in h for h in headers)
        if not (has_seat and has_diff):
            continue

        # カラムインデックス
        idx: dict[str, int] = {}
        for i, h in enumerate(headers):
            if re.search(r'台番|台No|番号', h): idx.setdefault('seat', i)
            if '差枚' in h: idx.setdefault('diff', i)
            if re.search(r'G数|ゲーム|回転数', h): idx.setdefault('games', i)
            if re.search(r'出率|RTP|機械割', h): idx.setdefault('ev', i)
            if re.search(r'^BB|ビッグ', h): idx.setdefault('bb', i)
            if re.search(r'^RB|レギュラー', h): idx.setdefault('rb', i)

        # テーブル直前の見出しを機種名に
        # ページ共通ラベルやナビ要素は除外
        _NON_MACHINE = re.compile(
            r'^(全データ|データ一覧|スロット|パチスロ|ホール|店舗|ランキング|'
            r'台番|台データ|設定|合計|平均|トップ|メニュー|全機種|一覧|スペック)$'
        )
        machine_name = ""
        for prev in table.find_all_previous(["h2", "h3", "h4", "strong", "caption"]):
            t = prev.get_text(strip=True)
            if t and len(t) < 60 and not _NON_MACHINE.search(t):
                machine_name = t
                break

        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue

            def td(key: str) -> str:
                i = idx.get(key)
                if i is None or i >= len(tds):
                    return ''
                return tds[i].get_text(strip=True)

            seat = _int(td('seat'))
            if seat is None:
                continue

            rows.append({
                'machine_name': _normalize_machine_name(machine_name) or "不明",
                'seat_number': seat,
                'diff_coins': _int(td('diff')),
                'games': _int(td('games')),
                'ev_pct': _float(td('ev')),
                'bb_prob': _float(td('bb')),
                'rb_prob': _float(td('rb')),
                'source_url': url,
            })

    return rows


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def scrape_hall(hall_name: str, prefecture: str = "大阪府", max_days: int = 30,
               cf_cookie: str = "", cookie_str: str = "", log_id: Optional[int] = None,
               unlimited: bool = False):
    """指定ホールの台番別データをスクレイプ。unlimited=TrueでMAX_PER_RUN制限を無効化（夜間バッチ用）。"""
    conn = init_db()
    # cf_cookieが指定されていればcookie_strに変換（後方互換）
    if cf_cookie and not cookie_str:
        cookie_str = f"cf_clearance={cf_cookie}"
    scraper = _make_session(cookie_str=cookie_str)
    store_url = f"{BASE_URL}/ホールデータ/{prefecture}/{hall_name}-データ一覧/"

    print(f"\n=== アナスロ スクレーパー ===")
    print(f"ホール: {hall_name}")
    print(f"URL  : {store_url}\n")

    # ログ記録開始
    if log_id is None:
        cur = conn.execute(
            "INSERT INTO scrape_log (hall_name, status) VALUES (?, 'running')",
            (hall_name,)
        )
        log_id = cur.lastrowid
        conn.commit()

    soup = _get(scraper, store_url)
    if soup is None:
        conn.execute(
            "UPDATE scrape_log SET status='failed', error_msg=?, finished_at=datetime('now','localtime') WHERE id=?",
            ("店舗ページ取得失敗", log_id)
        )
        conn.commit()
        print("店舗ページ取得失敗")
        conn.close()
        return

    date_links = _collect_date_links(soup, store_url)
    print(f"{len(date_links)} 件の日付データを発見\n")

    if not date_links:
        conn.execute(
            "UPDATE scrape_log SET status='no_data', finished_at=datetime('now','localtime') WHERE id=?",
            (log_id,)
        )
        conn.commit()
        print("⚠ 日付リンクが見つかりません。")
        conn.close()
        return

    total_saved = 0
    fetched = 0  # 今回のセッションで実際にリクエストした数
    for i, (date_str, date_url) in enumerate(date_links[:max_days]):
        existing = conn.execute(
            "SELECT COUNT(*) FROM hall_day_seat WHERE hall_name=? AND report_date=? AND source='anaslo'",
            (hall_name, date_str),
        ).fetchone()[0]
        if existing > 0:
            print(f"  [{i+1:3d}] {date_str} スキップ ({existing}件取得済み)")
            continue

        if not unlimited and fetched >= MAX_PER_RUN:
            print(f"\n  ⚠ 1回の実行制限 ({MAX_PER_RUN}件) に達しました。時間をおいて再実行してください。")
            break

        print(f"  [{i+1:3d}/{min(len(date_links), max_days)}] {date_str} 取得中...", end=" ", flush=True)
        fetched += 1
        try:
            day_soup = _get(scraper, date_url)
        except CloudflareBlockedError as cf_e:
            err_msg = f"Cloudflareブロック: {cf_e}"
            print(f"\n  🚫 {err_msg}")
            conn.execute(
                "UPDATE scrape_log SET status='cf_blocked', error_msg=?, finished_at=datetime('now','localtime') WHERE id=?",
                (err_msg, log_id)
            )
            conn.commit()
            conn.close()
            return total_saved
        if day_soup is None:
            print("失敗")
            continue

        seat_rows = _parse_seat_tables(day_soup, date_url)
        saved = 0
        if not seat_rows:
            # データなしマーカーを保存して次回スキップ
            conn.execute("""
                INSERT OR IGNORE INTO hall_day_seat
                (hall_name, report_date, machine_name, seat_number, source)
                VALUES (?, ?, '_NODATA_', 0, 'anaslo')
            """, (hall_name, date_str))
            conn.commit()
            print("データなし")
            time.sleep(DELAY)
            continue
        for row in seat_rows:
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO hall_day_seat
                    (hall_name, report_date, machine_name, seat_number,
                     diff_coins, games, ev_pct, bb_prob, rb_prob, source, source_url)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    hall_name, date_str, row['machine_name'], row['seat_number'],
                    row['diff_coins'], row['games'], row.get('ev_pct'),
                    row.get('bb_prob'), row.get('rb_prob'), 'anaslo', row['source_url'],
                ))
                saved += 1
            except Exception as e:
                print(f"\n    DB保存エラー: {e}")

        conn.commit()
        total_saved += saved
        print(f"{saved}件保存")
        time.sleep(DELAY)

    conn.execute(
        """UPDATE scrape_log SET status='done', rows_saved=?, days_fetched=?,
           finished_at=datetime('now','localtime') WHERE id=?""",
        (total_saved, fetched, log_id)
    )
    conn.commit()
    print(f"\n=== 完了: 合計 {total_saved} 件保存 → {DB_PATH} ===")
    conn.close()
    return total_saved


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="アナスロ 台番別データスクレーパー")
    parser.add_argument("--hall", default="キコーナ四條畷店", help="ホール名")
    parser.add_argument("--pref", default="大阪府", help="都道府県")
    parser.add_argument("--days", type=int, default=30, help="最大取得日数")
    parser.add_argument("--explore", action="store_true", help="都道府県の店舗一覧を表示")
    parser.add_argument("--cf-cookie", default="", help="Cloudflare cf_clearance Cookieを手動指定")
    parser.add_argument("--cookie-str", default="", help="ブラウザからコピーしたCookie文字列全体 (key=val; key2=val2 形式)")
    args = parser.parse_args()

    if args.explore:
        explore_halls(args.pref)
    else:
        scrape_hall(args.hall, args.pref, args.days, cf_cookie=args.cf_cookie, cookie_str=args.cookie_str)


if __name__ == "__main__":
    main()
