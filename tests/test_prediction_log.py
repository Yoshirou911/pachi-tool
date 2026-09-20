import copy
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from hall.prediction_log import JST, initialize, production_summary, resolve_outcomes, save_batch


@pytest.fixture
def conn(tmp_path):
    db = sqlite3.connect(tmp_path / "reports.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE hall_day_machine(hall_name TEXT,report_date TEXT,machine_name TEXT,
            avg_diff_coins REAL,unit_count INTEGER,avg_games REAL,source_url TEXT);
        CREATE TABLE hall_day_seat(hall_name TEXT,report_date TEXT,machine_name TEXT,
            seat_number INTEGER,diff_coins REAL,games INTEGER,source_url TEXT);
    """)
    yield db
    db.close()


def forecast():
    item = {"scope": "machine", "hall_name": "キコーナ四條畷店", "machine_name": "L北斗",
            "seat_number": 0, "projected": 123, "probability_pct": 70, "action": "狙う",
            "quality": {"analysis_eligible": True}}
    return {"visit_date": "2026-09-09", "region": "shijonawate", "input_cutoff_date": "2026-09-07",
            "forecast_subjects": [item, {**item, "scope": "seat", "seat_number": 123}],
            "frozen_inputs": {"machine_rows": [{"report_date": "2026-09-07", "avg_diff_coins": 90}]}}


NOW = datetime(2026, 9, 8, 13, tzinfo=JST)


def save(conn, prediction=None):
    return save_batch(conn, prediction or forecast(), version="3.23.0", started_at=NOW, now=NOW)


def machine_actual(conn, *, diff=0, day="2026-09-09", name="L北斗"):
    conn.execute("INSERT INTO hall_day_machine VALUES (?,?,?,?,?,?,?)",
                 ("キコーナ四条畷店", day, name, diff, 10, 4000, "https://example.com/result"))
    conn.commit()


def test_save_idempotent_and_freezes_original_payload(conn):
    assert save(conn)["recorded"] == 2
    changed = forecast()
    changed["forecast_subjects"][0]["probability_pct"] = 99
    assert save(conn, changed)["status"] == "already_saved"
    payload = json.loads(conn.execute("SELECT payload FROM prediction_batch").fetchone()[0])
    assert payload["forecast_subjects"][0]["probability_pct"] == 70
    assert payload["frozen_inputs"]["machine_rows"][0]["avg_diff_coins"] == 90
    for table in ["prediction_batch", "prediction_subject"]:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(f"UPDATE {table} SET id=id")


def test_event_challenger_context_is_frozen_without_changing_subjects(conn):
    prediction = forecast()
    prediction['event_studies'] = [{'policy':'hall-event-matched-weekday-v1', 'model_influence_eligible':False}]
    prediction['frozen_inputs']['event_study_records'] = [{'event_title':'取材', 'known_at':'2026-09-01T10:00:00+09:00'}]
    save(conn, prediction)
    prediction['event_studies'][0]['policy'] = 'changed'
    assert save(conn, prediction)['status'] == 'already_saved'
    stored = json.loads(conn.execute('SELECT payload FROM prediction_batch').fetchone()[0])
    assert stored['event_studies'][0]['policy'] == 'hall-event-matched-weekday-v1'
    assert stored['frozen_inputs']['event_study_records'][0]['known_at'].endswith('+09:00')
    assert stored['forecast_subjects'][0]['projected'] == 123


def test_machine_challenger_study_is_frozen(conn):
    prediction = forecast()
    prediction['machine_studies'] = [{'policy':'hall-machine-peer-shrink-v1', 'model_influence_eligible':False}]
    save(conn, prediction)
    prediction['machine_studies'][0]['policy'] = 'modified'
    assert save(conn, prediction)['status'] == 'already_saved'
    stored = json.loads(conn.execute('SELECT payload FROM prediction_batch').fetchone()[0])
    assert stored['machine_studies'][0]['policy'] == 'hall-machine-peer-shrink-v1'


def test_seat_ranks_and_identity_inputs_are_frozen_without_changing_subjects(conn):
    prediction = forecast()
    prediction['seat_ranking_studies'] = [{'policy':'same-machine-seat-peer-shrink-v1', 'ranked_seats':2}]
    prediction['frozen_inputs']['seat_ranking_observations'] = [{'report_date':'2026-09-07','machine_name':'マイジャグラーV','diff_coins':None}]
    prediction['frozen_inputs']['seat_ranking_layouts_by_cutoff'] = {'2026-09-07':[]}
    save(conn, prediction)
    prediction['seat_ranking_studies'][0]['ranked_seats'] = 99
    assert save(conn, prediction)['status'] == 'already_saved'
    stored = json.loads(conn.execute('SELECT payload FROM prediction_batch').fetchone()[0])
    assert stored['seat_ranking_studies'][0]['ranked_seats'] == 2
    assert stored['frozen_inputs']['seat_ranking_observations'][0]['diff_coins'] is None
    assert stored['forecast_subjects'][0]['projected'] == 123


def test_placement_research_and_dated_inputs_are_frozen(conn):
    prediction = forecast()
    prediction['placement_studies'] = [{'policy': 'verified-layout-peer-groups-v1', 'model_influence_eligible': False}]
    prediction['frozen_inputs']['placement_maps_by_cutoff'] = {'2026-09-07': [
        {'known_at': '2026-09-01', 'seats': [{'seat_number': 123, 'row_name': '通路側', 'row_order': 1}]}]}
    prediction['frozen_inputs']['confirmed_setting_records'] = [
        {'known_at': '2026-09-05', 'confirmed_setting': 4}]
    save(conn, prediction)
    prediction['placement_studies'][0]['model_influence_eligible'] = True
    prediction['frozen_inputs']['placement_maps_by_cutoff']['2026-09-07'][0]['seats'][0]['row_order'] = 99
    assert save(conn, prediction)['status'] == 'already_saved'
    stored = json.loads(conn.execute('SELECT payload FROM prediction_batch').fetchone()[0])
    assert stored['placement_studies'][0]['model_influence_eligible'] is False
    assert stored['frozen_inputs']['placement_maps_by_cutoff']['2026-09-07'][0]['seats'][0]['row_order'] == 1
    assert stored['frozen_inputs']['confirmed_setting_records'][0]['known_at'] == '2026-09-05'
    assert stored['forecast_subjects'] == forecast()['forecast_subjects']


@pytest.mark.parametrize("target", ["2026-09-07", "2026-09-08", "2026-09-10"])
def test_cannot_save_past_today_or_arbitrary_future(conn, target):
    prediction = forecast()
    prediction["visit_date"] = target
    with pytest.raises(ValueError):
        save(conn, prediction)


def test_reject_intraday_input_and_midnight_rollover(conn):
    prediction = forecast()
    prediction["input_cutoff_date"] = "2026-09-08"
    with pytest.raises(ValueError):
        save(conn, prediction)
    with pytest.raises(ValueError):
        save_batch(conn, forecast(), version="3.23", started_at=NOW, now=NOW+timedelta(days=1))


def test_missing_vs_zero_and_no_same_day_resolve(conn):
    save(conn)
    machine_actual(conn, diff=None)
    assert resolve_outcomes(conn, today=date(2026, 9, 10)) == 0
    conn.execute("DELETE FROM hall_day_machine")
    machine_actual(conn, diff=0)
    assert resolve_outcomes(conn, today=date(2026, 9, 9)) == 0
    assert resolve_outcomes(conn, today=date(2026, 9, 10)) == 1
    assert resolve_outcomes(conn, today=date(2026, 9, 10)) == 0
    summary = production_summary(conn)
    machine = summary["by_scope"][0]
    assert machine["recommended_resolved"] == 1 and machine["hits"] == 0
    assert machine["brier_score"] == .49
    assert summary["by_scope"][1]["pending"] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE prediction_outcome SET actual=100")


def test_exact_subject_matching_and_conflict_quarantine(conn):
    save(conn)
    machine_actual(conn, diff=100, day="2026-09-08")
    machine_actual(conn, diff=100, name="L東京喰種")
    conn.execute("INSERT INTO hall_day_seat VALUES (?,?,?,?,?,?,?)",
                 ("キコーナ四條畷店", "2026-09-09", "L東京喰種", 123, 100, 4000, "source"))
    conn.commit()
    assert resolve_outcomes(conn, today=date(2026, 9, 10)) == 0
    machine_actual(conn, diff=100)
    machine_actual(conn, diff=-100)
    assert resolve_outcomes(conn, today=date(2026, 9, 10)) == 0


def test_no_recommendations_not_zero_accuracy_and_reference_resolves(conn):
    prediction = forecast()
    for item in prediction["forecast_subjects"]:
        item["action"] = "見送り"
    save(conn, prediction)
    machine_actual(conn, diff=123)
    resolve_outcomes(conn, today=date(2026, 9, 10))
    result = production_summary(conn)
    assert result["by_scope"][0]["resolved"] == 1
    assert result["by_scope"][0]["success_pct"] is None
    assert result["by_scope"][0]["lower_bound_pct"] is None


def test_atomic_batch_rollback_on_duplicate_subject(conn):
    prediction = forecast()
    prediction["forecast_subjects"].append(copy.deepcopy(prediction["forecast_subjects"][0]))
    with pytest.raises((sqlite3.IntegrityError, ValueError)):
        save(conn, prediction)
    assert conn.execute("SELECT COUNT(*) FROM prediction_batch").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM prediction_subject").fetchone()[0] == 0


def test_get_summary_does_not_create_tables(conn):
    before = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    assert production_summary(conn)["batches"] == 0
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == before


def test_reinitializing_preserves_history(conn):
    save(conn)
    initialize(conn)
    assert production_summary(conn)["batches"] == 1
