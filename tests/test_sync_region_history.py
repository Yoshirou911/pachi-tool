import sqlite3
from pathlib import Path

from scripts.sync_region_history import export_region, import_region


def _seed_database(path: Path, avg_diff: int = 100) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE hall_day_machine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name TEXT NOT NULL, report_date TEXT NOT NULL, machine_name TEXT NOT NULL,
            unit_count INTEGER, avg_diff_coins INTEGER, avg_games INTEGER,
            win_rate_pct REAL, ev_pct REAL, source_url TEXT, scraped_at TEXT,
            UNIQUE(hall_name,report_date,machine_name)
        );
        CREATE TABLE hall_day_seat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hall_name TEXT NOT NULL, report_date TEXT NOT NULL, machine_name TEXT NOT NULL,
            seat_number INTEGER, diff_coins INTEGER, games INTEGER, ev_pct REAL,
            bb_prob REAL, rb_prob REAL, source TEXT, source_url TEXT, scraped_at TEXT,
            UNIQUE(hall_name,report_date,machine_name,seat_number)
        );
        """
    )
    conn.execute(
        "INSERT INTO hall_day_machine(hall_name,report_date,machine_name,unit_count,avg_diff_coins,avg_games,win_rate_pct,ev_pct,source_url,scraped_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("ニコニコ住道店", "2026-08-20", "L北斗", 4, avg_diff, 5000, 50, 101, "https://min-repo.com/test", "2026-08-21T00:00:00"),
    )
    conn.execute(
        "INSERT INTO hall_day_seat(hall_name,report_date,machine_name,seat_number,diff_coins,games,ev_pct,bb_prob,rb_prob,source,source_url,scraped_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ニコニコ住道店", "2026-08-20", "L北斗", 325, 800, 6000, 103, None, None, "minrepo", "https://min-repo.com/test", "2026-08-21T00:00:00"),
    )
    conn.commit()
    conn.close()


def test_region_history_export_and_import(tmp_path: Path):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    patch = tmp_path / "patch.db"
    _seed_database(source, 300)
    _seed_database(target, 100)

    assert export_region(source, patch, "shijonawate") == (1, 1)
    machines, seats, backup = import_region(patch, target, "shijonawate")

    assert (machines, seats) == (1, 1)
    assert backup is not None and backup.exists()
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT avg_diff_coins FROM hall_day_machine").fetchone()[0] == 300
        assert conn.execute("SELECT seat_number FROM hall_day_seat").fetchone()[0] == 325
    finally:
        conn.close()
