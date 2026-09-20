import copy
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hall import probability_validation as pv
from hall.model_selection import implementation_hash as source_hash
from hall.prediction_benchmark import freeze_comparison
from hall.prediction_log import JST, POLICY, dumps, initialize, save_batch

HALL = 'キコーナ四條畷店'
VERSION = '3.31.0'
NOW = datetime(2026, 9, 13, 13, tzinfo=JST)


@pytest.fixture
def conn():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    initialize(c)
    yield c
    c.close()


def forecast(target='2026-09-14', *, probability=70):
    cutoff = date.fromisoformat(target)-timedelta(days=2)
    item = {'scope':'machine', 'hall_name':HALL, 'machine_name':'L北斗', 'seat_number':0,
            'projected':100, 'probability_pct':probability, 'action':'参考', 'quality':{'analysis_eligible':False},
            'forecast_interval_coins':{'level_pct':80, 'low':-100, 'high':300}}
    rows = [{'hall_name':HALL, 'machine_name':'L北斗', 'report_date':(cutoff-timedelta(days=i)).isoformat(),
             'avg_diff_coins':100 if i%2 else -100, 'avg_games':4000, 'unit_count':10} for i in range(20)]
    result = {'visit_date':target, 'input_cutoff_date':cutoff.isoformat(), 'region':'shijonawate',
              'forecast_subjects':[item], 'frozen_inputs':{'machine_rows':rows, 'seat_rows':[]}}
    result['comparison'] = freeze_comparison(result)
    result['model_selection'] = {'implementation_hash':source_hash()}
    return result


def insert(conn, target='2026-09-10', *, actual=0, known='2026-09-11T12:00:00+09:00', version=VERSION,
           probability=70, games=4000, corrupt=False, shadow=None, region='shijonawate'):
    p = forecast(target, probability=probability)
    if shadow is not None:
        p['probability_validation'] = shadow
    saved = f'{(date.fromisoformat(target)-timedelta(days=1)).isoformat()}T13:00:00+09:00'
    bid = conn.execute('INSERT INTO prediction_batch(target_date,region,policy,version,started_at,saved_at,cutoff_date,payload,payload_sha256) VALUES(?,?,?,?,?,?,?,?,?)',
        (target,region,POLICY,version,saved,saved,p['input_cutoff_date'],dumps(p),'bad' if corrupt else pv.digest(p))).lastrowid
    item = p['forecast_subjects'][0]
    key = pv.subject_key(item)
    sid = conn.execute('INSERT INTO prediction_subject(batch_id,scope,hall_name,machine_key,seat_number,projected,probability,recommended,payload) VALUES(?,?,?,?,?,?,?,?,?)',
        (bid,key[0],key[1],key[2],0,100,probability/100,0,dumps(item))).lastrowid
    if actual is not None:
        evidence = {'row': {'hall_name':HALL,'machine_name':'L北斗','report_date':target,
                           'avg_diff_coins':actual,'avg_games':games,'unit_count':10}}
        conn.execute('INSERT INTO prediction_outcome VALUES(?,?,?,?,?)', (sid,actual,int(actual>0),known,dumps(evidence)))
    conn.commit()
    return bid


def history(days=35, *, probability=.7):
    result = []
    for i in range(days):
        day = date(2026,6,1)+timedelta(days=i)
        result.append({'target_date':day.isoformat(), 'known_at':f'{(day+timedelta(days=1)).isoformat()}T12:00:00+09:00',
                       'hall_name':HALL, 'probability':probability, 'projected':0,
                       'actual':100 if i%2 else -100, 'batch_id':i, 'subject_id':i, 'model':'current'})
    return result


def test_fit_small_sample_holds_instead_of_zero_or_certainty():
    fitted = pv.fit(history(29))
    assert fitted['radius'] is None
    assert pv.predict({'probability':.7,'projected':0}, fitted)['calibrated_probability'] is None
    assert pv.fit([])['last_date'] is None


def test_probability_shrinkage_and_range_calculated_from_training_only():
    fitted = pv.fit(history(30))
    result = pv.predict({'probability':.7, 'projected':500}, fitted)
    assert result['calibrated_probability'] == pytest.approx((7+15)/40)
    assert result['interval'] == {'level_pct':80, 'low':400,'high':600}
    # Fixed band boundaries include 100%, without overflowing the last bin.
    assert pv.predict({'probability':1, 'projected':0}, fitted)['calibrated_probability'] is None


