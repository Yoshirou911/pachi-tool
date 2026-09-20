from copy import deepcopy
from datetime import date, timedelta

from hall.machine_prediction import analyze_hall_machines


START = date(2026, 6, 1)
TARGET = START+timedelta(days=81)


def fixture(days=80):
    return [{'hall_name':'キコーナ四条畷店', 'report_date':(START+timedelta(days=i)).isoformat(),
             'machine_name':name, 'avg_diff_coins':diff, 'unit_count':units,
             'avg_games':5000, 'source_url':'https://example.com/data'}
            for i in range(days) for name, diff, units in [('L北斗',1000,100), ('L東京喰種',0,10), ('LモンキーターンV',200,30)]]


def study(rows, installation=None):
    return analyze_hall_machines(rows, hall_name='キコーナ四條畷店', target=TARGET,
                                 as_of=TARGET-timedelta(days=1), installation=installation)


def profile(result, name='L北斗'):
    return next(p for p in result['profiles'] if p['machine_name'] == name)


def test_peer_average_excludes_target_and_is_unit_weighted():
    result = study(fixture())
    p = profile(result)
    assert p['relative_diff_coins'] == 850
    assert p['candidate']['peer_estimate_coins'] == 150
    assert p['candidate']['baseline_coins'] == 1000
    assert p['candidate']['adjustment_coins'] <= 500
    assert p['paired_days'] == 80  # not 80*140 seats
    assert not result['model_influence_eligible']
    assert not p['model_influence_eligible']
    assert p['status'] == '比較上の改善なし'


def test_same_dates_compare_errors_and_cutoffs():
    p = profile(study(fixture()))
    assert p['validation']['days'] == 60
    assert p['validation']['baseline_mae_coins'] == 0
    for t in p['validation']['trials']:
        assert date.fromisoformat(t['input_cutoff_date']) == date.fromisoformat(t['date'])-timedelta(days=2)
        assert t['training_end'] <= t['input_cutoff_date']


def test_future_and_other_hall_and_juggler_rows_do_not_change_forecast():
    rows = fixture()
    before = study(rows)
    rows += [{**rows[0], 'report_date':TARGET.isoformat(), 'avg_diff_coins':999999},
             {**rows[0], 'hall_name':'別店舗', 'avg_diff_coins':999999},
             {**rows[0], 'machine_name':'マイジャグラーV', 'avg_diff_coins':999999}]
    after = study(rows)
    assert before['profiles'] == after['profiles']


def test_evaluation_does_not_require_target_day_peer_results_or_use_them():
    rows = fixture()
    before = profile(study(rows))['validation']['trials'][-1]
    last = (START+timedelta(days=79)).isoformat()
    rows = [r for r in rows if r['report_date'] != last or r['machine_name'] == 'L北斗']
    rows[-1]['avg_diff_coins'] = -9999
    after = profile(study(rows))['validation']['trials'][-1]
    assert after['date'] == before['date'] and after['actual_coins'] == -9999
    assert after['predicted_coins'] == before['predicted_coins']
    assert after['baseline_coins'] == before['baseline_coins']


def test_alias_duplicates_do_not_multiply_samples_and_source_not_mutated():
    rows = fixture()
    rows.append({**rows[2], 'machine_name':'スマスロモンキーターン5'})
    original = deepcopy(rows)
    result = study(rows)
    assert len(result['profiles']) == 3 and all(p['paired_days'] == 80 for p in result['profiles'])
    assert result['input_audit']['duplicate_rows'] == 1
    assert rows == original


def test_conflicting_missing_low_activity_rows_are_not_zeroes():
    rows = fixture()
    rows += [{**rows[0], 'avg_diff_coins':-999}]
    rows[3]['avg_diff_coins'] = None
    rows[6]['avg_games'] = None
    rows[9]['avg_games'] = 999
    result = study(rows)
    p = profile(result)
    assert result['input_audit']['conflict_keys'] == 1
    assert result['activity_excluded_rows'] == 2
    assert p['paired_days'] == 76


def test_one_peer_or_short_history_is_not_an_estimate():
    rows = [r for r in fixture() if r['machine_name'] != 'L東京喰種']
    p = profile(study(rows))
    assert p['paired_days'] == 0 and p['candidate'] is None
    assert p['validation']['days'] == 0 and p['validation']['unevaluated_days'] == 60
    p = profile(study(fixture(9)))
    assert p['candidate'] is None and p['status'] == '検証不足'


def test_installation_current_missing_blocks_candidate_but_not_past_trials():
    before = profile(study(fixture()))
    current = {'snapshot_date':(TARGET-timedelta(days=2)).isoformat(), 'machines':['L東京喰種']}
    p = profile(study(fixture(), current))
    assert p['candidate'] is None and p['installation_status'] == '設置一覧に記録なし'
    assert p['validation'] == before['validation']
    future = {**current, 'snapshot_date':TARGET.isoformat()}
    assert profile(study(fixture(), future))['candidate'] == before['candidate']


def test_empty_and_stale_data_do_not_imply_weak_store():
    assert study([])['profiles'] == []
    p = profile(study(fixture(60)))
    assert p['candidate'] is None
    assert '直近14日以内の比較実績なし' in p['blockers']
