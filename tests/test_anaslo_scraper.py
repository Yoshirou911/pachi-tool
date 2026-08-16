import sqlite3

from bs4 import BeautifulSoup

from scraper import anaslo


def test_scrape_hall_uses_configured_url_override(tmp_path, monkeypatch):
    db_path = tmp_path / "reports.db"
    monkeypatch.setattr(anaslo, "DB_PATH", db_path)
    monkeypatch.setattr(anaslo, "_make_session", lambda cookie_str="": object())

    conn = anaslo.init_db()
    conn.execute(
        "INSERT INTO scrape_hall_config (hall_name, prefecture, url_override) VALUES (?, ?, ?)",
        ("テスト店", "大阪府", "https://ana-slo.com/custom-hall/"),
    )
    conn.commit()
    conn.close()

    requested = []

    def fake_get(_session, url, retry=2):
        requested.append(url)
        return BeautifulSoup("<html><body>日付データなし</body></html>", "lxml")

    monkeypatch.setattr(anaslo, "_get", fake_get)

    assert anaslo.scrape_hall("テスト店", max_days=1) == 0
    assert requested == ["https://ana-slo.com/custom-hall/"]

    conn = sqlite3.connect(db_path)
    status = conn.execute("SELECT status FROM scrape_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.close()
    assert status == "no_data"


def test_scrape_hall_records_access_block_instead_of_leaving_running_log(tmp_path, monkeypatch):
    db_path = tmp_path / "reports.db"
    monkeypatch.setattr(anaslo, "DB_PATH", db_path)
    monkeypatch.setattr(anaslo, "_make_session", lambda cookie_str="": object())

    def blocked(_session, _url, retry=2):
        raise anaslo.CloudflareBlockedError("HTTP 403")

    monkeypatch.setattr(anaslo, "_get", blocked)

    assert anaslo.scrape_hall("テスト店", max_days=1) == 0
    conn = sqlite3.connect(db_path)
    status, error = conn.execute(
        "SELECT status, error_msg FROM scrape_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert status == "cf_blocked"
    assert "HTTP 403" in error
    assert "テスト店" in error


def test_seed_halls_adds_missing_defaults_without_overwriting_existing_settings(tmp_path, monkeypatch):
    db_path = tmp_path / "reports.db"
    monkeypatch.setattr(anaslo, "DB_PATH", db_path)
    anaslo.upsert_hall_config("既存店", "兵庫県", "https://example.com/hall", enabled=False)

    anaslo.seed_hall_configs([
        {"hall_name": "既存店", "prefecture": "大阪府"},
        {"hall_name": "キコーナ四條畷店", "prefecture": "大阪府"},
    ])

    configs = {row["hall_name"]: row for row in anaslo.get_hall_configs()}
    assert configs["既存店"]["prefecture"] == "兵庫県"
    assert configs["既存店"]["url_override"] == "https://example.com/hall"
    assert configs["既存店"]["enabled"] is False
    assert configs["キコーナ四條畷店"]["enabled"] is True
