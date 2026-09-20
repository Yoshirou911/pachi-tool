"""
ホールイベント情報スクレーパー v2

対応ソース（優先度順）:
  1. P-WORLD (p-world.ne.jp)  — 店舗公式の公開告知
  2. Dステ (dste.jp)           — 公開イベントカレンダー
  3. SloMap                    — 公開 Schema.org Event
  4. Google fallback           — 未確認候補として隔離

みんレポのホールタグ日は出玉実績の掲載日であり、イベント予定ではないため
収集履歴として保持しつつ予測対象から除外する。

廃止:
  - Twitter/Nitter (ほぼ全インスタンス死亡のため廃止)
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
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


# 公開ページと店舗名の対応が確認できた店舗だけを登録する。
# SloMap の非公開 API は使用せず、検索エンジン向けに公開された JSON-LD のみ読む。
SLOMAP_HALL_URLS = {
    "マルハン大東店": "https://slo-map.com/halls/7950",
    "キコーナ大東店": "https://slo-map.com/halls/7953",
    "ベガスベガス大東店": "https://slo-map.com/halls/7954",
    "スーパーコスモプレミアム大東店": "https://slo-map.com/halls/7955",
    "ニコニコ住道店": "https://slo-map.com/halls/7961",
    "キコーナ四條畷店": "https://slo-map.com/halls/7964",
    "キコーナ野崎店": "https://slo-map.com/halls/7963",
    "ひま・わり四條畷店": "https://slo-map.com/halls/8113",
}


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
            event_kind  TEXT NOT NULL DEFAULT 'unclassified',
            source_trust TEXT NOT NULL DEFAULT '未確認',
            prediction_eligible INTEGER NOT NULL DEFAULT 0,
            classification_reason TEXT,
            created_at  TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(hall_name, event_date, source, event_title)
        )
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hall_event)")}
    additions = {
        "event_type": "TEXT",
        "source": "TEXT",
        "source_url": "TEXT",
        "event_kind": "TEXT NOT NULL DEFAULT 'unclassified'",
        "source_trust": "TEXT NOT NULL DEFAULT '未確認'",
        "prediction_eligible": "INTEGER NOT NULL DEFAULT 0",
        "classification_reason": "TEXT",
        "created_at": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE hall_event ADD COLUMN {name} {definition}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hall_source_day_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            hall_name TEXT NOT NULL,
            report_date TEXT NOT NULL,
            avg_diff_coins INTEGER,
            total_diff_coins INTEGER,
            unit_count INTEGER,
            evidence_scope TEXT NOT NULL,
            source_trust TEXT NOT NULL,
            analysis_eligible INTEGER NOT NULL DEFAULT 0,
            quality_reason TEXT,
            source_url TEXT NOT NULL,
            scraped_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(source, hall_name, report_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hall_event_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            hall_name TEXT NOT NULL,
            event_date TEXT NOT NULL,
            event_name TEXT NOT NULL,
            media_name TEXT,
            event_rank TEXT,
            evidence_scope TEXT NOT NULL,
            avg_diff_coins INTEGER,
            total_diff_coins INTEGER,
            unit_count INTEGER,
            source_trust TEXT NOT NULL,
            analysis_eligible INTEGER NOT NULL DEFAULT 0,
            quality_reason TEXT,
            source_url TEXT NOT NULL,
            scraped_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(source, hall_name, event_date, event_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_model_evaluation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL,
            evaluated_on TEXT NOT NULL,
            region TEXT NOT NULL,
            reference_date TEXT NOT NULL,
            evaluated_trials INTEGER NOT NULL DEFAULT 0,
            recommended_trials INTEGER NOT NULL DEFAULT 0,
            hits INTEGER NOT NULL DEFAULT 0,
            success_pct INTEGER,
            lower_bound_pct INTEGER,
            promotion_status TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(model_name, evaluated_on, region)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_prediction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL,
            prediction_date TEXT NOT NULL,
            target_date TEXT NOT NULL,
            region TEXT NOT NULL,
            hall_name TEXT NOT NULL,
            event_name TEXT NOT NULL,
            decision TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            quality_passed INTEGER NOT NULL DEFAULT 0,
            snapshot_json TEXT NOT NULL DEFAULT '{}',
            outcome_avg_diff_coins INTEGER,
            outcome_success INTEGER,
            outcome_source TEXT,
            resolved_at TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(model_name, target_date, region, hall_name, event_name)
        )
    """)
    _backfill_event_classification(conn)
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

