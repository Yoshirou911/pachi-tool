import sqlite3

from scraper import pworld_snapshot


HTML = """
<html><body>
  <a href="/machine/database/10207">Ｌ　東京喰種</a>
  <a href="/machine/database/9786">スマスロ北斗の拳</a>
  <a href="/machine/database/9786">スマスロ北斗の拳</a>
  <a href="/machine/database/1234">Ｐ大海物語</a>
  <a href="/other/999">L ダミー</a>
</body></html>
"""


def test_parse_machine_links_keeps_only_unique_smartslot():
    rows = pworld_snapshot.parse_machine_links(HTML)
    assert rows == [
        {"machine_name": "Ｌ 東京喰種", "machine_id": "10207"},
        {"machine_name": "スマスロ北斗の拳", "machine_id": "9786"},
    ]


def test_scrape_snapshot_persists_daily_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "reports.db"
    monkeypatch.setattr(pworld_snapshot, "DB_PATH", db_path)
    count = pworld_snapshot.scrape_snapshot(
        "キコーナ四條畷店",
        snapshot_date="2026-08-09",
        html=HTML,
    )
    assert count == 2
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT machine_name, machine_id FROM hall_machine_snapshot ORDER BY id"
        ).fetchall()
    assert rows == [("Ｌ 東京喰種", "10207"), ("スマスロ北斗の拳", "9786")]


def test_empty_page_is_not_saved(tmp_path, monkeypatch):
    db_path = tmp_path / "reports.db"
    monkeypatch.setattr(pworld_snapshot, "DB_PATH", db_path)
    assert pworld_snapshot.scrape_snapshot(
        "キコーナ四條畷店", snapshot_date="2026-08-09", html="<html></html>"
    ) == 0
    assert not db_path.exists()
