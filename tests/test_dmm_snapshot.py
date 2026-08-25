from scraper import dmm_snapshot


HTML = """
<html><body>
<a href="https://cdn.p-town.dmm.com/shop_floor_maps/7964/map1.jpg">map</a>
<li class="unit"><h4 class="title">[20] スロ</h4><ul>
  <li class="item"><div class="text"><a href="/machines/4301">スマスロ北斗の拳</a></div><div class="number">4 台</div></li>
  <li class="item"><div class="text"><a href="/machines/4588">ミスタージャグラー</a></div><div class="number">2 台</div></li>
  <li class="item"><div class="text"><a href="/machines/1">対象外ノーマル</a></div><div class="number">1 台</div></li>
</ul></li>
<li class="unit"><h4 class="title">[5] スロ</h4><ul>
  <li class="item"><a href="/machines/4301">スマスロ北斗の拳</a><div class="number">10 台</div></li>
</ul></li>
</body></html>
"""


def test_parse_shop_page_keeps_20_yen_supported_machines_and_map():
    parsed = dmm_snapshot.parse_shop_page(HTML)
    assert parsed["floor_maps"] == ["https://cdn.p-town.dmm.com/shop_floor_maps/7964/map1.jpg"]
    assert {(item["machine_name"], item["unit_count"]) for item in parsed["machines"]} == {
        ("スマスロ北斗の拳", 4),
        ("ミスタージャグラー", 2),
    }


def test_scrape_snapshot_persists_machine_counts_and_floor_map(tmp_path, monkeypatch):
    monkeypatch.setattr(dmm_snapshot, "DB_PATH", tmp_path / "hall.db")
    result = dmm_snapshot.scrape_snapshot(
        "キコーナ四條畷店", snapshot_date="2026-08-25", html=HTML
    )
    assert result == {"machines": 2, "floor_maps": 1}
    conn = dmm_snapshot.init_db()
    assert conn.execute(
        "SELECT unit_count FROM hall_machine_snapshot WHERE machine_name='スマスロ北斗の拳'"
    ).fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM hall_floor_map_snapshot").fetchone()[0] == 1
    conn.close()