def classify_event_record(
    source: str | None,
    event_title: str | None,
    source_url: str | None = None,
) -> dict:
    """取得元を、予測へ使える予定と単なる実績掲載日に安全側で分ける。"""
    normalized_source = (source or "").strip().lower()
    title = (event_title or "").strip()
    url = (source_url or "").lower()

    if normalized_source == "minrepo" or title.startswith("みんレポ記録日"):
        return {
            "event_kind": "report_day",
            "source_trust": "対象外",
            "prediction_eligible": 0,
            "classification_reason": "実績記事の掲載日であり、イベント告知ではない",
        }
    if normalized_source == "pworld" or "p-world.co.jp" in url:
        return {
            "event_kind": "official_notice",
            "source_trust": "A",
            "prediction_eligible": 1,
            "classification_reason": "店舗公式系ページで確認した予定",
        }
    if normalized_source == "dste":
        return {
            "event_kind": "media_schedule",
            "source_trust": "B",
            "prediction_eligible": 1,
            "classification_reason": "取材・予定媒体の公開ページで確認",
        }
    if normalized_source == "slomap":
        return {
            "event_kind": "media_schedule",
            "source_trust": "C",
            "prediction_eligible": 1,
            "classification_reason": "公開イベント構造化データから取得",
        }
    if normalized_source == "manual":
        return {
            "event_kind": "manual_record",
            "source_trust": "C",
            "prediction_eligible": 1,
            "classification_reason": "利用者が確認して手動登録",
        }
    return {
        "event_kind": "web_candidate",
        "source_trust": "D",
        "prediction_eligible": 0,
        "classification_reason": "単一の未確認情報源のため、予測には未使用",
    }


