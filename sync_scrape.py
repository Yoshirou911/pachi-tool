"""
ローカルスクレイプ → Fly.io 自動アップロード

使い方:
    python sync_scrape.py

Windows タスクスケジューラに登録すると毎晩自動実行できる。
"""
import sys
import json
import sqlite3
import urllib.request
import urllib.error
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).parent))

SERVER_URL = "https://pachi-tool.fly.dev"
LOCAL_DB = Path(__file__).parent / "hall_reports.db"

HALLS = [
    {"hall_name": "ベガスベガス大東店",             "prefecture": "大阪府"},
    {"hall_name": "マルハン大東店",                 "prefecture": "大阪府"},
    {"hall_name": "ニコニコ住道店",                 "prefecture": "大阪府"},
    {"hall_name": "スーパーコスモプレミアム大東店", "prefecture": "大阪府"},
    {"hall_name": "マルハン枚方店",                 "prefecture": "大阪府"},
    {"hall_name": "ニコニコ枚方店",                 "prefecture": "大阪府"},
    {"hall_name": "ベガビック1700枚方店",           "prefecture": "大阪府"},
    {"hall_name": "G-ONE枚方宮之阪店",             "prefecture": "大阪府"},
    {"hall_name": "キコーナ寝屋川南店",             "prefecture": "大阪府"},
    {"hall_name": "ニコニコ寝屋川南インター店",     "prefecture": "大阪府"},
    {"hall_name": "マルハン寝屋川店",               "prefecture": "大阪府"},
    {"hall_name": "ベラジオ寝屋川店",               "prefecture": "大阪府"},
    {"hall_name": "ニコニコ寝屋川店スロット館",     "prefecture": "大阪府"},
    {"hall_name": "123交野店",                      "prefecture": "大阪府"},
    {"hall_name": "キコーナ守口店",                 "prefecture": "大阪府"},
    {"hall_name": "テキサス門真",                   "prefecture": "大阪府"},
]


def scrape_all(days: int = 5) -> int:
    """全ホールをスクレイプしてローカルDBに保存。成功件数を返す。"""
    from scraper.anaslo import scrape_hall
    total = 0
    for h in HALLS:
        name = h["hall_name"]
        pref = h["prefecture"]
        print(f"[スクレイプ] {name} ...", end=" ", flush=True)
        try:
            result = scrape_hall(name, prefecture=pref, max_days=days, unlimited=True)
            count = result if isinstance(result, int) else 0
            print(f"OK ({count}件)")
            total += count
        except Exception as e:
            print(f"NG: {e}")
    return total


def upload_to_server(days_back: int = 7) -> dict:
    """ローカルDBの最新データをサーバーに送信。"""
    if not LOCAL_DB.exists():
        print(f"[アップロード] ローカルDB未発見: {LOCAL_DB}")
        return {}

    since = (date.today() - timedelta(days=days_back)).isoformat()
    conn = sqlite3.connect(LOCAL_DB)
    rows = conn.execute("""
        SELECT hall_name, report_date, machine_name, seat_number,
               diff_coins, games, ev_pct, bb_prob, rb_prob
        FROM hall_day_seat
        WHERE report_date >= ?
        ORDER BY report_date DESC
    """, (since,)).fetchall()
    conn.close()

    if not rows:
        print(f"[アップロード] {since} 以降のデータなし")
        return {}

    payload = [
        {
            "hall_name": r[0], "report_date": r[1], "machine_name": r[2],
            "seat_number": r[3], "diff_coins": r[4], "games": r[5],
            "ev_pct": r[6], "bb_prob": r[7], "rb_prob": r[8],
        }
        for r in rows
    ]

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{SERVER_URL}/api/scrape/upload",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print(f"[アップロード] {len(payload)}件 → {SERVER_URL} ...", end=" ", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            result = json.loads(res.read())
            print(f"OK (挿入:{result.get('inserted', '?')} / スキップ:{result.get('skipped', '?')})")
            return result
    except urllib.error.HTTPError as e:
        print(f"NG: HTTP {e.code} {e.reason}")
        return {}
    except Exception as e:
        print(f"NG: {e}")
        return {}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ローカルスクレイプ → サーバー同期")
    parser.add_argument("--days", type=int, default=5, help="スクレイプ日数 (default: 5)")
    parser.add_argument("--upload-only", action="store_true", help="スクレイプをスキップしてアップロードのみ")
    parser.add_argument("--scrape-only", action="store_true", help="アップロードをスキップ")
    args = parser.parse_args()

    if not args.upload_only:
        print(f"=== スクレイプ開始 ({args.days}日分) ===")
        scrape_all(days=args.days)

    if not args.scrape_only:
        print("=== サーバーへアップロード ===")
        upload_to_server(days_back=args.days + 2)

    print("=== 完了 ===")
