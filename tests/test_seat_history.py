import json
import sqlite3
from datetime import date, timedelta

import pytest

from hall.seat_history import build_seat_history, known_layouts, layout_observations


def row(day, machine="L北斗", **kwargs):
    return {"hall_name": "キコーナ四条畷店", "seat_number": 101, "report_date": day,
            "machine_name": machine, "diff_coins": 100, "games": 4000, **kwargs}


def test_a_b_a_does_not_pool_old_a_and_does_not_mutate_source():
    rows = [row("2026-08-01"), row("2026-08-02", "L東京喰種"), row("2026-08-04")]
    result = build_seat_history(rows, cutoff=date(2026, 8, 5))
    assert len(rows) == 3 and rows[0]["hall_name"] == "キコーナ四条畷店"
    assert [r["report_date"] for r in result["rows"]] == ["2026-08-04"]
    assert result["seats"][0]["changes"] == 2
    assert result["seats"][0]["periods"][-1]["previous_observed"] == "2026-08-02"


def test_missing_diff_non_smartslot_replacement_still_breaks_tenure():
    rows = [row("2026-08-01"), row("2026-08-02", "マイジャグラーV", diff_coins=None), row("2026-08-03")]
    assert len(build_seat_history(rows, cutoff=date(2026, 8, 3))["rows"]) == 1


def test_alias_same_machine_not_a_replacement_and_future_ignored():
    rows = [row("2026-08-01", "LモンキーターンV"), row("2026-08-02", "スマスロモンキーターン5"), row("2026-08-03", "L東京喰種")]
    result = build_seat_history(rows, cutoff=date(2026, 8, 2))
    assert len(result["rows"]) == 2 and result["seats"][0]["changes"] == 0


def test_conflicting_same_day_excludes_until_new_observation():
    rows = [row("2026-08-01"), row("2026-08-02"), row("2026-08-02", "L東京喰種")]
    result = build_seat_history(rows, cutoff=date(2026, 8, 2))
    assert result["rows"] == [] and result["seats"][0]["status"] == "情報矛盾"
    rows.append(row("2026-08-03"))
    assert len(build_seat_history(rows, cutoff=date(2026, 8, 3))["rows"]) == 1


def test_gap_is_not_claimed_as_removal_and_short_gap_is_allowed():
    assert len(build_seat_history([row("2026-08-01"), row("2026-08-10")], cutoff=date(2026, 8, 10))["rows"]) == 2
    result = build_seat_history([row("2026-07-01"), row("2026-08-10")], cutoff=date(2026, 8, 10))
    assert len(result["rows"]) == 1 and result["seats"][0]["periods"][-1]["reason"] == "observation_gap"
    assert build_seat_history([row("2026-07-01")], cutoff=date(2026, 8, 10))["rows"] == []


def test_verified_placement_change_resets_same_machine():
    layouts = [row("2026-08-01", kind="layout", layout_id=1, floor_name="1階", x=10, y=10),
               row("2026-08-03", kind="layout", layout_id=2, floor_name="1階", x=50, y=10)]
    rows = [row("2026-08-02"), row("2026-08-04")]
    result = build_seat_history(rows, layouts, cutoff=date(2026, 8, 5))
    assert [r["report_date"] for r in result["rows"]] == ["2026-08-04"]
    assert result["seats"][0]["periods"][-1]["reason"] == "placement_changed"
    assert result["seats"][0]["position"][2] == 50


def test_expired_map_and_later_new_observation():
    maps = [row("2026-08-01", layout_id=1, x=1, valid_to="2026-08-02")]
    assert not build_seat_history([], maps, cutoff=date(2026, 8, 3))["seats"][0]["usable"]
    result = build_seat_history([row("2026-08-04")], maps, cutoff=date(2026, 8, 4))
    assert result["seats"][0]["usable"] and result["seats"][0]["position"] is None


def test_hall_and_seat_scope_kept_separate():
    rows = [row("2026-08-01"), row("2026-08-02", "L東京喰種", hall_name="別店"), row("2026-08-02", "L東京喰種", seat_number=102)]
    result = build_seat_history(rows, cutoff=date(2026, 8, 2))
    assert len(result["seats"]) == 3 and all(s["changes"] == 0 for s in result["seats"])


def test_no_missing_row_means_removed():
    result = build_seat_history([row("2026-08-01")], cutoff=date(2026, 8, 2))
    assert len(result["rows"]) == 1
    assert "撤去" in result["notice"]


def test_versioned_map_correction_not_visible_before_recording(tmp_path, monkeypatch):
    from api.routers import layout
    path = tmp_path / "history.db"
    monkeypatch.setattr(layout, "HALL_REPORTS_DB", path)
    body = layout.LayoutInput(hall_name="キコーナ四条畷店", valid_from="2026-08-01", verification_status="確認済み",
                              seats=[layout.LayoutSeatInput(seat_number=101, machine_name="L北斗", x=10, y=10)])
    layout.save_layout(body)
    body.seats[0].machine_name = "L東京喰種"
    layout.save_layout(body)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    records = c.execute("SELECT * FROM hall_layout_revision ORDER BY id").fetchall()
    assert len(records) == 2
    assert json.loads(records[0]["payload"])["seats"][0]["machine_name"] == "L北斗"
    assert json.loads(records[1]["payload"])["seats"][0]["machine_name"] == "L東京喰種"
    assert layout_observations(c, date(2026, 8, 30)) == []
    assert layout_observations(c, date.today())[0]["machine_name"] == "L東京喰種"
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM hall_layout_revision")
    c.close()


def test_current_future_valid_from_does_not_hide_known_earlier_revision(tmp_path):
    c = sqlite3.connect(tmp_path / 'revision.db')
    c.executescript('''
        CREATE TABLE hall_layout(id INTEGER, valid_from TEXT);
        CREATE TABLE hall_layout_seat(layout_id INTEGER);
        CREATE TABLE hall_layout_revision(id INTEGER, layout_id INTEGER, payload TEXT, recorded_at TEXT);
        INSERT INTO hall_layout VALUES(1, '2026-10-01');
    ''')
    old = {'id': 1, 'valid_from': '2026-08-01', 'seats': [{'row_name': '通路側', 'row_order': 1}]}
    c.execute('INSERT INTO hall_layout_revision VALUES(1,1,?,?)', (json.dumps(old), '2026-08-01T10:00:00'))
    c.execute('INSERT INTO hall_layout_revision VALUES(2,1,?,?)',
              (json.dumps({**old, 'valid_from': '2026-10-01'}), '2026-09-12T10:00:00'))
    result = known_layouts(c, date(2026, 9, 1))
    assert len(result) == 1 and result[0]['valid_from'] == '2026-08-01'
    assert result[0]['known_at'] == '2026-08-01T10:00:00'
    assert result[0]['seats'][0]['row_order'] == 1
    assert known_layouts(c, date(2026, 9, 13)) == []
    c.close()


def test_unverified_or_generated_map_not_used_for_tenure(tmp_path, monkeypatch):
    from api.routers import layout
    monkeypatch.setattr(layout, "HALL_REPORTS_DB", tmp_path / "m.db")
    layout.save_layout(layout.LayoutInput(hall_name="店", valid_from="2026-08-01",
        seats=[layout.LayoutSeatInput(seat_number=1, machine_name="L北斗", x=0, y=0)]))
    c = layout.init_layout_db()
    assert len(known_layouts(c, date.today())) == 1
    assert layout_observations(c, date.today()) == []
    c.close()
