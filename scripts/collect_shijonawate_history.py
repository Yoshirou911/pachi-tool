"""四條畷駅周辺のスマスロ・ジャグラー公開履歴を重点収集する。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scraper import anoslot_public, minrepo_archive
from hall.regions import SHIJONAWATE_AREA_HALLS


ARCHIVE_HALLS = (
    "キコーナ大東店",
    "ニコニコ住道店",
    "ベガスベガス大東店",
    "スーパーコスモプレミアム大東店",
)


def coverage() -> list[dict]:
    with sqlite3.connect(minrepo_archive.DB_PATH) as conn:
        rows = conn.execute(
            """SELECT hall_name,
                      COUNT(DISTINCT report_date) AS days,
                      COUNT(*) AS rows,
                      SUM(CASE WHEN machine_name LIKE '%ジャグラー%' THEN 1 ELSE 0 END) AS juggler_rows,
                      MIN(report_date), MAX(report_date)
                 FROM hall_day_machine
                WHERE hall_name IN (?,?,?,?,?,?,?,?)
                GROUP BY hall_name ORDER BY hall_name""",
            tuple(SHIJONAWATE_AREA_HALLS),
        ).fetchall()
    return [
        {
            "hall_name": row[0], "days": row[1], "rows": row[2],
            "juggler_rows": row[3], "oldest": row[4], "newest": row[5],
        }
        for row in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="四條畷駅周辺の公開履歴を重点収集します")
    parser.add_argument("--from", dest="date_from", default="2025-08-01")
    parser.add_argument("--to", dest="date_to", default="2026-08-24")
    parser.add_argument("--max-pages", type=int, default=160)
    parser.add_argument("--hall", action="append", choices=ARCHIVE_HALLS)
    parser.add_argument("--public-only", action="store_true")
    args = parser.parse_args()

    print(json.dumps(anoslot_public.scrape_all(), ensure_ascii=False, indent=2), flush=True)
    if not args.public_only:
        for hall_name in args.hall or ARCHIVE_HALLS:
            job_id = minrepo_archive.create_job(
                hall_name, args.date_from, args.date_to, max_pages=args.max_pages
            )
            minrepo_archive.run_job(job_id)
            print(json.dumps(minrepo_archive.get_status(), ensure_ascii=False), flush=True)
    print(json.dumps(coverage(), ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
