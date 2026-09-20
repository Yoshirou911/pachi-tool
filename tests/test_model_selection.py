import copy
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hall import model_selection as selection
from hall.prediction_log import JST, save_batch, resolve_outcomes, initialize

HALL = 'キコーナ四條畷店'
MACHINE = 'L北斗'


def prediction(day='2026-09-09'):
    cutoff = (date.fromisoformat(day)-timedelta(days=2)).isoformat()
    candidate = {'predicted_coins': 100, 'baseline_coins': 1000, 'input_cutoff_date': cutoff}
    profile = {'machine_name': MACHINE, 'machine_key': 'L北斗', 'candidate': candidate, 'validation': {'trials': []}}
    return {'visit_date': day, 'region': 'shijonawate', 'input_cutoff_date': cutoff,
            'forecast_subjects': [{'scope': 'machine', 'hall_name': HALL, 'machine_name': MACHINE, 'seat_number': 0,
                                   'projected': 1100, 'probability_pct': 70, 'action': '見送り', 'quality': {'analysis_eligible': False}}],
            'frozen_inputs': {'machine_rows': [{'hall_name': HALL, 'machine_name': MACHINE, 'report_date': cutoff,
                                               'avg_diff_coins': 100, 'unit_count': 5, 'avg_games': 4000}]},
            'machine_studies': [{'hall_name': HALL, 'profiles': [profile]}]}


def samples(n=70):
    return [{'family': 'machine', 'hall_name': HALL, 'target_date': (date(2026, 6, 22)+timedelta(days=i)).isoformat(),
             'models': {'candidate': 100, 'baseline': 1000, 'current': 1100}, 'actual': 0}
            for i in range(n)]


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path/'review.db')
    c.row_factory = sqlite3.Row
    c.executescript('''CREATE TABLE hall_day_machine(hall_name TEXT,report_date TEXT,machine_name TEXT,
        avg_diff_coins REAL,unit_count INTEGER,avg_games REAL,source_url TEXT);
        CREATE TABLE hall_day_seat(hall_name TEXT,report_date TEXT,machine_name TEXT,seat_number INTEGER,
        diff_coins REAL,games INTEGER,source_url TEXT);''')
    yield c
    c.close()


def save(c, p=None):
    p = p or prediction()
    now = datetime.combine(date.fromisoformat(p['visit_date'])-timedelta(days=1), datetime.min.time(), JST)+timedelta(hours=13)
    return save_batch(c, p, version='3.30.0', started_at=now, now=now)


def actual(c, games=4000):
    c.execute('INSERT INTO hall_day_machine VALUES(?,?,?,?,?,?,?)', (HALL, '2026-09-09', MACHINE, 0, 5, games, 'https://example.com'))
    c.commit()
    # Simulated outcomes must be recorded during the simulated review period,
    # not at the wall-clock date when this test happens to run.
    from hall import prediction_log
    class OutcomeClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 9, 10, 12, tzinfo=JST)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    with pytest.MonkeyPatch.context() as clock_patch:
        clock_patch.setattr(prediction_log, 'datetime', OutcomeClock)
        resolve_outcomes(c, today=date(2026, 9, 10))


def test_freeze_does_not_mutate_and_keeps_exact_current_scope():
    p = prediction()
    old = copy.deepcopy(p)
    frozen = selection.freeze_selection(p)
    assert p == old
    assert frozen['samples'][0]['models'] == {'candidate': 100, 'baseline': 1000, 'current': 1100}
    p['forecast_subjects'] = []
    assert selection.freeze_selection(p)['samples'][0]['models']['current'] is None


def test_all_four_families_and_group_members_are_preserved():
    p = prediction()
    candidate = p['machine_studies'][0]['profiles'][0]['candidate']
    p['event_studies'] = [{'hall_name': HALL, 'profiles': [{'event_name': '旧イベ', **candidate}]}]
    p['seat_ranking_studies'] = [{'hall_name': HALL, 'machines': [{'profiles': [
        {'machine_name': MACHINE, 'machine_key': 'L北斗', 'seat_number': 101, 'segment_id': 'period', 'candidate': candidate}]}]}]
    p['placement_studies'] = [{'hall_name': HALL, 'profiles': [{'group_id': 'group', 'candidate': candidate,
        'members': [{'machine_name': MACHINE, 'seat_number': 101, 'segment_id': 'period'}]}]}]
    frozen = selection.freeze_selection(p)
    assert {s['family'] for s in frozen['samples']} == set(selection.FAMILIES)
    event = next(s for s in frozen['samples'] if s['family'] == 'event')
    assert event['models']['current'] is None and event['members'] == []
    assert next(s for s in frozen['samples'] if s['family'] == 'seat')['members'][0]['segment_id'] == 'period'


