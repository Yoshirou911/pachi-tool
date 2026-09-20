import copy
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hall import prediction_monitor as pm
from hall.prediction_log import JST, initialize
from tests.test_probability_validation import insert

END = date(2026, 9, 13)
NOW = datetime(2026, 9, 14, 13, tzinfo=JST)


def history(*, before=100, after=100, p=.7):
    return [{'target_date': (END-timedelta(days=83-i)).isoformat(),
             'key': ['machine','店','機種',0], 'probability': p, 'projected': 100,
             'actual': before if i<56 else after, 'unresolved_reason': None}
            for i in range(84)]


@pytest.fixture
def conn():
    c=sqlite3.connect(':memory:'); c.row_factory=sqlite3.Row; initialize(c)
    yield c
    c.close()


def test_fixed_disjoint_windows_and_daily_zero_is_not_missing():
    result=pm.compare_cohort(history(before=0,after=0),end=END)
    assert result['baseline']['start']=='2026-06-22'
    assert result['baseline']['end']=='2026-08-16'
    assert result['recent']['start']=='2026-08-17'
    assert result['recent']['end']=='2026-09-13'
    assert result['baseline']['complete_days']==56 and result['recent']['complete_days']==28
    assert result['recent']['metrics']['positive_pct']==0
    assert result['recent']['metrics']['avg_diff_coins']==0
    assert result['status']=='no_alert' and not result['live_applied']


def test_sustained_forecast_error_and_observed_change_are_separate():
    result=pm.compare_cohort(history(after=-600),end=END)
    assert result['status']=='review'
    assert {a['kind'] for a in result['alerts']}=={'forecast','observed'}
    assert {a['metric'] for a in result['alerts']}=={'brier','mae_coins','avg_diff_coins','positive_pct'}
    assert result['delta']['mae_coins']==700


def test_observed_change_does_not_automatically_mean_forecast_worsened():
    rows=history(after=700)
    for r in rows[56:]: r['projected']=700
    result=pm.compare_cohort(rows,end=END)
    assert {a['kind'] for a in result['alerts']}=={'observed'}


def test_one_bad_half_is_not_a_sustained_alert():
    rows=history()
    for r in rows[-14:]: r['actual']=-1000
    result=pm.compare_cohort(rows,end=END)
    assert result['status']=='no_alert' and result['delta']['mae_coins']>0


def test_opposite_direction_halves_are_not_an_observed_change_alert():
    rows=history()
    for i,r in enumerate(rows[56:]):
        r['actual']=800 if i<14 else -800
        r['projected']=r['actual']
    result=pm.compare_cohort(rows,end=END)
    assert not [a for a in result['alerts'] if a['kind']=='observed']


def test_missing_outcomes_hold_instead_of_degradation():
    rows=history(after=-600)
    for r in rows[-20:]: r.update(actual=None,unresolved_reason='結果待ち')
    result=pm.compare_cohort(rows,end=END)
    assert result['status']=='insufficient_data' and result['alerts']==[]
    assert result['recent']['pending']==20 and result['recent']['complete_days']==8
    assert result['recent']['unresolved_reasons']=={'結果待ち':20}


def test_partial_day_excludes_all_panel_members_not_just_missing_loser():
    rows=history()
    extra=[{**r,'key':['machine','店','別機種',0]} for r in rows]
    extra[-1]['actual']=None
    result=pm.compare_cohort(rows+extra,end=END)
    assert result['common_subjects']==2 and result['recent']['complete_days']==27
    assert result['recent']['incomplete_days']==1
    assert all(d['subjects']==2 for d in result['recent']['daily'])


def test_missing_forecast_for_panel_member_also_excludes_day():
    rows=history()
    extra=[{**r,'key':['machine','店','別機種',0]} for r in rows[:-1]]
    result=pm.compare_cohort(rows+extra,end=END)
    assert result['recent']['complete_days']==27 and result['recent']['pending']==0


def test_no_common_subjects_is_composition_gap_not_store_degradation():
    rows=history(after=-1000)
    for r in rows[56:]: r['key']=['machine','店','入替機種',0]
    result=pm.compare_cohort(rows,end=END)
    assert result['common_subjects']==0 and result['status']=='insufficient_data'
    assert result['alerts']==[] and result['recent']['metrics']['brier'] is None


