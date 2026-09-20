import copy
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hall import candidate_evaluation as ce
from hall.prediction_benchmark import MODELS, subject_key
from hall.prediction_log import JST, POLICY, dumps, initialize, save_batch
from hall.probability_validation import digest
from tests.test_probability_validation import forecast, NOW


@pytest.fixture
def conn():
    c=sqlite3.connect(':memory:'); c.row_factory=sqlite3.Row
    initialize(c)
    yield c
    c.close()


def ranked_prediction():
    p=forecast()
    item=p['comparison']['samples'][0]
    p['comparison']['samples']=[{**copy.deepcopy(item),'scope':'seat','seat_number':i,
        'live_recommended':i%2==0,'models':{m:{'projected':100-i,'probability':.7} for m in MODELS}}
        for i in range(1,7)]
    return p


def case(values, *, day='2026-09-10', eligible=None):
    ranking=[{'key':['seat','店','機種',i], 'projected':100-i, 'probability':.7,
              'eligible':eligible[i] if eligible else True} for i in range(len(values))]
    group={'ranking':ranking,'total':len(values),'excluded_missing_models':0}
    actuals={tuple(r['key']):{'actual':v,'unresolved_reason':'結果待ち' if v is None else None} for r,v in zip(ranking,values)}
    return ce.select_case(group,actuals,target=day,pool='all')


def test_rank_is_deterministic_and_independent_of_input_order():
    p=ranked_prediction()
    first=ce.freeze_candidates(p)
    p['comparison']['samples'].reverse()
    assert ce.freeze_candidates(p)==first
    seats=next(g for g in first['groups'] if g['scope']=='seat' and g['model']=='current')
    assert [r['key'][3] for r in seats['ranking']]==[1,2,3,4,5,6]
    assert not first['live_applied']


def test_ties_use_probability_then_stable_identity_and_are_reported():
    p=ranked_prediction()
    for s in p['comparison']['samples']:
        s['models']={m:{'projected':0,'probability':.8 if s['seat_number']==6 else .7} for m in MODELS}
    group=next(g for g in ce.freeze_candidates(p)['groups'] if g['scope']=='seat' and g['model']=='current')
    assert [r['key'][3] for r in group['ranking']]==[6,1,2,3,4,5]
    c=ce.select_case(group,{},target='2026-09-10',pool='all')
    assert ce.aggregate([c],k=3,end=date(2026,9,12))['tie_boundary_days']==1


def test_common_model_cohort_excludes_missing_but_preserves_denominator():
    p=ranked_prediction()
    del p['comparison']['samples'][0]['models']['weekday']
    frozen=ce.freeze_candidates(p)
    groups=[g for g in frozen['groups'] if g['scope']=='seat']
    assert all(g['total']==6 and g['excluded_missing_models']==1 and len(g['ranking'])==5 for g in groups)
    assert all(g['ranking'][0]['key'][3]==2 for g in groups)


@pytest.mark.parametrize('invalid', [float('nan'),float('inf'),True,1.5])
def test_invalid_forecast_never_enters_any_models_comparison_cohort(invalid):
    p=ranked_prediction()
    p['comparison']['samples'][0]['models']['weekday']['probability']=invalid
    groups=[g for g in ce.freeze_candidates(p)['groups'] if g['scope']=='seat']
    assert all(g['excluded_missing_models']==1 and len(g['ranking'])==5 for g in groups)


def test_duplicate_subjects_rejected_before_freeze():
    p=ranked_prediction()
    p['comparison']['samples'].append(copy.deepcopy(p['comparison']['samples'][0]))
    with pytest.raises(ValueError,match='duplicate'):
        ce.freeze_candidates(p)


def test_live_gate_pool_is_separate_from_reference_and_never_changes_flags():
    group=next(g for g in ce.freeze_candidates(ranked_prediction())['groups'] if g['scope']=='seat' and g['model']=='current')
    before=copy.deepcopy(group)
    c=ce.select_case(group,{},target='2026-09-10',pool='eligible')
    assert [r['key'][3] for r in c['rows']]==[2,4,6]
    assert c['withheld']==3 and group==before
    assert ce.aggregate([c],k=5,end=date(2026,9,12))['shortfall_days']==1


def test_missing_top_candidate_never_replaced_by_lower_winner():
    c=case([None,100,200,300,400,500])
    one=ce.aggregate([c],k=1,end=date(2026,9,12))
    assert one['selected']==1 and one['pending']==1 and one['positive_pct'] is None
    assert one['complete_days']==0 and one['common_days']==0


def test_zero_is_nonpositive_and_partial_days_not_counted_as_complete():
    cases=[case([0,-100,100]),case([100,None,300],day='2026-09-11')]
    one=ce.aggregate(cases,k=1,end=date(2026,9,12))
    three=ce.aggregate(cases,k=3,end=date(2026,9,12))
    assert one['positive_pct']==50 and one['complete_days']==2
    assert three['complete_days']==1 and three['incomplete_days']==1
    assert three['positive_pct']==pytest.approx(100/3)
    assert one['common_days']==three['common_days']==1
    assert one['common_positive_pct']==0


def test_same_day_comparison_uses_full_pool_not_only_top_five():
    c=case([100]*5+[None])
    result=ce.aggregate([c],k=5,end=date(2026,9,12))
    assert result['positive_pct']==100 and result['complete_days']==1
    assert result['common_days']==0 and result['pool_positive_pct'] is None


def test_day_weights_not_total_seat_weights():
    cases=[case([100]*5),case([-100],day='2026-09-11')]
    result=ce.aggregate(cases,k=5,end=date(2026,9,12))
    assert result['positive_pct']==50 and result['avg_diff_coins']==0
    assert result['selected']==6 and result['shortfall_days']==1


