"""Export and safely merge public hall history for one configured region."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hall.regions import MATSUMOTO_SHIOJIRI_HALLS, SHIJONAWATE_AREA_HALLS


REGION_HALLS = {
    "shijonawate": SHIJONAWATE_AREA_HALLS,
    "matsumoto_shiojiri": MATSUMOTO_SHIOJIRI_HALLS,
}
ALLOWED_SOURCE_HOSTS = {
    "anoslot.moe",
    "min-repo.com",
    "pachireview.com",
    "pekasen.com",
}
MACHINE_COLUMNS = (
    "hall_name", "report_date", "machine_name", "unit_count",
    "avg_diff_coins", "avg_games", "win_rate_pct", "ev_pct",
    "source_url", "scraped_at",
)
SEAT_COLUMNS = (
    "hall_name", "report_date", "machine_name", "seat_number",
    "diff_coins", "games", "ev_pct", "bb_prob", "rb_prob",
    "source", "source_url", "scraped_at",
)


def _placeholders(values: set[str] | frozenset[str]) -> str:
    return ",".join("?" for _ in values)


def _create_patch_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE sync_manifest (
            region TEXT PRIMARY KEY,
            exported_at TEXT NOT NULL,
            machine_rows INTEGER NOT NULL,
            seat_rows INTEGER NOT NULL
        );
        CREATE TABLE hall_day_machine (
            hall_name TEXT NOT NULL, report_date TEXT NOT NULL,
            machine_name TEXT NOT NULL, unit_count INTEGER,
            avg_diff_coins INTEGER, avg_games INTEGER, win_rate_pct REAL,
            ev_pct REAL, source_url TEXT, scraped_at TEXT,
            UNIQUE(hall_name, report_date, machine_name)
        );
        CREATE TABLE hall_day_seat (
            hall_name TEXT NOT NULL, report_date TEXT NOT NULL,
            machine_name TEXT NOT NULL, seat_number INTEGER,
            diff_coins INTEGER, games INTEGER, ev_pct REAL, bb_prob REAL,
            rb_prob REAL, source TEXT, source_url TEXT, scraped_at TEXT,
            UNIQUE(hall_name, report_date, machine_name, seat_number)
        );
        """
    )


def export_region(source_path: Path, output_path: Path, region: str) -> tuple[int, int]:
    halls = REGION_HALLS[region]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    source = sqlite3.connect(source_path)
    patch = sqlite3.connect(output_path)
    try:
        _create_patch_schema(patch)
        hall_args = tuple(sorted(halls))
        placeholders = _placeholders(halls)
        machines = source.execute(
            f"SELECT {','.join(MACHINE_COLUMNS)} FROM hall_day_machine "
            f"WHERE hall_name IN ({placeholders})",
            hall_args,
        ).fetchall()
        seats = source.execute(
            f"SELECT {','.join(SEAT_COLUMNS)} FROM hall_day_seat "
            f"WHERE hall_name IN ({placeholders}) AND seat_number > 0",
            hall_args,
        ).fetchall()
        patch.executemany(
            f"INSERT INTO hall_day_machine VALUES ({','.join('?' for _ in MACHINE_COLUMNS)})",
            machines,
        )
        patch.executemany(
            f"INSERT INTO hall_day_seat VALUES ({','.join('?' for _ in SEAT_COLUMNS)})",
            seats,
        )
        patch.execute(
            "INSERT INTO sync_manifest VALUES (?,?,?,?)",
            (region, datetime.now().isoformat(timespec="seconds"), len(machines), len(seats)),
        )
        patch.commit()
        return len(machines), len(seats)
    finally:
        source.close()
        patch.close()


def _validated_rows(
    patch: sqlite3.Connection, table: str, columns: tuple[str, ...], region: str
) -> list[tuple]:
    halls = REGION_HALLS[region]
    rows = patch.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
    if len(rows) > 100_000:
        raise ValueError(f"too many rows in {table}")
    for row in rows:
        hall_name, report_date = row[0], row[1]
        source_url = row[-2]
        parsed = urlparse(source_url or "")
        if hall_name not in halls or not ("2020-01-01" <= report_date <= "2030-12-31"):
            raise ValueError(f"invalid scope: {hall_name} {report_date}")
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_SOURCE_HOSTS:
            raise ValueError(f"invalid source URL: {source_url}")
    return rows


def import_region(
    patch_path: Path, target_path: Path, region: str, *, backup: bool = True
) -> tuple[int, int, Path | None]:
    patch = sqlite3.connect(patch_path)
    try:
        integrity = patch.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"patch integrity check failed: {integrity}")
        manifest = patch.execute(
            "SELECT machine_rows,seat_rows FROM sync_manifest WHERE region=?", (region,)
        ).fetchone()
        if manifest is None:
            raise ValueError(f"manifest for {region} was not found")
        machines = _validated_rows(patch, "hall_day_machine", MACHINE_COLUMNS, region)
        seats = _validated_rows(patch, "hall_day_seat", SEAT_COLUMNS, region)
        if manifest != (len(machines), len(seats)):
            raise ValueError("manifest row counts do not match patch contents")
    finally:
        patch.close()

    backup_path = None
    if backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = target_path.with_name(f"{target_path.name}.bak-{stamp}")
        source_conn = sqlite3.connect(target_path)
        backup_conn = sqlite3.connect(backup_path)
        try:
            source_conn.backup(backup_conn)
        finally:
            source_conn.close()
            backup_conn.close()

    target = sqlite3.connect(target_path, timeout=60)
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
                 scraped_at=excluded.scraped_at
               WHERE COALESCE(excluded.scraped_at,'') >= COALESCE(hall_day_machine.scraped_at,'')""",
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
                 source_url=excluded.source_url, scraped_at=excluded.scraped_at
               WHERE COALESCE(excluded.scraped_at,'') >= COALESCE(hall_day_seat.scraped_at,'')""",
            seats,
        )
        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()
    return len(machines), len(seats), backup_path


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--source", type=Path, default=Path("data/hall_reports.db"))
    export_parser.add_argument("--output", type=Path, required=True)
    export_parser.add_argument("--region", choices=REGION_HALLS, required=True)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("patch", type=Path)
    import_parser.add_argument("--target", type=Path, required=True)
    import_parser.add_argument("--region", choices=REGION_HALLS, required=True)
    import_parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    if args.command == "export":
        machines, seats = export_region(args.source, args.output, args.region)
        print(f"exported machine={machines} seat={seats} output={args.output}")
    else:
        machines, seats, backup_path = import_region(
            args.patch, args.target, args.region, backup=not args.no_backup
        )
        print(f"imported machine={machines} seat={seats} backup={backup_path}")


if __name__ == "__main__":
    main()
