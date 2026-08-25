"""別PC開発用の共有データバックアップと安全な復元。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app_version import APP_VERSION


FORMAT_VERSION = 1
SHARED_DATABASE = "hall_reports.db"
PERSONAL_DATABASES = ("sessions.db", "opportunities.db")
REQUIRED_TABLES = {"hall_day_machine", "scrape_hall_config"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_database(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise ValueError(f"SQLite integrity check failed: {check}")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            raise ValueError(f"Required tables are missing: {', '.join(sorted(missing))}")
        return {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in sorted(REQUIRED_TABLES)
        }
    finally:
        connection.close()


def create_backup(data_dir: Path, output: Path | None = None) -> Path:
    """個人収支を除外し、店舗分析DBだけを一貫したZIPへ保存する。"""
    data_dir = data_dir.resolve()
    source = data_dir / SHARED_DATABASE
    if not source.is_file():
        raise FileNotFoundError(f"Hall analysis database was not found: {source}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = (output or data_dir / "dev-backups" / f"pachi-tool-dev-data-{stamp}.zip").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pachi-dev-backup-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        snapshot = temp_dir / SHARED_DATABASE
        source_connection = sqlite3.connect(source)
        snapshot_connection = sqlite3.connect(snapshot)
        try:
            source_connection.backup(snapshot_connection)
        finally:
            snapshot_connection.close()
            source_connection.close()

        counts = _validate_database(snapshot)
        manifest = {
            "format": "pachi-tool-dev-data",
            "format_version": FORMAT_VERSION,
            "app_version": APP_VERSION,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "files": {
                SHARED_DATABASE: {
                    "sha256": _sha256(snapshot),
                    "size": snapshot.stat().st_size,
                    "required_table_rows": counts,
                }
            },
            "excluded_personal_databases": list(PERSONAL_DATABASES),
        }
        temporary_output = output.with_suffix(output.suffix + ".tmp")
        if temporary_output.exists():
            temporary_output.unlink()
        with zipfile.ZipFile(
            temporary_output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            archive.write(snapshot, SHARED_DATABASE)
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        os.replace(temporary_output, output)
    return output


def restore_backup(archive_path: Path, data_dir: Path) -> tuple[Path, Path | None]:
    """検証済み共有DBを復元し、既存DBがあれば退避する。"""
    archive_path = archive_path.resolve()
    data_dir = data_dir.resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(f"Backup archive was not found: {archive_path}")
    data_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        if names != {SHARED_DATABASE, "manifest.json"}:
            raise ValueError("Backup archive contains an invalid file set")
        manifest = json.loads(archive.read("manifest.json"))
        if (
            manifest.get("format") != "pachi-tool-dev-data"
            or manifest.get("format_version") != FORMAT_VERSION
        ):
            raise ValueError("Unsupported backup format")
        expected = manifest.get("files", {}).get(SHARED_DATABASE, {}).get("sha256")
        if not expected:
            raise ValueError("Backup hash is missing")

        with tempfile.TemporaryDirectory(
            prefix="pachi-dev-restore-", dir=data_dir
        ) as temp_dir_name:
            restored = Path(temp_dir_name) / SHARED_DATABASE
            with archive.open(SHARED_DATABASE) as source, restored.open("wb") as target:
                shutil.copyfileobj(source, target)
            if _sha256(restored) != expected:
                raise ValueError("Backup hash does not match")
            _validate_database(restored)

            destination = data_dir / SHARED_DATABASE
            previous: Path | None = None
            if destination.exists():
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                previous = data_dir / f"{SHARED_DATABASE}.bak-{stamp}"
                os.replace(destination, previous)
            try:
                os.replace(restored, destination)
            except Exception:
                if previous and previous.exists() and not destination.exists():
                    os.replace(previous, destination)
                raise
    return destination, previous


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup", help="共有店舗データをZIPへ保存")
    backup_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    backup_parser.add_argument("--output", type=Path)
    restore_parser = subparsers.add_parser("restore", help="共有店舗データをZIPから復元")
    restore_parser.add_argument("archive", type=Path)
    restore_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()

    if args.command == "backup":
        output = create_backup(args.data_dir, args.output)
        print(f"backup={output}")
        print("Personal sessions and opportunity databases are not included.")
    else:
        destination, previous = restore_backup(args.archive, args.data_dir)
        print(f"restored={destination}")
        if previous:
            print(f"previous={previous}")


if __name__ == "__main__":
    main()
