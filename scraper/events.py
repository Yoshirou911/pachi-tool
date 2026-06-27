"""
ホールイベント情報スクレーパー

対応ソース:
  - みんパチ (minpachi.jp)
  - パチタウン (pachitown系)
  - Twitter / X  (Nitter経由 — 複数インスタンスをフォールバック)
  - Facebook 公開ページ
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
}

NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://xcancel.com",
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
    """日本語日付テキスト → YYYY-MM-DD"""
    year = base_year or date.today().year
    # YYYY-MM-DD or YYYY/MM/DD
    m = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            pass
    # MM/DD or M月D日
    m = re.search(r'(\d{1,2})[/月](\d{1,2})', text)
    if m:
        try:
            return date(year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            pass
    return None


def _extract_dates_from_text(text: str) -> list[str]:
    """本文中のイベント日付を全て抽出"""
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
    return list(dict.fromkeys(found))  # 重複除去・順序保持


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


# ---------------------------------------------------------------------------
# みんパチ
# ---------------------------------------------------------------------------

def _minpachi_hall_url(hall_name: str) -> Optional[str]:
    url = f"https://minpachi.jp/search/?name={urllib.parse.quote(hall_name)}&type=pachinko"
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[href*='/hall/'], a[href*='/pachinko/']"):
            href = a.get("href", "")
            if href and "search" not in href:
                if href.startswith("/"):
                    return "https://minpachi.jp" + href
                return href
    except Exception as e:
        print(f"[みんパチ] 検索エラー: {e}")
    return None


def scrape_minpachi(hall_name: str, month: Optional[str] = None) -> list[dict]:
    """みんパチのイベントカレンダーを取得"""
    events = []
    hall_url = _minpachi_hall_url(hall_name)
    if not hall_url:
        print(f"[みんパチ] {hall_name}: ホールページ未発見")
        return events

    base = hall_url.rstrip("/")
    ym_param = ""
    if month:
        ym_param = f"?ym={month.replace('-', '')}"
    event_url = f"{base}/event/{ym_param}"

    try:
        r = requests.get(event_url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")

        # パターン1: data-date 属性付きセル
        for cell in soup.select("[data-date]"):
            date_str = cell.get("data-date", "")
            if len(date_str) == 8:
                date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            if not re.match(r'\d{4}-\d{2}-\d{2}', date_str):
                continue
            for item in cell.select(".event, li, p"):
                title = item.get_text(strip=True)
                if title:
                    events.append({
                        "hall_name": hall_name,
                        "event_date": date_str,
                        "event_type": _classify_event(title),
                        "event_title": title,
                        "source": "minpachi",
                        "source_url": event_url,
                    })

        # パターン2: テーブル形式
        if not events:
            for row in soup.select("tr, .event-row"):
                cols = row.select("td, .event-col")
                if len(cols) >= 2:
                    date_str = _parse_jp_date(cols[0].get_text(strip=True))
                    title = cols[1].get_text(strip=True) if len(cols) > 1 else ""
                    if date_str and title:
                        events.append({
                            "hall_name": hall_name,
                            "event_date": date_str,
                            "event_type": _classify_event(title),
                            "event_title": title,
                            "source": "minpachi",
                            "source_url": event_url,
                        })

    except Exception as e:
        print(f"[みんパチ] {hall_name} エラー: {e}")

    print(f"[みんパチ] {hall_name}: {len(events)}件取得")
    return events


# ---------------------------------------------------------------------------
# パチタウン系
# ---------------------------------------------------------------------------

PACHITOWN_BASES = [
    "https://slot.dmmpachitown.com",
    "https://pachislot.pachitown.com",
]


def scrape_pachitown(hall_name: str) -> list[dict]:
    events = []
    for base in PACHITOWN_BASES:
        search_url = f"{base}/search/?keyword={urllib.parse.quote(hall_name)}"
        try:
            r = requests.get(search_url, headers=HEADERS, timeout=12)
            soup = BeautifulSoup(r.text, "html.parser")
            hall_link = soup.select_one(".shop-name a, .hall-name a, h2 a, h3 a")
            if not hall_link:
                continue
            href = hall_link.get("href", "")
            hall_url = (base + href) if href.startswith("/") else href

            r2 = requests.get(hall_url, headers=HEADERS, timeout=12)
            soup2 = BeautifulSoup(r2.text, "html.parser")

            for item in soup2.select(".event-item, .schedule-item, .campaign-item"):
                date_el = item.select_one("[class*='date'], time, .date")
                title_el = item.select_one("[class*='title'], .name, p")
                if not date_el:
                    continue
                date_str = _parse_jp_date(date_el.get_text(strip=True))
                title = title_el.get_text(strip=True) if title_el else item.get_text(strip=True)[:80]
                if date_str and title:
                    events.append({
                        "hall_name": hall_name,
                        "event_date": date_str,
                        "event_type": _classify_event(title),
                        "event_title": title,
                        "source": "pachitown",
                        "source_url": hall_url,
                    })
            if events:
                break
        except Exception as e:
            print(f"[パチタウン] {base} エラー: {e}")
            continue

    print(f"[パチタウン] {hall_name}: {len(events)}件取得")
    return events


# ---------------------------------------------------------------------------
# Twitter / X  (Nitter 経由)
# ---------------------------------------------------------------------------

def scrape_twitter(hall_name: str) -> list[dict]:
    events = []
    query = urllib.parse.quote(f"{hall_name} イベント OR 特日 OR 新台")

    for instance in NITTER_INSTANCES:
        url = f"{instance}/search?q={query}&f=tweets"
        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")

            for item in soup.select(".timeline-item, .tweet"):
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

        except Exception as e:
            print(f"[Twitter] {instance} エラー: {e}")
            continue

    return events


# ---------------------------------------------------------------------------
# Facebook 公開ページ
# ---------------------------------------------------------------------------

def scrape_facebook(hall_name: str) -> list[dict]:
    """Facebook公開ページ検索でイベントを探す（モバイル版）"""
    events = []
    query = urllib.parse.quote(f"{hall_name} パチンコ スロット イベント")
    url = f"https://m.facebook.com/search/top/?q={query}"
    try:
        headers = {**HEADERS, "Accept": "text/html"}
        r = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        for post in soup.select("[data-store], article, ._5rgt"):
            text = post.get_text(" ", strip=True)
            if not _is_event_text(text):
                continue
            dates = _extract_dates_from_text(text)
            link_el = post.select_one("a[href]")
            src_url = ""
            if link_el:
                href = link_el.get("href", "")
                src_url = "https://m.facebook.com" + href if href.startswith("/") else href
            for d in dates:
                events.append({
                    "hall_name": hall_name,
                    "event_date": d,
                    "event_type": _classify_event(text),
                    "event_title": text[:120],
                    "source": "facebook",
                    "source_url": src_url,
                })
    except Exception as e:
        print(f"[Facebook] {hall_name} エラー: {e}")

    print(f"[Facebook] {hall_name}: {len(events)}件取得")
    return events


# ---------------------------------------------------------------------------
# 全ソース統合
# ---------------------------------------------------------------------------

def scrape_all(hall_name: str, save: bool = True) -> dict:
    """全ソースからイベントを収集してDBに保存"""
    all_events: list[dict] = []

    results = {}
    for scraper_fn, src_name in [
        (scrape_minpachi, "minpachi"),
        (scrape_pachitown, "pachitown"),
        (scrape_twitter, "twitter"),
        (scrape_facebook, "facebook"),
    ]:
        try:
            evs = scraper_fn(hall_name)
            all_events.extend(evs)
            results[src_name] = len(evs)
        except Exception as e:
            print(f"[events] {src_name} 全体エラー: {e}")
            results[src_name] = 0
        time.sleep(2)

    saved = _save_events(all_events) if save else 0
    return {"hall_name": hall_name, "total": len(all_events), "saved": saved, "by_source": results}


def scrape_all_halls(hall_list: list, save: bool = True) -> list[dict]:
    """全ホールを一括スクレイプ"""
    results = []
    for h in hall_list:
        hname = h["hall_name"] if isinstance(h, dict) else h
        r = scrape_all(hname, save=save)
        results.append(r)
        time.sleep(3)
    return results