def test_repeated_seats_do_not_create_days_or_change_daily_weights():
    rows = history(1)*1000
    assert pv.fit(rows)['days'] == 1
    assert pv.fit(rows)['radius'] is None
    original = history(35)
    duplicate_hall = original + [original[0]]*1000
    assert pv.fit(original)['bins'] == pv.fit(duplicate_hall)['bins']


def test_range_uses_daily_max_and_finite_sample_rank():
    rows = history(30)
    rows += [{**rows[0], 'actual':9999}]
    assert pv.fit(rows)['radius'] == 100  # single outlier day is not 1000 independent rows
    for i in range(7):
        rows[i]['actual'] = 1000
    assert pv.fit(rows)['radius'] == 1000


@pytest.mark.parametrize('actual,known,games,expected,reason', [
    (0,'2026-09-11T12:00:00+09:00',4000,0,None),
    (None,None,4000,None,'結果待ち'),
    (100,'2026-09-14T12:00:00+09:00',4000,None,'結果待ち'),
    (100,'2026-09-11T12:00:00+09:00',999,None,'稼働不明・1000G未満'),
    (100,'2026-09-11T12:00:00+09:00',None,None,'稼働不明・1000G未満'),
])
def test_missing_zero_late_and_low_activity_separated(conn, actual,known,games,expected,reason):
    insert(conn,actual=actual,known=known,games=games)
    before = conn.total_changes
    rows, excluded = pv.read_samples(conn,since=date(2026,9,1),as_of=NOW)
    assert len(rows) == 4 and not excluded
    assert all(r['actual'] == expected and r['unresolved_reason'] == reason for r in rows)
    assert conn.total_changes == before


def test_corruption_and_premature_results_are_quarantined(conn):
    insert(conn,corrupt=True)
    insert(conn,'2026-09-09',known='2026-09-09T22:00:00+09:00')
    report = pv.validation_report(conn,as_of=NOW)
    assert not report['items']
    assert report['exclusions']['記録の整合性・保存時点違反'] == 2


def test_version_region_and_implementation_not_pooled(conn):
    insert(conn,version='old')
    insert(conn,'2026-09-09')
    insert(conn,'2026-09-08',region='nagano')
    report = pv.validation_report(conn,as_of=NOW)
    assert len(report['items']) == 8
    assert {r['version'] for r in report['items']} == {'old',VERSION}
    assert all(r['paired'] == 0 and r['status'] == 'hold' for r in report['items'])


def test_freeze_only_uses_earlier_known_same_version_and_leaves_live_untouched(conn):
    for i in range(35):
        day = date(2026,7,1)+timedelta(days=i)
        insert(conn,day.isoformat(),actual=100 if i%2 else -100,
               known=f'{(day+timedelta(days=1)).isoformat()}T12:00:00+09:00')
    # Late publication and another model version cannot help train the current model.
    insert(conn,'2026-09-10',actual=10000,known='2026-09-14T12:00:00+09:00')
    insert(conn,'2026-09-09',version='old')
    p = forecast()
    before = copy.deepcopy(p)
    frozen = pv.freeze_calibration(conn,p,version=VERSION,started_at=NOW)
    assert p == before
    assert len(frozen['samples']) == 4 and not frozen['live_applied']
    assert all(t['days'] == 35 for t in frozen['training'])
    assert all(t['last_date'] == '2026-08-04' for t in frozen['training'])
    assert frozen['samples'][0]['calibrated_probability'] is not None
    original = frozen
    # Same-day outcomes after save start are still unavailable.
    insert(conn,'2026-09-11',actual=99999,known='2026-09-13T14:00:00+09:00')
    assert pv.freeze_calibration(conn,p,version=VERSION,started_at=NOW) == original
    save_batch(conn,p,version=VERSION,started_at=NOW,now=NOW)
    row = conn.execute('SELECT s.id FROM prediction_subject s JOIN prediction_batch b ON b.id=s.batch_id WHERE b.target_date=?', ('2026-09-14',)).fetchone()
    evidence = {'row':{'hall_name':HALL,'machine_name':'L北斗','report_date':'2026-09-14','avg_diff_coins':0,'avg_games':4000,'unit_count':10}}
    conn.execute('INSERT INTO prediction_outcome VALUES(?,?,?,?,?)', (row[0],0,0,'2026-09-15T12:00:00+09:00',dumps(evidence)))
    report = pv.validation_report(conn,as_of=datetime(2026,9,16,13,tzinfo=JST))
    current = next(r for r in report['items'] if r['version'] == VERSION and r['model'] == 'current')
    assert current['paired'] == 1 and current['candidate_interval']['count'] == 1
    assert current['existing_interval']['count'] > current['paired_intervals']['before']['count'] == 1
    assert current['status'] == 'hold'


