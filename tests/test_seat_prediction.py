import copy
from datetime import date, timedelta
from statistics import mean

import pytest

from hall.seat_prediction import analyze_hall_seats

HALL = 'キコーナ四條畷店'
START = date(2026, 7, 1)


def rows(count=65):
    return [{'hall_name':HALL, 'report_date':(START+timedelta(days=i)).isoformat(),
             'machine_name':'LモンキーターンV', 'seat_number':number,
             'diff_coins':diff+(i % 7)*10, 'games':4000, 'source_url':f'https://example.com/{i}'}
            for i in range(count) for number, diff in ((101,300), (102,-100), (103,0))]


def analyze(data, **kwargs):
    return analyze_hall_seats(data, hall_name=HALL, target=kwargs.pop('target', date(2026,9,10)),
                              as_of=kwargs.pop('as_of', date(2026,9,9)), **kwargs)


def test_same_machine_peer_ranks_and_all_rows_preserved():
    data = rows()
    original = copy.deepcopy(data)
    result = analyze(data)
    machine = result['machines'][0]
    assert result['ranked_seats'] == 3
    assert [p['seat_number'] for p in machine['profiles']] == [101,103,102]
    assert [p['rank'] for p in machine['profiles']] == [1,2,3]
    assert not result['model_influence_eligible']
    assert data == original
    p = machine['profiles'][0]
    assert p['candidate']['training_days'] == p['paired_days'] <= 56
    assert p['candidate']['peer_estimate_coins'] < 100  # does not include itself
    assert p['candidate']['adjustment_coins'] <= 500
    assert machine['validation']['days'] >= 20


def test_future_and_other_hall_or_machine_do_not_change_existing_forecasts():
    data = rows()
    baseline = analyze(data)['machines'][0]
    extra = [{**r, 'hall_name':'別店舗', 'diff_coins':99999} for r in data]
    extra += [{**r, 'machine_name':'マイジャグラーV', 'seat_number':501, 'diff_coins':99999} for r in data]
    extra += [{**r, 'machine_name':'L東京喰種', 'seat_number':601, 'diff_coins':99999} for r in data]
    extra += [{**data[-1], 'report_date':'2026-09-09', 'diff_coins':99999}]
    result = analyze(data+extra)
    assert next(m for m in result['machines'] if m['machine_key']==baseline['machine_key']) == baseline


def test_all_replay_forecasts_precede_outcomes_and_mae_is_paired():
    result = analyze(rows())
    machine = result['machines'][0]
    for p in machine['profiles']:
        trials = p['validation']['trials']
        for t in trials:
            assert date.fromisoformat(t['training_end']) <= date.fromisoformat(t['date'])-timedelta(days=2)
        answered = [t for t in trials if t['actual_coins'] is not None]
        assert p['validation']['candidate_mae_coins'] == round(mean(abs(t['actual_coins']-t['predicted_coins']) for t in answered),1)
    assert len(machine['validation']['trials']) <= 60


def test_missing_top_outcome_does_not_rerank_or_score_as_zero():
    data = rows()
    day = data[-1]['report_date']
    before = analyze(data)['machines'][0]['validation']['trials'][-1]
    data = [r for r in data if not (r['report_date']==day and r['seat_number']==101)]
    after = analyze(data)['machines'][0]['validation']['trials'][-1]
    assert after['selected_seats'] == before['selected_seats'] == [101]
    assert after['actual_coins'] is None and not after['resolved']
    assert after['unresolved_seats'] == [101]
    assert [(t['seat_number'], t['predicted_coins']) for t in after['predictions']] == [(t['seat_number'], t['predicted_coins']) for t in before['predictions']]


def test_nonselected_missing_also_excludes_complete_cohort_comparison():
    data = rows()
    day = data[-1]['report_date']
    data = [r for r in data if not (r['report_date']==day and r['seat_number']==102)]
    trial = analyze(data)['machines'][0]['validation']['trials'][-1]
    assert trial['selected_seats'] == [101]
    assert not trial['resolved'] and trial['baseline_regret_coins'] is None