def test_no_future_or_duplicate_forecasts():
    p = prediction()
    p['machine_studies'][0]['profiles'][0]['candidate']['input_cutoff_date'] = '2026-09-08'
    with pytest.raises(ValueError, match='cutoff'):
        selection.freeze_selection(p)
    p = prediction()
    p['machine_studies'] *= 2
    with pytest.raises(ValueError, match='duplicate'):
        selection.freeze_selection(p)


def test_research_is_not_adoption_and_deduplicates():
    p = prediction()
    trial = {'date': '2026-08-01', 'input_cutoff_date': '2026-07-30', 'predicted_coins': 0, 'baseline_coins': 100, 'actual_coins': 0}
    p['machine_studies'][0]['profiles'][0]['validation']['trials'] = [trial, dict(trial)]
    result = selection.research_summary(p)['items'][1]
    assert result['resolved'] == 1 and result['candidate_mae'] == 0
    assert '採用の根拠にしない' in result['status']
    p['machine_studies'][0]['profiles'][0]['validation']['trials'].append({**trial, 'actual_coins': 99})
    assert selection.research_summary(p)['items'][1]['resolved'] == 0


def test_gate_pass_means_adoptable_never_live_switch():
    result = selection.assess(samples(), family='machine')
    assert result['decision'] == 'adoptable' and not result['live_applied']
    assert result['metrics']['comparisons'][0]['lower_bound'] == 900
    assert result == selection.assess(samples(), family='machine')


@pytest.mark.parametrize('kind', ['one_day', 'missing', 'no_current', 'nonfinite', 'weak', 'unstable'])
def test_insufficient_or_unstable_evidence_is_held(kind):
    data = samples()
    if kind == 'one_day':
        for s in data:
            s['target_date'] = '2026-08-01'
    elif kind == 'missing':
        for s in data[:30]:
            s['actual'] = None
    elif kind in {'no_current', 'nonfinite'}:
        data[0]['models']['current'] = None if kind == 'no_current' else float('nan')
    elif kind == 'weak':
        for s in data:
            s['models']['candidate'] = 980
    else:
        for s in data[35:]:
            s['models']['candidate'] = 1200
    assert selection.assess(data, family='machine')['decision'] == 'hold'


def test_no_improvement_is_rejected_with_sufficient_evidence():
    data = samples()
    for s in data:
        s['models']['candidate'] = 1000
    assert selection.assess(data, family='machine')['decision'] == 'reject'


def test_stale_evidence_is_held_even_if_earlier_performance_was_good():
    result = selection.assess(samples(), family='machine', evaluation_end=date(2026, 9, 20))
    assert result['decision'] == 'hold'
    assert any('14日' in reason for reason in result['reasons'])


def test_large_hall_does_not_overweight_calendar_days():
    data = samples()
    original = selection.assess(data, family='machine')['metrics']
    assert selection.assess(data*20, family='machine')['metrics'] == original


def test_batch_freezes_selection_without_changing_forecast(conn):
    p = prediction()
    save(conn, p)
    stored = json.loads(conn.execute('SELECT payload FROM prediction_batch').fetchone()[0])
    assert stored['forecast_subjects'] == p['forecast_subjects']
    assert stored['model_selection']['protocol'] == selection.PROTOCOL
    p['machine_studies'][0]['profiles'][0]['candidate']['predicted_coins'] = 999
    assert save(conn, p)['status'] == 'already_saved'
    assert json.loads(conn.execute('SELECT payload FROM prediction_batch').fetchone()[0])['model_selection']['samples'][0]['models']['candidate'] == 100


def test_prospective_window_missing_zero_and_implementation_separation(conn):
    save(conn)
    before = selection.prospective_review(conn, today=date(2026, 9, 13))
    assert before['window_end'] == '2026-09-06' and before['items'][1]['saved'] == 0
    pending = selection.prospective_review(conn, today=date(2026, 9, 14))
    assert pending['items'][1]['pending'] == 1
    actual(conn)
    after = selection.prospective_review(conn, today=date(2026, 9, 14))
    assert after['items'][1]['resolved'] == 1  # Actual zero is a valid outcome.
    assert after['evidence_sha256'] != pending['evidence_sha256']
    changed = selection.prospective_review(conn, today=date(2026, 9, 14), code_hash='different')
    assert changed['items'][1]['saved'] == 0 and changed['exclusions']