def test_empty_candidate_day_is_not_a_success_or_an_unrecorded_day():
    result=ce.aggregate([case([])],k=1,end=date(2026,9,12))
    assert result['saved_days']==1 and result['no_candidate_days']==1
    assert result['candidate_days']==0 and result['positive_pct'] is None


@pytest.mark.parametrize('kind',['small','stale','unresolved'])
def test_sample_size_freshness_and_resolution_hold(kind):
    cases=[case([100],day=(date(2026,7,1)+timedelta(days=i)).isoformat()) for i in range(60)]
    end=date(2026,9,1)
    if kind=='small': cases=cases[-10:]
    if kind=='stale': end=date(2026,10,1)
    if kind=='unresolved':
        for c in cases[:30]: c['rows'][0].update(actual=None,unresolved_reason='結果待ち')
    assert ce.aggregate(cases,k=1,end=end)['status']=='hold'


def test_sufficient_sample_still_only_descriptive():
    cases=[case([100,-100],day=(date(2026,7,1)+timedelta(days=i)).isoformat()) for i in range(60)]
    result=ce.aggregate(cases,k=1,end=date(2026,9,1))
    assert result['status']=='descriptive_only' and not result['reasons']


def archive(conn, *, target='2026-09-10', mode='prospective', version='3.32.0', actual=0, bad=False, region='shijonawate'):
    p=forecast(target)
    if mode=='prospective': p['candidate_evaluation']=ce.freeze_candidates(p)
    if bad: p['candidate_evaluation']['groups'][0]['ranking'][0]['projected']+=100
    saved=f'{(date.fromisoformat(target)-timedelta(days=1)).isoformat()}T13:00:00+09:00'
    bid=conn.execute('INSERT INTO prediction_batch(target_date,region,policy,version,started_at,saved_at,cutoff_date,payload,payload_sha256) VALUES(?,?,?,?,?,?,?,?,?)',
        (target,region,POLICY,version,saved,saved,p['input_cutoff_date'],dumps(p),digest(p))).lastrowid
    item=p['forecast_subjects'][0]; key=subject_key(item)
    sid=conn.execute('INSERT INTO prediction_subject(batch_id,scope,hall_name,machine_key,seat_number,projected,probability,recommended,payload) VALUES(?,?,?,?,?,?,?,?,?)',
        (bid,key[0],key[1],key[2],0,100,.7,0,dumps(item))).lastrowid
    if actual is not None:
        evidence={'row':{'hall_name':key[1],'machine_name':item['machine_name'],'report_date':target,'avg_diff_coins':actual,'avg_games':4000,'unit_count':10}}
        conn.execute('INSERT INTO prediction_outcome VALUES(?,?,?,?,?)',(sid,actual,int(actual>0),'2026-09-12T12:00:00+09:00',dumps(evidence)))
    conn.commit()


def test_prospective_and_posthoc_rules_are_not_merged(conn):
    archive(conn)
    archive(conn,target='2026-09-09',mode='research',version='3.31.0')
    archive(conn,target='2026-09-08',region='nagano')
    before=conn.total_changes
    report=ce.candidate_report(conn,as_of=NOW)
    assert report['recorded_days']==2 and report['unassessed_calendar_days']==82
    assert {i['mode'] for i in report['items']}=={'research','prospective'}
    for item in report['items']:
        if item['scope']=='machine' and item['pool']=='all':
            assert item['metrics'][0]['positive_pct']==0
        if item['pool']=='eligible':
            assert item['metrics'][0]['candidate_days']==0
    assert conn.total_changes==before and not report['live_applied']


def test_saved_order_mismatch_is_quarantined(conn):
    archive(conn,bad=True)
    report=ce.candidate_report(conn,as_of=NOW)
    assert not report['items']
    assert report['exclusions']['保存順位の整合性違反']==1
    archive(conn,target='2026-09-09')
    result=ce.candidate_report(conn,as_of=NOW)
    assert all(m['status']=='hold' and '保存記録に整合性の問題があるため保留' in m['reasons'] for item in result['items'] for m in item['metrics'])


def test_old_ranking_algorithm_not_reinterpreted_as_new_prospective(conn,monkeypatch):
    archive(conn)
    monkeypatch.setattr(ce,'implementation_hash',lambda:'new')
    result=ce.candidate_report(conn,as_of=NOW)
    assert not result['items'] and result['exclusions']['異なる順位ルール・実装']==1


def test_save_is_atomic_and_does_not_change_live_subjects(conn):
    p=forecast()
    save_batch(conn,p,version='3.32.0',started_at=NOW,now=NOW)
    text=conn.execute('SELECT payload FROM prediction_batch').fetchone()[0]
    saved=json.loads(text)
    assert saved['forecast_subjects']==p['forecast_subjects']
    assert saved['candidate_evaluation']==ce.freeze_candidates(saved)
    assert save_batch(conn,p,version='3.32.0',started_at=NOW,now=NOW)['status']=='already_saved'
    assert conn.execute('SELECT payload FROM prediction_batch').fetchone()[0]==text


def test_api_fixed_rule_read_only_and_no_empty_db_mutation(tmp_path,monkeypatch):
    from api.routers import predictions
    path=tmp_path/'reports.db'
    def connect():
        c=sqlite3.connect(path); c.row_factory=sqlite3.Row
        return c
    monkeypatch.setattr(predictions,'_get_reports_conn',connect)
    app=FastAPI(); app.include_router(predictions.router)
    client=TestClient(app)
    response=client.get('/api/predictions/candidate_evaluation?k=100&region=nagano')
    assert response.status_code==200 and response.json()['spec']['top_k']==[1,3,5]
    assert response.json()['items']==[]
    assert client.post('/api/predictions/candidate_evaluation').status_code==405
    c=connect(); assert not c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(); c.close()