def test_ties_share_rank_and_all_tied_outcomes_are_averaged():
    data = [{**r, 'diff_coins':100} for r in rows()]
    data[-1]['diff_coins'] = 400  # unseen at the final replay cutoff
    result = analyze(data)
    trial = result['machines'][0]['validation']['trials'][-1]
    assert trial['selected_seats'] == [101,102,103]
    assert trial['actual_coins'] == 200
    tied = analyze([{**r, 'diff_coins':100} for r in rows()])
    assert [p['rank'] for p in tied['machines'][0]['profiles']] == [1,1,1]


def test_alias_duplicates_are_not_extra_days_and_conflicts_quarantined():
    data = rows()
    duplicated = data+[{**r, 'machine_name':'スマスロモンキーターン5'} for r in data]
    assert analyze(data)['machines'] == analyze(duplicated)['machines']
    broken = data+[{**data[-1], 'diff_coins':9999}]
    result = analyze(broken)
    assert result['input_audit']['conflict_keys'] == 1
    assert not result['machines'][0]['validation']['trials'][-1]['resolved']


@pytest.mark.parametrize('changes', [{'games':None}, {'games':999}, {'diff_coins':None}])
def test_low_or_missing_activity_and_diff_never_create_a_rank(changes):
    data = [{**r, **changes} for r in rows(10)]
    result = analyze(data, target=date(2026,7,13), as_of=date(2026,7,12))
    assert result['ranked_seats'] == 0
    assert all(p['candidate'] is None for m in result['machines'] for p in m['profiles'])


def test_replacement_a_b_a_limits_training_but_keeps_past_rank_trials():
    data = rows()
    data += [{**data[0], 'report_date':'2026-09-04', 'machine_name':'マイジャグラーV', 'diff_coins':None},
             {**data[0], 'report_date':'2026-09-05'}]
    result = analyze(data)
    p = next(p for p in result['machines'][0]['profiles'] if p['seat_number']==101)
    assert p['training_from'] == '2026-09-05' and p['paired_days']==0
    assert p['candidate'] is None and p['validation']['days']==0
    assert result['machines'][0]['validation']['days'] > 20


def test_replacement_on_outcome_day_is_not_scored_as_old_machine_loss():
    data = rows()
    data[-3]['machine_name'] = 'マイジャグラーV'
    data[-3]['diff_coins'] = None
    trial = analyze(data)['machines'][0]['validation']['trials'][-1]
    assert not trial['resolved']
    assert trial['predictions'][0]['unresolved_reason'] == '入替・配置変更・観測期間の不一致'


def test_missing_stale_or_only_two_seats_are_unranked():
    assert analyze([])['machines'] == []
    assert analyze(rows(), target=date(2026,10,10), as_of=date(2026,10,9))['ranked_seats']==0
    assert analyze([r for r in rows() if r['seat_number']!=103])['ranked_seats']==0


def test_layout_changes_use_each_replay_snapshot_not_current_map():
    data = rows()
    changed = {**data[0], 'report_date':'2026-09-02', 'layout_id':1,
               'floor_name':'1階','island_name':'島A','x':100,'y':0}
    baseline = analyze(data)
    result = analyze(data, layouts_by_cutoff={'2026-09-08':[changed]})
    assert result['machines'][0]['validation']==baseline['machines'][0]['validation']
    p = next(p for p in result['machines'][0]['profiles'] if p['seat_number']==101)
    assert p['candidate'] is None and p['training_from']=='2026-09-02'


def test_installation_blocks_current_not_retrospective_and_future_ignored():
    data = rows()
    baseline = analyze(data)
    result = analyze(data, installation={'snapshot_date':'2026-09-08','machines':['L北斗']})
    assert result['ranked_seats']==0
    assert result['machines'][0]['validation']==baseline['machines'][0]['validation']
    future = analyze(data, installation={'snapshot_date':'2026-09-09','machines':['L北斗']})
    assert future['machines']==baseline['machines']
