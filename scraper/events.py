"""
ホールイベント情報スクレーパー

対応ソース:
  - Dステ (dste.jp) — ホール公式イベントカレンダー
  - P-WORLD (p-world.ne.jp) — ホール情報・特定日
  - Twitter / X  (Nitter経由)
  - Google検索 (スニペットからイベント情報を補完)
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
import urllib.parse

import requests
from bs4 import BeautifulSoup

try:
    from config import HALL_REPORTS_DB as DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent.parent / "data" / "hall_reports.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://xcancel.com",
    "https://nitter.privacydev.net",
    "https://nitter.cz",
]


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def init_event_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hall_event (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name   TEXT NOT NULL,
            event_date  TEXT NOT NULL,
            event_type  TEXT,
            event_title TEXT,
            source      TEXT,
            source_url  TEXT,
            created_at  TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(hall_name, event_date, source, event_title)
        )
    """)
    conn.commit()


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    init_event_db(conn)
    return conn


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _classify_event(text: str) -> str:
    if re.search(r'新台|入替', text):
        return "新台入替"
    if re.search(r'[0-9０-９]{3}|ゾロ目', text):
        return "特日ゾロ目"
    if re.search(r'7+|７+', text):
        return "特日7"
    if re.search(r'感謝|記念|周年|誕生', text):
        return "感謝デー"
    if re.search(r'朝イチ|朝一|モーニング', text):
        return "朝イチ"
    if re.search(r'高設定|設定示唆|全台|全機種', text):
        return "高設定示唆"
    if re.search(r'イベント|イベ', text):
        return "通常イベント"
    return "その他"


def _parse_jp_date(text: str, base_year: Optional[int] = None) -> Optional[str]:
    year = base_year or date.today().year
    m = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            pass
    m = re.search(r'(\d{1,2})[/月](\d{1,2})', text)
    if m:
        try:
            return date(year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            pass
    return None


def _extract_dates_from_text(text: str) -> list[str]:
    found = []
    today = date.today()
    for m in re.finditer(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', text):
        try:
            found.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat())
        except ValueError:
            pass
    for m in re.finditer(r'(\d{1,2})[/月](\d{1,2})[日]?', text):
        try:
            found.append(date(today.year, int(m.group(1)), int(m.group(2))).isoformat())
        except ValueError:
            pass
    if '本日' in text or '今日' in text:
        found.append(today.isoformat())
    if '明日' in text:
        found.append((today + timedelta(days=1)).isoformat())
    return list(dict.fromkeys(found))


def _is_event_text(text: str) -> bool:
    return bool(re.search(
        r'イベント|イベ|特日|ゾロ目|777|新台|高設定|感謝|周年|全台|モーニング', text
    ))


def _save_events(events: list[dict]) -> int:
    if not events:
        return 0
    conn = get_conn()
    saved = 0
    for ev in events:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO hall_event
                  (hall_name, event_date, event_type, event_title, source, source_url)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                ev["hall_name"], ev["event_date"], ev.get("event_type", "その他"),
                ev.get("event_title", "")[:120], ev.get("source", ""), ev.get("source_url", "")
            ))
            saved += conn.execute("SELECT changes()").fetchone()[0]
        except Exception as e:
            print(f"[events] 保存エラー: {e}")
    conn.commit()
    conn.close()
    return saved


