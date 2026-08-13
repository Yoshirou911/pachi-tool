"""Create a small, validated SQLite package for syncing Nagano history."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


HALLS = (
    "キング塩尻店",
    "マルハン松本店",
    "ABC松本白板店",
    "ラッシュMATSUMOTO#59",
    "KEIZ松本店",
    "APULO塩尻北インター店",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("data/hall_reports.db"))
    parser.add_argument("--output", type=Path, default=Path("data/nagano_history_patch.db"))
    parser.add_argument("--from", dest="date_from", default="2026-05-01")
    parser.add_argument("--to", dest="date_to", default="2026-08-12")
    parser.add_argument("--hall", action="append", choices=HALLS)
    args = parser.parse_args()

    halls = tuple(args.hall or HALLS)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        args.output.unlink()
    source = sqlite3.connect(args.source)
    patch = sqlite3.connect(args.output)
    try:
        patch.executescript(
            """
            CREATE TABLE hall_day_machine (
                hall_name TEXT NOT NULL, report_date TEXT NOT NULL, machine_name TEXT NOT NULL,
                unit_count INTEGER, avg_diff_coins INTEGER, avg_games INTEGER,
                win_rate_pct REAL, ev_pct REAL, source_url TEXT, scraped_at TEXT,
                UNIQUE(hall_name, report_date, machine_name)
            );
            CREATE TABLE hall_day_seat (
                hall_name TEXT NOT NULL, report_date TEXT NOT NULL, machine_name TEXT NOT NULL,
                seat_number INTEGER, diff_coins INTEGER, games INTEGER, ev_pct REAL,
                bb_prob REAL, rb_prob REAL, source TEXT, source_url TEXT, scraped_at TEXT,
                UNIQUE(hall_name, report_date, machine_name, seat_number)
            );
            """
        )
        placeholders = ",".join("?" for _ in halls)
        machine_rows = source.execute(
            f"""SELECT hall_name,report_date,machine_name,unit_count,avg_diff_coins,avg_games,
                       win_rate_pct,ev_pct,source_url,scraped_at
                FROM hall_day_machine
                WHERE hall_name IN ({placeholders}) AND report_date BETWEEN ? AND ?""",
            (*halls, args.date_from, args.date_to),
        ).fetchall()
        seat_rows = source.execute(
            f"""SELECT hall_name,report_date,machine_name,seat_number,diff_coins,games,ev_pct,
                       bb_prob,rb_prob,
                       CASE WHEN source_url LIKE 'https://min-repo.com/%' THEN 'minrepo' ELSE source END,
                       source_url,scraped_at
                FROM hall_day_seat
                WHERE hall_name IN ({placeholders}) AND report_date BETWEEN ? AND ?""",
            (*halls, args.date_from, args.date_to),
        ).fetchall()
        patch.executemany(
            "INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?,?,?,?,?)", machine_rows
        )
        patch.executemany(
            "INSERT INTO hall_day_seat VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", seat_rows
        )
        patch.commit()
        print(f"exported machine={len(machine_rows)} seat={len(seat_rows)} to {args.output}")
    finally:
        source.close()
        patch.close()


if __name__ == "__main__":
    main()
