"""Collect historical smart-slot reports for the supported Nagano halls.

This script intentionally runs one hall at a time and reuses the archive
collector's request delay. Existing report rows are preserved.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scraper import minrepo_archive


DEFAULT_HALLS = (
    "マルハン松本店",
    "KEIZ松本店",
    "ABC松本白板店",
    "APULO塩尻北インター店",
    "キング塩尻店",
)


def _coverage(db_path: Path, hall_name: str) -> dict[str, object]:
    with sqlite3.connect(db_path) as conn:
        machine = conn.execute(
            """SELECT COUNT(DISTINCT report_date), MIN(report_date), MAX(report_date), COUNT(*)
               FROM hall_day_machine
               WHERE hall_name=? AND machine_name!='_NODATA_'""",
            (hall_name,),
        ).fetchone()
        seat = conn.execute(
            """SELECT COUNT(DISTINCT report_date), COUNT(*)
               FROM hall_day_seat WHERE hall_name=?""",
            (hall_name,),
        ).fetchone()
    return {
        "days": machine[0],
        "oldest": machine[1],
        "newest": machine[2],
        "machine_rows": machine[3],
        "seat_days": seat[0],
        "seat_rows": seat[1],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="長野県ホールの公開履歴を順次収集します")
    parser.add_argument("--from", dest="date_from", required=True, help="開始日 YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", required=True, help="終了日 YYYY-MM-DD")
    parser.add_argument("--max-pages", type=int, default=160)
    parser.add_argument("--hall", action="append", choices=DEFAULT_HALLS)
    args = parser.parse_args()

    halls = args.hall or list(DEFAULT_HALLS)
    failures = 0
    for hall_name in halls:
        before = _coverage(Path(minrepo_archive.DB_PATH), hall_name)
        print(f"START {hall_name}: {before}", flush=True)
        try:
            job_id = minrepo_archive.create_job(
                hall_name,
                args.date_from,
                args.date_to,
                max_pages=args.max_pages,
            )
            minrepo_archive.run_job(job_id)
            with minrepo_archive._connect() as conn:
                job = dict(
                    conn.execute(
                        "SELECT * FROM archive_collection_job WHERE id=?", (job_id,)
                    ).fetchone()
                )
            after = _coverage(Path(minrepo_archive.DB_PATH), hall_name)
            print(
                f"DONE {hall_name}: status={job['status']} processed={job['processed']} "
                f"failed={job['failed_count']} added_machine={job['machine_rows']} "
                f"added_seat={job['seat_rows']} coverage={after}",
                flush=True,
            )
            if job["status"] != "completed" or job["failed_count"]:
                failures += 1
        except Exception as exc:
            failures += 1
            print(f"FAILED {hall_name}: {exc}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