def _get(url: str, timeout: int = 12) -> Optional[requests.Response]:
    """シンプルなGETラッパー。失敗時はNone"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        return r if r.status_code == 200 else None
    except Exception as e:
        print(f"[events] GET失敗 {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Dステ (dste.jp)
# ---------------------------------------------------------------------------

def _dste_search(hall_name: str) -> Optional[str]:
    """Dステでホールページを検索"""
    url = f"https://dste.jp/search/?q={urllib.parse.quote(hall_name)}"
    r = _get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.select("a[href*='/pachinko/'], a[href*='/slot/'], a[href*='/hall/']"):
        href = a.get("href", "")
        if hall_name[:4] in (a.get_text() or ""):
            if href.startswith("/"):
                return "https://dste.jp" + href
            return href
    # フォールバック: 最初のホールリンク
    first = soup.select_one("a[href*='/pachinko/osaka/'], a[href*='/slot/osaka/']")
    if first:
        href = first.get("href", "")
        return ("https://dste.jp" + href) if href.startswith("/") else href
    return None


def scrape_dste(hall_name: str) -> list[dict]:
    """Dステからイベント情報を取得"""
    events = []
    hall_url = _dste_search(hall_name)
    if not hall_url:
        print(f"[Dステ] {hall_name}: ホールページ未発見")
        return events

    # イベントページ
    event_url = hall_url.rstrip("/") + "/event/"
    r = _get(event_url)
    if not r:
        print(f"[Dステ] {hall_name}: イベントページ取得失敗")
        return events

    soup = BeautifulSoup(r.text, "html.parser")

    # パターン1: data-date 属性
    for cell in soup.select("[data-date], [data-day]"):
        raw = cell.get("data-date") or cell.get("data-day", "")
        date_str = _parse_jp_date(raw)
        if not date_str:
            if len(raw) == 8 and raw.isdigit():
                date_str = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        if not date_str:
            continue
        for item in cell.select("li, .event, p, span"):
            title = item.get_text(strip=True)
            if title and len(title) > 1:
                events.append({
                    "hall_name": hall_name,
                    "event_date": date_str,
                    "event_type": _classify_event(title),
                    "event_title": title[:120],
                    "source": "dste",
                    "source_url": event_url,
                })

    # パターン2: テーブル / リスト形式
    if not events:
        for row in soup.select("tr, .schedule-row, .event-row, li.event"):
            texts = [td.get_text(strip=True) for td in row.select("td, .col, span")]
            if len(texts) >= 2:
                date_str = _parse_jp_date(texts[0])
                if date_str and texts[1]:
                    events.append({
                        "hall_name": hall_name,
                        "event_date": date_str,
                        "event_type": _classify_event(texts[1]),
                        "event_title": texts[1][:120],
                        "source": "dste",
                        "source_url": event_url,
                    })

    print(f"[Dステ] {hall_name}: {len(events)}件取得")
    return events


# ---------------------------------------------------------------------------
# P-WORLD (p-world.ne.jp)
# ---------------------------------------------------------------------------

def _pworld_search(hall_name: str) -> Optional[str]:
    url = f"https://www.p-world.ne.jp/search.cgi?key={urllib.parse.quote(hall_name)}&pref=27"
    r = _get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.select("a[href*='pachinko-pc']"):
        return "https://www.p-world.ne.jp" + a.get("href", "")
    return None


def scrape_pworld(hall_name: str) -> list[dict]:
    """P-WORLDからイベント・特定日情報を取得"""
    events = []
    hall_url = _pworld_search(hall_name)
    if not hall_url:
        print(f"[P-WORLD] {hall_name}: ページ未発見")
        return events

    r = _get(hall_url)
    if not r:
        return events

    soup = BeautifulSoup(r.text, "html.parser")
    today = date.today()

    # P-WORLDは特定日情報をテキストで表示していることが多い
    for el in soup.select(".tokuteibi, .event, .schedule, .p-info"):
        text = el.get_text(" ", strip=True)
        if not _is_event_text(text):
            continue
        dates = _extract_dates_from_text(text)
        for d in dates or [today.isoformat()]:
            events.append({
                "hall_name": hall_name,
                "event_date": d,
                "event_type": _classify_event(text),
                "event_title": text[:120],
                "source": "pworld",
                "source_url": hall_url,
            })

    print(f"[P-WORLD] {hall_name}: {len(events)}件取得")
    return events


# ---------------------------------------------------------------------------
# Twitter / X  (Nitter 経由)
# ---------------------------------------------------------------------------

def scrape_twitter(hall_name: str) -> list[dict]:
    events = []
    query = urllib.parse.quote(f"{hall_name} イベント OR 特日 OR 新台")

    for instance in NITTER_INSTANCES:
        url = f"{instance}/search?q={query}&f=tweets"
        r = _get(url, timeout=10)
        if not r:
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select(".timeline-item, .tweet")
        if not items:
            print(f"[Twitter] {instance}: ツイートセレクタなし (HTML={r.text[:200]})")
            continue

        for item in items:
            content_el = item.select_one(".tweet-content, .content")
            if not content_el:
                continue
            text = content_el.get_text(" ", strip=True)
            if not _is_event_text(text):
                continue

            date_el = item.select_one(".tweet-date a, time")
            tweet_date_str = None
            if date_el:
                raw = date_el.get("title") or date_el.get("datetime") or date_el.get_text()
                tweet_date_str = _parse_jp_date(raw) or raw[:10]

            tweet_url = ""
            perm_link = item.select_one(".tweet-link, a.permalink")
            if perm_link:
                href = perm_link.get("href", "")
                tweet_url = (instance + href) if href.startswith("/") else href

            ev_dates = _extract_dates_from_text(text)
            if not ev_dates and tweet_date_str:
                ev_dates = [tweet_date_str]

            for ev_date in ev_dates:
                events.append({
                    "hall_name": hall_name,
                    "event_date": ev_date,
                    "event_type": _classify_event(text),
                    "event_title": text[:120],
                    "source": "twitter",
                    "source_url": tweet_url,
                })

        if events:
            print(f"[Twitter] {hall_name}: {instance} で{len(events)}件取得")
            break
        print(f"[Twitter] {instance}: {hall_name} イベントなし")

    return events


# ---------------------------------------------------------------------------
# Google検索スニペット (補完用)
# ---------------------------------------------------------------------------

def scrape_google(hall_name: str) -> list[dict]:
    """Google検索スニペットからイベント情報を補完取得"""
    events = []
    today = date.today()
    queries = [
        f"{hall_name} イベント {today.year}年{today.month}月",
        f"{hall_name} 特定日 スロット",
    ]
    for q in queries:
        url = f"https://www.google.com/search?q={urllib.parse.quote(q)}&hl=ja&num=10"
        r = _get(url, timeout=10)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for el in soup.select(".BNeawe, .VwiC3b, span"):
            text = el.get_text(" ", strip=True)
            if not _is_event_text(text) or len(text) < 10:
                continue
            dates = _extract_dates_from_text(text)
            for d in dates:
                events.append({
                    "hall_name": hall_name,
                    "event_date": d,
                    "event_type": _classify_event(text),
                    "event_title": text[:120],
                    "source": "google",
                    "source_url": url,
                })
        time.sleep(1.5)
    print(f"[Google] {hall_name}: {len(events)}件取得")
    return events


# ---------------------------------------------------------------------------
# 全ソース統合
# ---------------------------------------------------------------------------

def scrape_all(hall_name: str, save: bool = True) -> dict:
    all_events: list[dict] = []
    results = {}
    for scraper_fn, src_name in [
        (scrape_dste,    "dste"),
        (scrape_pworld,  "pworld"),
        (scrape_twitter, "twitter"),
        (scrape_google,  "google"),
    ]:
        try:
            evs = scraper_fn(hall_name)
            all_events.extend(evs)
            results[src_name] = len(evs)
        except Exception as e:
            print(f"[events] {src_name} エラー: {e}")
            results[src_name] = 0
        time.sleep(2)

    saved = _save_events(all_events) if save else 0
    return {"hall_name": hall_name, "total": len(all_events), "saved": saved, "by_source": results}


def scrape_all_halls(hall_list: list, save: bool = True) -> list[dict]:
    results = []
    for h in hall_list:
        hname = h["hall_name"] if isinstance(h, dict) else h
        r = scrape_all(hname, save=save)
        results.append(r)
        time.sleep(3)
    return results