def test_changed_calibration_algorithm_does_not_reuse_old_shadow(conn, monkeypatch):
    insert(conn)
    old_hash = pv.implementation_hash()
    p = forecast()
    save_batch(conn,p,version=VERSION,started_at=NOW,now=NOW)
    monkeypatch.setattr(pv,'implementation_hash',lambda:'new-algorithm')
    rows, _ = pv.read_samples(conn,since=date(2026,9,1),as_of=datetime(2026,9,16,13,tzinfo=JST))
    assert old_hash != pv.implementation_hash()
    assert all(r['shadow'] is None for r in rows)


@pytest.mark.parametrize('bad', [float('nan'),float('inf'),-1,1.01,True])
def test_invalid_probability_is_rejected_before_freezing(conn,bad):
    p = forecast()
    p['comparison']['samples'][0]['models']['current']['probability'] = bad
    with pytest.raises(ValueError,match='invalid probability'):
        pv.freeze_calibration(conn,p,version=VERSION,started_at=NOW)


def test_save_freezes_calibration_with_forecasts_and_never_backfills(conn):
    p = forecast()
    save_batch(conn,p,version=VERSION,started_at=NOW,now=NOW)
    original = conn.execute('SELECT payload FROM prediction_batch').fetchone()[0]
    stored = json.loads(original)
    assert stored['probability_validation']['protocol'] == pv.PROTOCOL
    assert stored['forecast_subjects'] == p['forecast_subjects']
    p['forecast_subjects'][0]['probability_pct'] = 90
    assert save_batch(conn,p,version=VERSION,started_at=NOW,now=NOW)['status'] == 'already_saved'
    assert conn.execute('SELECT payload FROM prediction_batch').fetchone()[0] == original


def evaluated(days=60):
    rows = history(days,probability=.9)
    for r in rows:
        r.update({'shadow':{'calibrated_probability':.5,'interval':{'level_pct':80,'low':-200,'high':200}},
                  'existing_interval':{'level_pct':80,'low':-50,'high':50}, 'unresolved_reason':None})
    return rows


def test_holdout_scores_and_range_scores_on_same_targets():
    rows = evaluated()
    result = pv.summarize(rows,end=date(2026,7,30))
    assert result['status'] == 'reference_improved' and not result['live_applied']
    assert result['paired_before_brier'] > result['paired_after_brier']
    assert result['paired_after_brier'] == .25
    assert result['candidate_interval']['coverage_pct'] == 100
    assert result['existing_interval']['coverage_pct'] == 0
    assert result['candidate_interval']['interval_score'] == 400
    assert result['existing_interval']['interval_score'] == 600


@pytest.mark.parametrize('kind', ['small','stale','missing','worse','second_half','single_class'])
def test_insufficient_or_worse_corrections_stay_on_hold(kind):
    rows = evaluated()
    end = date(2026,7,30)
    if kind == 'small': rows = rows[:20]
    if kind == 'stale': end = date(2026,9,13)
    if kind == 'missing':
        for r in rows[:20]: r['actual'] = None; r['unresolved_reason'] = '結果待ち'
    if kind == 'worse':
        for r in rows: r['shadow']['calibrated_probability'] = 1
    if kind == 'second_half':
        for r in rows[30:]: r['shadow']['calibrated_probability'] = 1
    if kind == 'single_class':
        for r in rows: r['actual'] = 100
    assert pv.summarize(rows,end=end)['status'] == 'hold'


def test_intervals_missing_reversed_or_nonfinite_not_zero_width_hits():
    rows = evaluated(4)
    for r, value in zip(rows,[None,{}, {'level_pct':80,'low':2,'high':1}, {'level_pct':80,'low':float('nan'),'high':3}]):
        r['existing_interval'] = value
    result = pv.summarize(rows,end=date(2026,7,30))
    assert result['existing_interval']['count'] == 0
    assert result['paired_intervals']['after']['coverage_pct'] is None
    assert result['candidate_interval']['count'] == 4


def test_api_get_only_does_not_create_tables_or_run_predictions(tmp_path, monkeypatch):
    from api.routers import predictions
    path = tmp_path/'reports.db'
    def connect():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(predictions,'_get_reports_conn',connect)
    app = FastAPI(); app.include_router(predictions.router)
    client = TestClient(app)
    response = client.get('/api/predictions/probability_validation')
    assert response.status_code == 200 and response.json()['items'] == []
    assert client.post('/api/predictions/probability_validation').status_code == 405
    c = connect()
    assert not c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    c.close()