def test_low_activity_outcome_not_treated_as_loss(conn):
    save(conn)
    actual(conn, games=999)
    result = selection.prospective_review(conn, today=date(2026, 9, 14))
    assert result['items'][1]['pending'] == 1 and result['items'][1]['resolved'] == 0


def test_old_protocol_is_not_backfilled_from_current_studies(conn):
    initialize(conn)
    p = prediction()
    now = '2026-09-08T13:00:00+09:00'
    conn.execute('INSERT INTO prediction_batch VALUES(?,?,?,?,?,?,?,?,?,?)',
        (1, '2026-09-09', 'shijonawate', 'smartslot-prospective-v1', '3.29.0', now, now,
         '2026-09-07', selection.dumps(p), selection.digest(p)))
    result = selection.prospective_review(conn, today=date(2026,9,14))
    assert result['items'][1]['saved'] == 0
    assert result['exclusions']['旧方式・異なる実装・基準未保存'] == 1


def test_new_outcomes_append_a_new_review_and_keep_old_report(conn):
    save(conn)
    pending = selection.prospective_review(conn, today=date(2026,9,14))
    old_id = selection.store_review(conn, pending, created_at='2026-09-14')
    actual(conn)
    answered = selection.prospective_review(conn, today=date(2026,9,14))
    new_id = selection.store_review(conn, answered, created_at='2026-09-14')
    assert new_id != old_id
    old = json.loads(conn.execute('SELECT payload FROM prediction_model_review WHERE id=?',(old_id,)).fetchone()[0])
    assert old['items'][1]['resolved'] == 0 and answered['items'][1]['resolved'] == 1


def test_corrupt_fixed_archive_forces_hold(conn):
    initialize(conn)
    p = prediction()
    p['model_selection'] = selection.freeze_selection(p)
    now = '2026-09-08T13:00:00+09:00'
    conn.execute('INSERT INTO prediction_batch VALUES(?,?,?,?,?,?,?,?,?,?)',
        (1, '2026-09-09', 'shijonawate', 'smartslot-prospective-v1', '3.30.0', now, now,
         '2026-09-07', selection.dumps(p), 'invalid-hash'))
    result = selection.prospective_review(conn, today=date(2026,9,14))
    assert result['exclusions']['固定記録の整合性または保存時点違反'] == 1
    assert all(item['decision'] == 'hold' for item in result['items'])


def test_seat_identity_and_missing_group_member_are_not_substituted():
    m = {'scope': 'seat', 'hall_name': HALL, 'machine_key': 'L北斗', 'seat_number': 101, 'segment_id': 'old'}
    key = ('seat', HALL, 'L北斗', 101)
    actuals = {key: {'actual': 100, 'row': {'games': 4000}}}
    assert selection._resolve_sample({'members': [m]}, actuals, {(HALL,101): {'usable': True, 'segment_id': 'new'}})[0] is None
    identities = {(HALL,101): {'usable': True, 'segment_id': 'old'}}
    assert selection._resolve_sample({'members': [m]}, actuals, identities)[0] == 100
    assert selection._resolve_sample({'members': [m, {**m, 'seat_number': 102}]}, actuals, identities)[0] is None


def test_review_immutable_idempotent_and_get_read_only(conn):
    before = conn.total_changes
    assert selection.latest_review(conn) is None
    review = selection.prospective_review(conn, today=date(2026, 9, 13))
    assert conn.total_changes == before
    first = selection.store_review(conn, review, created_at='2026-09-13T12:00:00+09:00')
    assert selection.store_review(conn, review, created_at='2026-09-13T13:00:00+09:00') == first
    latest = selection.latest_review(conn)
    assert 'samples' not in latest and 'evidence' not in latest
    assert latest['created_at'] == '2026-09-13T12:00:00+09:00'
    for sql in ('UPDATE prediction_model_review SET id=id', 'DELETE FROM prediction_model_review'):
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute(sql)


def test_api_does_not_run_search_or_mutate_on_get(tmp_path, monkeypatch):
    from api.routers import predictions
    path = tmp_path/'api.db'
    def connect():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(predictions, '_get_reports_conn', connect)
    app = FastAPI()
    app.include_router(predictions.router)
    client = TestClient(app)
    assert client.get('/api/predictions/model_review').json()['report'] is None
    c = connect()
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0
    c.close()
    response = client.post('/api/predictions/model_review')
    assert response.status_code == 200
    assert {s['decision'] for s in response.json()['report']['items']} == {'hold'}
    assert client.post('/api/predictions/model_review').json()['id'] == response.json()['id']
    predictions._run_lock.acquire()
    try:
        assert client.post('/api/predictions/model_review').status_code == 409
    finally:
        predictions._run_lock.release()