def test_common_panel_must_cover_80_percent_of_saved_subjects():
    rows=history(after=-600)
    rows += [{**r,'key':['machine','店','前期のみ',0]} for r in rows[:56]]
    result=pm.compare_cohort(rows,end=END)
    assert result['baseline']['panel_share']==.5
    assert result['status']=='insufficient_data' and not result['alerts']


@pytest.mark.parametrize('kind',['few_baseline','few_recent','half','stale'])
def test_dates_weeks_halves_and_freshness_are_gates(kind):
    rows=history(after=-600)
    if kind=='few_baseline': rows=rows[35:]
    if kind=='few_recent': rows=rows[:56]+rows[-10:]
    if kind=='half': rows=rows[:56]+rows[64:]
    if kind=='stale': rows=rows[:-8]
    assert pm.compare_cohort(rows,end=END)['status']=='insufficient_data'


def test_duplicate_subject_dates_are_not_double_counted():
    rows=history(); rows.append(copy.deepcopy(rows[-1]))
    result=pm.compare_cohort(rows,end=END)
    assert result['status']=='insufficient_data'
    assert result['recent']['metrics']['brier'] is None


def test_outside_window_and_future_ignored_and_input_not_mutated():
    rows=history(after=-600)
    expected=pm.compare_cohort(rows,end=END)
    rows.extend([{**rows[0],'target_date':'2026-01-01'},{**rows[-1],'target_date':'2026-09-14'}])
    before=copy.deepcopy(rows)
    assert pm.compare_cohort(rows,end=END)==expected and rows==before


def test_empty_is_unmeasured_not_zero():
    result=pm.compare_cohort([],end=END)
    assert result['baseline']['metrics']['brier'] is None
    assert result['recent']['unrecorded_days']==28
    assert pm.monitor_report(None,as_of=NOW)['items']==[]


def test_report_partition_includes_store_scope_version_implementation_and_model(monkeypatch):
    rows=[]
    for field,values in [('hall_name',['店A','店B']),('scope',['seat']),('version',['3.32']),('source_hash',['別実装'])]:
        for value in values:
            rows.append({**history()[0], 'hall_name':'店', 'scope':'machine', 'version':'3.33',
                         'source_hash':'実装', 'model':'current',field:value})
    rows.append({**rows[0],'model':'mean'})
    from collections import Counter
    monkeypatch.setattr(pm,'read_samples',lambda *a,**k:(rows,Counter()))
    assert len(pm.monitor_report(None,as_of=NOW)['items'])==6


def test_real_ledger_read_only_region_time_quality_and_integrity(conn):
    insert(conn, target='2026-09-10', actual=0)
    insert(conn, target='2026-09-09', actual=100, games=999)
    insert(conn, target='2026-09-08', actual=100, known='2026-09-15T12:00:00+09:00')
    insert(conn, target='2026-09-07', region='nagano')
    before=conn.total_changes
    report=pm.monitor_report(conn,as_of=NOW)
    assert report['recorded_days']==3 and report['unassessed_calendar_days']==81
    assert len(report['items'])==4
    assert conn.total_changes==before
    # Shared reader retains unknown/low-activity rows, never promotes to zero.
    samples,_=pm.read_samples(conn,since=date(2026,6,22),as_of=NOW)
    assert {r['actual'] for r in samples}=={0,None}
    insert(conn,target='2026-09-06',corrupt=True)
    report=pm.monitor_report(conn,as_of=NOW)
    assert report['exclusions']['記録の整合性・保存時点違反']==1
    assert all('整合性' in r['reasons'][-1] and not r['alerts'] for r in report['items'])


def test_readonly_api_fixed_rule_and_no_db_mutation(tmp_path,monkeypatch):
    from api.routers import predictions
    path=tmp_path/'empty.db'
    def connect():
        c=sqlite3.connect(path); c.row_factory=sqlite3.Row; return c
    monkeypatch.setattr(predictions,'_get_reports_conn',connect)
    app=FastAPI(); app.include_router(predictions.router)
    client=TestClient(app)
    result=client.get('/api/predictions/monitor?recent_days=1&region=nagano').json()
    assert result['spec']['recent_days']==28 and result['items']==[] and not result['live_applied']
    assert client.post('/api/predictions/monitor').status_code==405
    c=connect(); assert c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[]; c.close()
