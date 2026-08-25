from datetime import date
import sqlite3

from scraper import pekasen


HTML = """
<!doctype html><html><head>
<title>ニコニコ住道店のマイジャグラーV設定推測データ | ペカセン</title>
</head><body>
<div class="dayblock">
  <div class="day-head">8/24 （ 月 ） 3 台</div>
  <div class="table-wrap"><div class="scroller"><table>
    <tr><th>台番</th><th>G数</th><th>BIG</th><th>REG</th><th>合成</th><th>差枚</th><th>ブドウ逆算</th></tr>
    <tr><td>395</td><td>6,000</td><td>26</td><td>25</td><td>1/118</td><td>+1,250</td><td>1/5.8</td></tr>
    <tr><td>396</td><td>2,400</td><td>7</td><td>4</td><td>1/218</td><td>-850</td><td>−</td></tr>
    <tr><td>平均</td><td>4,200</td><td>16</td><td>14</td><td>1/140</td><td>+200</td><td>1/5.9</td></tr>
  </table></div></div>
</div>
<div class="dayblock">
  <div class="day-head">12/31 （ 水 ） 1 台</div>
  <table>
    <tr><th>台番</th><th>G数</th><th>BIG</th><th>REG</th><th>合成</th><th>差枚</th></tr>
    <tr><td>395</td><td>5,000</td><td>20</td><td>20</td><td>1/125</td><td>500</td></tr>
  </table>
</div>
</body></html>
"""


def test_parse_machine_page_extracts_counts_and_rolls_year_back():
    rows = pekasen.parse_machine_page(
        HTML,
        "ニコニコ住道店",
        "マイジャグラーV",
        "https://pekasen.com/store/nikoniko-suminodou/myjuggler-v",
        as_of=date(2026, 8, 25),
    )
    assert len(rows) == 3
    first = next(row for row in rows if row["seat_number"] == 395 and row["report_date"] == "2026-08-24")
    assert first["games"] == 6000
    assert first["bb_count"] == 26
    assert first["rb_count"] == 25
    assert first["diff_coins"] == 1250
    assert any(row["report_date"] == "2025-12-31" for row in rows)


def test_parse_machine_page_rejects_wrong_hall_or_machine():
    assert pekasen.parse_machine_page(
        HTML, "別店舗", "マイジャグラーV", "https://source", as_of=date(2026, 8, 25)
    ) == []
    assert pekasen.parse_machine_page(
        HTML, "ニコニコ住道店", "ゴーゴージャグラー3", "https://source", as_of=date(2026, 8, 25)
    ) == []


def test_save_rows_persists_raw_counts_and_analysis_probabilities(tmp_path):
    rows = pekasen.parse_machine_page(
        HTML,
        "ニコニコ住道店",
        "マイジャグラーV",
        "https://source",
        as_of=date(2026, 8, 25),
    )
    database = tmp_path / "hall.db"
    assert pekasen.save_rows(rows, database) == 3
    conn = sqlite3.connect(database)
    raw = conn.execute(
        "SELECT games,bb_count,rb_count FROM hall_source_juggler_daily WHERE report_date='2026-08-24' AND seat_number=395"
    ).fetchone()
    analysis = conn.execute(
        "SELECT games,bb_prob,rb_prob,source FROM hall_day_seat WHERE report_date='2026-08-24' AND seat_number=395"
    ).fetchone()
    conn.close()
    assert raw == (6000, 26, 25)
    assert analysis[0] == 6000
    assert analysis[1] == 26 / 6000
    assert analysis[2] == 25 / 6000
    assert analysis[3] == "pekasen"


def test_save_rows_does_not_overwrite_manual_bonus_counts(tmp_path):
    database = tmp_path / "hall.db"
    conn = pekasen.init_db(database)
    conn.execute(
        """INSERT INTO hall_day_seat
             (hall_name,report_date,machine_name,seat_number,diff_coins,games,bb_prob,rb_prob,source,source_url)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("ニコニコ住道店", "2026-08-24", "マイジャグラーV", 395, 999, 6000, 0.01, 0.02, "manual", "manual"),
    )
    conn.commit()
    conn.close()
    rows = pekasen.parse_machine_page(
        HTML, "ニコニコ住道店", "マイジャグラーV", "https://source", as_of=date(2026, 8, 25)
    )
    assert pekasen.save_rows(rows, database) == 2
    conn = sqlite3.connect(database)
    row = conn.execute(
        "SELECT diff_coins,bb_prob,rb_prob,source FROM hall_day_seat WHERE report_date='2026-08-24' AND seat_number=395"
    ).fetchone()
    raw_count = conn.execute("SELECT COUNT(*) FROM hall_source_juggler_daily").fetchone()[0]
    conn.close()
    assert row == (999, 0.01, 0.02, "manual")
    assert raw_count == 3


def test_refresh_throttle_uses_raw_collection_timestamp(tmp_path):
    database = tmp_path / "hall.db"
    conn = pekasen.init_db(database)
    conn.execute(
        """INSERT INTO hall_source_juggler_daily
             (source,hall_name,report_date,machine_name,seat_number,games,bb_count,rb_count,diff_coins,source_url)
           VALUES ('pekasen','店','2026-08-24','マイジャグラーV',1,1000,3,3,0,'https://source')"""
    )
    conn.commit()
    conn.close()
    assert pekasen.is_refresh_due(database, max_age_hours=18) is False
