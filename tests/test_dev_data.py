import json
import sqlite3
import zipfile

import pytest

from scripts.dev_data import create_backup, restore_backup


def _create_reports_db(path, hall_name="テスト店"):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE scrape_hall_config (
            hall_name TEXT PRIMARY KEY, enabled INTEGER NOT NULL
        );
        CREATE TABLE hall_day_machine (
            hall_name TEXT, report_date TEXT, machine_name TEXT,
            avg_diff_coins INTEGER
        );
        """
    )
    connection.execute("INSERT INTO scrape_hall_config VALUES (?, 1)", (hall_name,))
    connection.execute(
        "INSERT INTO hall_day_machine VALUES (?, '2026-08-14', 'L北斗', 500)",
        (hall_name,),
    )
    connection.commit()
    connection.close()


def test_dev_data_backup_excludes_personal_databases_and_restores(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    _create_reports_db(source_dir / "hall_reports.db")
    (source_dir / "sessions.db").write_bytes(b"personal")
    (source_dir / "opportunities.db").write_bytes(b"personal")

    archive_path = create_backup(source_dir, tmp_path / "shared.zip")
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {"hall_reports.db", "manifest.json"}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["excluded_personal_databases"] == [
            "sessions.db",
            "opportunities.db",
        ]

    target_dir = tmp_path / "target"
    destination, previous = restore_backup(archive_path, target_dir)
    assert previous is None
    connection = sqlite3.connect(destination)
    assert connection.execute("SELECT hall_name FROM hall_day_machine").fetchone()[0] == "テスト店"
    connection.close()


def test_dev_data_restore_keeps_previous_database(tmp_path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    _create_reports_db(source_dir / "hall_reports.db", "新しい店")
    _create_reports_db(target_dir / "hall_reports.db", "古い店")

    archive_path = create_backup(source_dir, tmp_path / "shared.zip")
    destination, previous = restore_backup(archive_path, target_dir)

    assert previous is not None and previous.exists()
    connection = sqlite3.connect(destination)
    assert connection.execute("SELECT hall_name FROM hall_day_machine").fetchone()[0] == "新しい店"
    connection.close()
    old_connection = sqlite3.connect(previous)
    assert old_connection.execute("SELECT hall_name FROM hall_day_machine").fetchone()[0] == "古い店"
    old_connection.close()


def test_dev_data_restore_rejects_tampered_archive(tmp_path):
    archive_path = tmp_path / "tampered.zip"
    manifest = {
        "format": "pachi-tool-dev-data",
        "format_version": 1,
        "files": {"hall_reports.db": {"sha256": "0" * 64}},
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("hall_reports.db", b"not a database")
        archive.writestr("manifest.json", json.dumps(manifest))

    with pytest.raises(ValueError, match="hash"):
        restore_backup(archive_path, tmp_path / "target")