def _backfill_event_classification(conn: sqlite3.Connection) -> None:
    """旧データを削除せず、分類列だけを付与する可逆な移行。"""
    rows = conn.execute(
        """
        SELECT rowid, source, event_title, source_url
        FROM hall_event
        WHERE event_kind IS NULL OR event_kind='unclassified'
           OR source_trust IS NULL OR source_trust='未確認'
        """
    ).fetchall()
    for row in rows:
        classification = classify_event_record(row[1], row[2], row[3])
        conn.execute(
            """
            UPDATE hall_event
               SET event_kind=?, source_trust=?, prediction_eligible=?,
                   classification_reason=?
             WHERE rowid=?
            """,
            (
                classification["event_kind"],
                classification["source_trust"],
                classification["prediction_eligible"],
                classification["classification_reason"],
                row[0],
            ),
        )

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
            classification = classify_event_record(
                ev.get("source"), ev.get("event_title"), ev.get("source_url")
            )
            conn.execute("""
                INSERT OR IGNORE INTO hall_event
                  (hall_name, event_date, event_type, event_title, source, source_url,
                   event_kind, source_trust, prediction_eligible, classification_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ev["hall_name"], ev["event_date"], ev.get("event_type", "その他"),
                ev.get("event_title", "")[:120], ev.get("source", ""), ev.get("source_url", ""),
                classification["event_kind"], classification["source_trust"],
                classification["prediction_eligible"], classification["classification_reason"], datetime.now(timezone.utc).isoformat(),
            ))
            saved += conn.execute("SELECT changes()").fetchone()[0]
        except Exception as e:
            print(f"[events] 保存エラー: {e}")
    conn.commit()
    conn.close()
    return saved


def _decode_next_flight(html: str) -> str:
    """Next.js が公開HTMLへ埋め込んだRSC文字列を結合する。"""
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


def parse_slomap_day_summaries(
    html: str, hall_name: str, source_url: str
) -> list[dict]:
    """公開HTML内の店舗全体日別差枚を読む。全日0のマスク値は拒否する。"""
    text = _decode_next_flight(html)
    decoder = json.JSONDecoder()
    candidates: list[dict] = []
    for match in re.finditer(r'"dailyMedalDiff":', text):
        try:
            value, _ = decoder.raw_decode(text, match.end())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            candidates.append(value)
    payload = max(candidates, key=len, default={})
    rows: list[dict] = []
    for report_date, raw in payload.items():
        try:
            date.fromisoformat(str(report_date))
        except ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        avg_diff = raw.get("avg_medal_diff")
        total_diff = raw.get("total_medal_diff")
        unit_count = raw.get("total_machines")
        if not isinstance(avg_diff, (int, float)) or not isinstance(total_diff, (int, float)):
            continue
        if not isinstance(unit_count, (int, float)) or int(unit_count) <= 0:
            continue
        rows.append({
            "source": "slomap", "hall_name": hall_name,
            "report_date": str(report_date), "avg_diff_coins": round(avg_diff),
            "total_diff_coins": round(total_diff), "unit_count": int(unit_count),
            "evidence_scope": "hall_all_reported_units", "source_trust": "C",
            "analysis_eligible": 1,
            "quality_reason": "公開ページの店舗全体日別差枚",
            "source_url": source_url,
        })
    if (
        len(rows) >= 20
        and all(row["avg_diff_coins"] == 0 for row in rows)
        and all(row["total_diff_coins"] == 0 for row in rows)
    ):
        return []
    return rows


def parse_slomap_event_evidence(
    html: str, hall_name: str, source_url: str, day_summaries: list[dict]
) -> list[dict]:
    """イベント記録と同日の店舗全体差枚を結び付け、証拠範囲を明示する。"""
    text = _decode_next_flight(html)
    decoder = json.JSONDecoder()
    summaries = {row["report_date"]: row for row in day_summaries}
    found: dict[tuple[str, str], dict] = {}
    for match in re.finditer(r'\{"event_date":', text):
        try:
            raw, _ = decoder.raw_decode(text, match.start())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or raw.get("store_name") != hall_name:
            continue
        event_date = str(raw.get("event_date") or "")[:10]
        event_name = re.sub(r"\s+", " ", str(raw.get("syuzai_name") or "")).strip()
        try:
            date.fromisoformat(event_date)
        except ValueError:
            continue
        if not event_name:
            continue
        summary = summaries.get(event_date)
        eligible = int(summary is not None)
        found[(event_date, event_name)] = {
            "source": "slomap", "hall_name": hall_name, "event_date": event_date,
            "event_name": event_name, "media_name": str(raw.get("media_name") or ""),
            "event_rank": str(raw.get("syuzai_rank") or ""),
            "evidence_scope": "hall_all_reported_units" if eligible else "schedule_only",
            "avg_diff_coins": summary["avg_diff_coins"] if summary else None,
            "total_diff_coins": summary["total_diff_coins"] if summary else None,
            "unit_count": summary["unit_count"] if summary else None,
            "source_trust": "C", "analysis_eligible": eligible,
            "quality_reason": (
                "同日の店舗全体差枚と照合済み" if eligible
                else "開催記録のみ。店舗全体差枚は非公開または欠損"
            ),
            "source_url": source_url,
        }
    return list(found.values())


def _save_slomap_public_data(day_summaries: list[dict], evidence: list[dict]) -> dict:
    conn = get_conn()
    day_saved = 0
    evidence_saved = 0
    for row in day_summaries:
        conn.execute(
            """
            INSERT OR REPLACE INTO hall_source_day_summary
              (source,hall_name,report_date,avg_diff_coins,total_diff_coins,unit_count,
               evidence_scope,source_trust,analysis_eligible,quality_reason,source_url,scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
            """,
            tuple(row[key] for key in (
                "source", "hall_name", "report_date", "avg_diff_coins",
                "total_diff_coins", "unit_count", "evidence_scope", "source_trust",
                "analysis_eligible", "quality_reason", "source_url",
            )),
        )
        day_saved += 1
    for row in evidence:
        conn.execute(
            """
            INSERT OR REPLACE INTO hall_event_evidence
              (source,hall_name,event_date,event_name,media_name,event_rank,evidence_scope,
               avg_diff_coins,total_diff_coins,unit_count,source_trust,analysis_eligible,
               quality_reason,source_url,scraped_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            tuple(row[key] for key in (
                "source", "hall_name", "event_date", "event_name", "media_name",
                "event_rank", "evidence_scope", "avg_diff_coins", "total_diff_coins",
                "unit_count", "source_trust", "analysis_eligible", "quality_reason",
                "source_url",
            )) + (datetime.now(timezone.utc).isoformat(),),
        )
        evidence_saved += 1
    conn.commit()
    conn.close()
    return {"day_summaries": day_saved, "event_evidence": evidence_saved}


def _get(url: str, timeout: int = 15) -> Optional[requests.Response]:
    try:
        r = SESSION.get(url, timeout=timeout)
        return r if r.status_code == 200 else None
    except Exception as e:
        print(f"[events] GET失敗 {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# SloMap AI - 公開 JSON-LD の取材・旧イベント予定
# ---------------------------------------------------------------------------

def parse_slomap_events(html: str, hall_name: str, source_url: str) -> list[dict]:
    """公開ページ内の Schema.org Event だけを正規化して返す。"""
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict] = []

    def iter_objects(value):
        if isinstance(value, list):
            for item in value:
                yield from iter_objects(item)
        elif isinstance(value, dict):
            yield value
            graph = value.get("@graph")
            if graph is not None:
                yield from iter_objects(graph)

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue

        for item in iter_objects(payload):
            item_type = item.get("@type")
            is_event = item_type == "Event" or (
                isinstance(item_type, list) and "Event" in item_type
            )
            if not is_event:
                continue

            raw_date = str(item.get("startDate") or "")[:10]
            try:
                event_date = date.fromisoformat(raw_date).isoformat()
            except ValueError:
                continue

            raw_title = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()
            if not raw_title:
                continue
            # Schema.org 名は「イベント名 - 店舗名」のため、アプリ内では冗長な店舗名を外す。
            title = re.sub(rf"\s*[-－]\s*{re.escape(hall_name)}\s*$", "", raw_title).strip()
            description = re.sub(
                r"\s+", " ", str(item.get("description") or "")
            ).strip()
            rank_match = re.search(r"ランク\s*[:：]\s*([A-Z])", description, re.IGNORECASE)
            if rank_match and "ランク" not in title:
                title = f"{title}（{rank_match.group(1).upper()}ランク）"

            location = item.get("location")
            if isinstance(location, dict):
                published_hall = str(location.get("name") or "").strip()
                if published_hall and published_hall != hall_name:
                    continue

            events.append({
                "hall_name": hall_name,
                "event_date": event_date,
                "event_type": _classify_event(title),
                "event_title": title[:120],
                "source": "slomap",
                "source_url": source_url,
            })

    # 同じJSON-LDが複数箇所に埋め込まれても1件として扱う。
    unique: dict[tuple[str, str], dict] = {}
    for event in events:
        unique.setdefault((event["event_date"], event["event_title"]), event)
    return list(unique.values())


def scrape_slomap_events(hall_name: str) -> list[dict]:
    """対応を確認済みの店舗ページから公開イベント予定を取得する。"""
    source_url = SLOMAP_HALL_URLS.get(hall_name)
    if not source_url:
        return []
    response = _get(source_url)
    if not response:
        return []
    events = parse_slomap_events(response.text, hall_name, source_url)
    day_summaries = parse_slomap_day_summaries(response.text, hall_name, source_url)
    evidence = parse_slomap_event_evidence(
        response.text, hall_name, source_url, day_summaries
    )
    saved = _save_slomap_public_data(day_summaries, evidence)
    print(
        f"[SloMap] {hall_name}: 予定{len(events)}件・"
        f"店舗日次{saved['day_summaries']}件・証拠{saved['event_evidence']}件"
    )
    return events


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
    from scraper.minrepo import build_tag_url, fetch_report_links

    tag_url = build_tag_url(hall_name)
    links = fetch_report_links(tag_url, max_pages=3, expected_hall_name=hall_name)
    if not links:
        print(f"[みんレポ] {hall_name}: 店舗専用データなし")
        return events
    year = date.today().year

    # 検証済みの店舗レポートリンクから日付収集
    for text, report_url in links:
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
                    "source_url": report_url,
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
    overrides = {
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
    if hall_name in overrides:
        return overrides[hall_name]
    for pref in ["27", "28", "20"]:  # 大阪, 兵庫, 長野
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
        (scrape_slomap_events,       "slomap"),
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
