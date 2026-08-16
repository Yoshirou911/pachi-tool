"""Merge a validated Nagano history package into the application database."""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


HALLS = {
    "キング塩尻店",
    "マルハン松本店",
    "ABC松本白板店",
    "ラッシュMATSUMOTO#59",
    "KEIZ松本店",
    "APULO塩尻北インター店",
}


def _validated_rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    if len(rows) > 50_000:
        raise ValueError(f"too many rows in {table}")
    for row in rows:
        hall_name, report_date = row[0], row[1]
        source_url = row[-2]
        if hall_name not in HALLS or not ("2020-01-01" <= report_date <= "2030-12-31"):
            raise ValueError(f"invalid scope: {hall_name} {report_date}")
        parsed = urlparse(source_url or "")
        if parsed.scheme != "https" or parsed.hostname != "min-repo.com":
            raise ValueError(f"invalid source URL: {source_url}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("patch", type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    patch = sqlite3.connect(args.patch)
    try:
        machines = _validated_rows(patch, "hall_day_machine")
        seats = _validated_rows(patch, "hall_day_seat")
    finally:
        patch.close()

    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = args.target.with_name(f"{args.target.name}.bak-{stamp}")
        source_conn = sqlite3.connect(args.target)
        backup_conn = sqlite3.connect(backup)
        try:
            source_conn.backup(backup_conn)
        finally:
            source_conn.close()
            backup_conn.close()
        print(f"backup={backup}")

    target = sqlite3.connect(args.target, timeout=60)
    try:
        target.execute("BEGIN IMMEDIATE")
        target.executemany(
            """INSERT INTO hall_day_machine
               (hall_name,report_date,machine_name,unit_count,avg_diff_coins,avg_games,
                win_rate_pct,ev_pct,source_url,scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(hall_name,report_date,machine_name) DO UPDATE SET
                 unit_count=excluded.unit_count, avg_diff_coins=excluded.avg_diff_coins,
                 avg_games=excluded.avg_games, win_rate_pct=excluded.win_rate_pct,
                 ev_pct=excluded.ev_pct, source_url=excluded.source_url,
                 scraped_at=excluded.scraped_at""",
            machines,
        )
        target.executemany(
            """INSERT INTO hall_day_seat
               (hall_name,report_date,machine_name,seat_number,diff_coins,games,ev_pct,
                bb_prob,rb_prob,source,source_url,scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(hall_name,report_date,machine_name,seat_number) DO UPDATE SET
                 diff_coins=excluded.diff_coins, games=excluded.games, ev_pct=excluded.ev_pct,
                 bb_prob=excluded.bb_prob, rb_prob=excluded.rb_prob, source=excluded.source,
                 source_url=excluded.source_url, scraped_at=excluded.scraped_at""",
            seats,
        )
        target.commit()
        print(f"imported machine={len(machines)} seat={len(seats)}")
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()


if __name__ == "__main__":
    main()
