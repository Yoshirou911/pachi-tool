from pathlib import Path

import desktop_app


def test_user_data_dir_uses_local_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert desktop_app.user_data_dir() == tmp_path / "PachiTool" / "data"


def test_migrate_legacy_databases_never_overwrites(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "current"
    source.mkdir()
    destination.mkdir()
    (source / "sessions.db").write_bytes(b"legacy sessions")
    (source / "opportunities.db").write_bytes(b"legacy opportunities")
    (destination / "sessions.db").write_bytes(b"current sessions")

    migrated = desktop_app.migrate_legacy_databases(destination, source)

    assert migrated == ["opportunities.db"]
    assert (destination / "sessions.db").read_bytes() == b"current sessions"
    assert (destination / "opportunities.db").read_bytes() == b"legacy opportunities"


def test_reserve_local_port_returns_available_port():
    port = desktop_app.reserve_local_port()
    assert 1024 < port <= 65535
