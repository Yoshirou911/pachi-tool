"""
ホールイベント情報スクレーパー v2

対応ソース（優先度順）:
  1. みんレポ (min-repo.com)   — ホールタグページからイベント日を推定
  2. Dステ (dste.jp)           — ホール公式イベントカレンダー
  3. P-WORLD (p-world.ne.jp)  — 特定日情報
  4. やすだのスロット日記系サイト / Google fallback

廃止:
  - Twitter/Nitter (ほぼ全インスタンス死亡のため廃止)
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

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


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
    if re.search(r'7+|７+|セブン', text):
        return "特日7"
    if re.search(r'感謝|記念|周年|誕生|バースデー', text):
        return "感謝デー"
    if re.search(r'朝イチ|朝一|モーニング', text):
        return "朝イチ"
    if re.search(r'高設定|設定示唆|全台|全機種|6確|5確', text):
        return "高設定示唆"
    if re.search(r'イベント|イベ|特定日|特日', text):
        return "通常イベント"
    return "その他"


def _parse_jp_date(text: str, base_year: Optional[int] = None) -> Optional[str]:
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
            mo, dy = int(m.group(1)), int(m.group(2))
            # 年またぎ補正
            y = year if mo >= date.today().month - 1 else year + 1
            return date(y, mo, dy).isoformat()
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
            mo, dy = int(m.group(1)), int(m.group(2))
            y = today.year if mo >= today.month - 1 else today.year + 1
            found.append(date(y, mo, dy).isoformat())
        except ValueError:
            pass
    if '本日' in text or '今日' in text:
        found.append(today.isoformat())
    if '明日' in text:
        found.append((today + timedelta(days=1)).isoformat())
    return list(dict.fromkeys(found))


def _is_event_text(text: str) -> bool:
    return bool(re.search(
        r'イベント|イベ|特日|特定日|ゾロ目|777|新台|高設定|感謝|周年|全台|モーニング|6確|5確', text
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


def _get(url: str, timeout: int = 15) -> Optional[requests.Response]:
    try:
        r = SESSION.get(url, timeout=timeout)
        return r if r.status_code == 200 else None
    except Exception as e:
        print(f"[events] GET失敗 {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# 1. みんレポ (min-repo.com) — 差枚データのある日をイベント候補として推定
# ---------------------------------------------------------------------------

def scrape_minrepo_events(hall_name: str) -> list[dict]:
    """
    みんレポのホールタグページから、データが記録されている日を取得する。
    差枚データがある日 = 営業日として、週末・特定日付を「通常営業日」として記録。
    また、機種別平均差枚が全体より明らかに高い日をイベント候補として返す。
    """
    events = []
    base = "https://min-repo.com"
    tag_url = f"{base}/tag/{urllib.parse.quote(hall_name)}/"
    r = _get(tag_url)
    if not r:
        print(f"[みんレポ] {hall_name}: タグページ取得失敗")
        return events

    soup = BeautifulSoup(r.text, "html.parser")
    year = date.today().year

    # レポートリンクから日付収集
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        # みんレポの日付テキスト: "6/25(木)" 形式
        if re.match(r'\d+/\d+[（(][月火水木金土日][）)]', text):
            date_str = _parse_jp_date(text, year)
            if not date_str:
                continue
            # 日曜・土曜・特定日（1日・7日など末尾）は候補として登録
            d = date.fromisoformat(date_str)
            dow = d.weekday()  # 0=月 6=日
            day = d.day
            is_special = (dow >= 5) or (day % 7 == 0) or (day in [1, 7, 11, 14, 17, 21, 22, 25, 28])
            if is_special:
                events.append({
                    "hall_name": hall_name,
                    "event_date": date_str,
                    "event_type": "通常イベント",
                    "event_title": f"みんレポ記録日（{text}）",
                    "source": "minrepo",
                    "source_url": tag_url,
                })

    print(f"[みんレポ] {hall_name}: {len(events)}件候補")
    return events


# ---------------------------------------------------------------------------
# 2. Dステ (dste.jp)
# ---------------------------------------------------------------------------

def _dste_search(hall_name: str) -> Optional[str]:
    """Dステでホールページを検索（複数戦略）"""
    # 戦略1: 検索API
    url = f"https://dste.jp/search/?q={urllib.parse.quote(hall_name)}&type=hall"
    r = _get(url)
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        # 検索結果の最初のホールリンク
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            label = a.get_text(strip=True)
            if hall_name[:3] in label and re.search(r'/hall/\d+/', href):
                return ("https://dste.jp" + href) if href.startswith("/") else href

    # 戦略2: 直接URLパターン試行
    encoded = urllib.parse.quote(hall_name)
    for pref_code in ["osaka", "27"]:
        guess = f"https://dste.jp/hall/search/?pref={pref_code}&name={encoded}"
        r2 = _get(guess)
        if r2 and r2.status_code == 200:
            soup2 = BeautifulSoup(r2.text, "html.parser")
            a2 = soup2.select_one("a[href*='/hall/']")
            if a2:
                href = a2.get("href", "")
                return ("https://dste.jp" + href) if href.startswith("/") else href
    return None


def scrape_dste(hall_name: str) -> list[dict]:
    """Dステからイベント情報を取得（複数パターン対応）"""
    events = []
    hall_url = _dste_search(hall_name)
    if not hall_url:
        print(f"[Dステ] {hall_name}: ホールページ未発見")
        return events

    # イベントページを試す
    for event_path in ["/event/", "/schedule/", "/tokuteibi/"]:
        event_url = hall_url.rstrip("/") + event_path
        r = _get(event_url)
        if not r:
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        # パターン1: data-date / data-day 属性
        for cell in soup.select("[data-date], [data-day]"):
            raw = cell.get("data-date") or cell.get("data-day", "")
            date_str = _parse_jp_date(raw)
            if not date_str:
                if len(raw) == 8 and raw.isdigit():
                    date_str = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
            if not date_str:
                continue
            for item in cell.select("li, .event, .schedule-item, p"):
                title = item.get_text(strip=True)
                if title and len(title) > 1 and _is_event_text(title):
                    events.append({
                        "hall_name": hall_name,
                        "event_date": date_str,
                        "event_type": _classify_event(title),
                        "event_title": title[:120],
                        "source": "dste",
                        "source_url": event_url,
                    })

        # パターン2: カレンダー形式（日付セル + テキスト）
        if not events:
            for td in soup.select("td, .cal-cell, .day-cell"):
                day_el = td.select_one(".day, .date, [class*='day-num'], [class*='date-num']")
                if not day_el:
                    continue
                day_text = day_el.get_text(strip=True)
                date_str = _parse_jp_date(day_text)
                if not date_str:
                    continue
                for item in td.select(".event, li, p, span.badge"):
                    title = item.get_text(strip=True)
                    if title and len(title) > 1 and _is_event_text(title):
                        events.append({
                            "hall_name": hall_name,
                            "event_date": date_str,
                            "event_type": _classify_event(title),
                            "event_title": title[:120],
                            "source": "dste",
                            "source_url": event_url,
                        })

        # パターン3: テーブル行（日付 | イベント名）
        if not events:
            for row in soup.select("table tr"):
                tds = row.find_all(["td", "th"])
                if len(tds) < 2:
                    continue
                date_str = _parse_jp_date(tds[0].get_text(strip=True))
                title = tds[1].get_text(strip=True)
                if date_str and title and _is_event_text(title):
                    events.append({
                        "hall_name": hall_name,
                        "event_date": date_str,
                        "event_type": _classify_event(title),
                        "event_title": title[:120],
                        "source": "dste",
                        "source_url": event_url,
                    })

        if events:
            break

    print(f"[Dステ] {hall_name}: {len(events)}件取得")
    return events


# ---------------------------------------------------------------------------
# 3. P-WORLD (p-world.ne.jp)
# ---------------------------------------------------------------------------

def _pworld_search(hall_name: str) -> Optional[str]:
    """P-WORLDでホールページを検索（大阪府=27）"""
    for pref in ["27", "28"]:  # 大阪, 兵庫
        url = f"https://www.p-world.ne.jp/search.cgi?key={urllib.parse.quote(hall_name)}&pref={pref}&type=slot"
        r = _get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[href*='pachinko-pc'], a[href*='slot-pc'], a[href*='hall']"):
            href = a.get("href", "")
            if hall_name[:3] in a.get_text():
                return ("https://www.p-world.ne.jp" + href) if href.startswith("/") else href
        # フォールバック: 最初のホールリンク
        first = soup.select_one("a[href*='pachinko-pc'], a[href*='slot-pc']")
        if first:
            href = first.get("href", "")
            return ("https://www.p-world.ne.jp" + href) if href.startswith("/") else href
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

    # P-WORLDの特定日テーブル（.tokuteibi-table や .p-event-list 等）
    for el in soup.select(".tokuteibi, .event, .schedule, .p-info, .p-event, table.event-table tr"):
        text = el.get_text(" ", strip=True)
        if not _is_event_text(text):
            continue
        dates = _extract_dates_from_text(text)
        for d in (dates or [today.isoformat()]):
            events.append({
                "hall_name": hall_name,
                "event_date": d,
                "event_type": _classify_event(text),
                "event_title": text[:120],
                "source": "pworld",
                "source_url": hall_url,
            })

    # 特定日ページを追加チェック
    tokutei_url = hall_url.rstrip("/") + "/tokuteibi/"
    r2 = _get(tokutei_url)
    if r2:
        soup2 = BeautifulSoup(r2.text, "html.parser")
        for row in soup2.select("tr"):
            tds = row.find_all(["td", "th"])
            if len(tds) >= 2:
                date_str = _parse_jp_date(tds[0].get_text(strip=True))
                title = tds[1].get_text(strip=True)
                if date_str and title and _is_event_text(title):
                    events.append({
                        "hall_name": hall_name,
                        "event_date": date_str,
                        "event_type": _classify_event(title),
                        "event_title": title[:120],
                        "source": "pworld",
                        "source_url": tokutei_url,
                    })

    print(f"[P-WORLD] {hall_name}: {len(events)}件取得")
    return events


# ---------------------------------------------------------------------------
# 4. スロドル / パチンコ店イベント検索 (Google Custom Search fallback)
# ---------------------------------------------------------------------------

def scrape_google_fallback(hall_name: str) -> list[dict]:
    """
    Google検索スニペットからイベント情報を補完。
    ボット検知を避けるため最小限のリクエストのみ。
    """
    events = []
    today = date.today()
    query = f"{hall_name} 特定日 イベント {today.year}年{today.month}月"
    url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&hl=ja&num=5"

    try:
        r = SESSION.get(url, timeout=10, headers={
            **HEADERS,
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        })
        if r.status_code != 200:
            return events
        soup = BeautifulSoup(r.text, "html.parser")
        # スニペットテキスト抽出
        for el in soup.select(".BNeawe, .VwiC3b, .s3v9rd, span.aCOpRe"):
            text = el.get_text(" ", strip=True)
            if not _is_event_text(text) or len(text) < 8:
                continue
            dates = _extract_dates_from_text(text)
            for d in dates:
                # 未来または直近の日付のみ
                try:
                    if date.fromisoformat(d) >= today - timedelta(days=7):
                        events.append({
                            "hall_name": hall_name,
                            "event_date": d,
                            "event_type": _classify_event(text),
                            "event_title": text[:120],
                            "source": "google",
                            "source_url": url,
                        })
                except ValueError:
                    pass
    except Exception as e:
        print(f"[Google] {hall_name}: {e}")

    print(f"[Google] {hall_name}: {len(events)}件取得")
    return events


# ---------------------------------------------------------------------------
# 全ソース統合
# ---------------------------------------------------------------------------

def scrape_all(hall_name: str, save: bool = True) -> dict:
    all_events: list[dict] = []
    results = {}

    scrapers = [
        (scrape_dste,              "dste"),
        (scrape_pworld,            "pworld"),
        (scrape_minrepo_events,    "minrepo"),
        (scrape_google_fallback,   "google"),
    ]

    for scraper_fn, src_name in scrapers:
        try:
            evs = scraper_fn(hall_name)
            all_events.extend(evs)
            results[src_name] = len(evs)
        except Exception as e:
            print(f"[events] {src_name} エラー: {e}")
            results[src_name] = 0
        time.sleep(1.5)

    # 重複除去: 同日に同ソースからの同タイトルは除く
    seen: set[tuple] = set()
    unique: list[dict] = []
    for ev in all_events:
        key = (ev["hall_name"], ev["event_date"], ev.get("event_title", "")[:40])
        if key not in seen:
            seen.add(key)
            unique.append(ev)

    saved = _save_events(unique) if save else 0
    total = len(unique)
    print(f"[events] {hall_name}: 計{total}件（重複除去後）, {saved}件新規保存")
    return {"hall_name": hall_name, "total": total, "saved": saved, "by_source": results}


def scrape_all_halls(hall_list: list, save: bool = True) -> list[dict]:
    results = []
    for h in hall_list:
        hname = h["hall_name"] if isinstance(h, dict) else h
        r = scrape_all(hname, save=save)
        results.append(r)
        time.sleep(2)
    return results
