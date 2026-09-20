import copy
import sqlite3
from datetime import date, timedelta
from statistics import mean

import pytest

from hall.placement_analysis import analyze_hall_placement

HALL = 'キコーナ四條畷店'
START = date(2026, 7, 1)
TARGET = date(2026, 9, 8)


def rows(count=65):
    # Neighbours have deliberately non-consecutive seat numbers.
    return [dict(hall_name=HALL, report_date=(START+timedelta(days=i)).isoformat(),
                 machine_name='LモンキーターンV', seat_number=n, diff_coins=v+(i%7)*20,
                 games=4000, source_url=f'https://example.com/{i}/{n}')
            for i in range(count) for n,v in ((501,300),(508,200),(503,100),(601,-100),(602,-200))]


def layout(**changes):
    result = dict(id=1, hall_name=HALL, floor_name='1階', valid_from=START.isoformat(),
                  known_at='2026-07-01T08:00:00+09:00', source_kind='manual', verification_status='確認済み',
                  source_url='https://example.com/map', seats=[
                      dict(seat_number=n, machine_name='LモンキーターンV', island_name=island,
                           row_name='通路側', row_order=i+1, x=i*50, y=10, rotation=0)
                      for island,numbers in [('A',[501,508,503]),('B',[601,602])]
                      for i,n in enumerate(numbers)])
    return {**result, **changes}


def maps(value=None):
    value = value or layout()
    return {(START+timedelta(days=i)).isoformat():[copy.deepcopy(value)] for i in range(-2,75)}


def analyze(data=None, **changes):
    return analyze_hall_placement(rows() if data is None else data, hall_name=HALL,
        target=changes.pop('target', TARGET), as_of=changes.pop('as_of', TARGET-timedelta(days=1)),
        layouts_by_cutoff=changes.pop('layouts_by_cutoff', maps()), **changes)


def group(result):
    return next(g for g in result['profiles'] if g['kind']=='row3')


def test_explicit_row_and_island_use_other_same_machine_peers():
    data, placements = rows(), maps()
    before = copy.deepcopy((data, placements))
    result = analyze(data, layouts_by_cutoff=placements)
    p = group(result)
    assert p['seat_numbers'] == [501,508,503]
    assert p['relative_diff_coins'] == 350
    assert p['candidate']['adjustment_coins'] > 0
    assert p['candidate']['adjustment_coins'] <= 500
    assert p['validation']['days'] >= 20
    assert not p['model_influence_eligible'] and not result['model_influence_eligible']
    assert (data, placements) == before


@pytest.mark.parametrize('change', [dict(verification_status='未確認'), dict(source_kind='derived'),
                                  dict(known_at=None), dict(known_at='2026-12-01'), dict(valid_from='2026-12-01')])
def test_untrusted_or_future_maps_do_not_create_groups(change):
    result = analyze(layouts_by_cutoff=maps(layout(**change)))
    assert result['profiles'] == []
    assert result['candidate_groups'] == 0


def test_consecutive_numbers_or_xy_never_imply_a_row():
    m = layout()
    for s in m['seats']:
        s.update(row_name='', row_order=None)
    assert all(p['kind']=='island' for p in analyze(layouts_by_cutoff=maps(m))['profiles'])


def test_row_order_gap_and_duplicate_do_not_form_triplets():
    m = layout()
    m['seats'][1]['row_order'] = 8
    assert all(p['kind']=='island' for p in analyze(layouts_by_cutoff=maps(m))['profiles'])
    m['seats'][1]['row_order'] = 1
    result = analyze(layouts_by_cutoff=maps(m))
    assert all(p['kind']=='island' for p in result['profiles'])
    assert '列内の順番が不正・重複' in result['layout_issues']


def test_expired_latest_map_does_not_resurrect_old_map():
    old, new = layout(), layout(id=2, valid_from='2026-07-10', known_at='2026-07-10', valid_to='2026-08-01')
    mapping = {day:[old,new] for day in maps()}
    result = analyze(layouts_by_cutoff=mapping)
    assert result['profiles'] == []
    assert '配置の有効期限切れ' in result['layout_issues']


def test_no_cross_hall_or_other_machine_leak_and_input_cutoff_is_two_days():
    base = analyze()
    extra = [{**r,'hall_name':'別店舗','diff_coins':9999} for r in rows()]
    extra += [{**rows()[0],'seat_number':1,'machine_name':'マイジャグラーV','diff_coins':9999}]
    extra += [{**r,'report_date':'2026-09-07','diff_coins':99999} for r in rows(1)]
    result = analyze(rows()+extra)
    assert result['profiles'] == base['profiles']
    assert result['input_cutoff_date'] == '2026-09-06'


def test_unverified_new_layout_never_falls_back_to_older_verified_map():
    old=layout()
    new=layout(id=2,valid_from='2026-09-01',known_at='2026-09-01',verification_status='未確認')
    mapping={day:[old,new] for day in maps()}
    result=analyze(layouts_by_cutoff=mapping)
    assert result['profiles']==[]
    assert '最新の配置が未確認・自動生成' in result['layout_issues']


def test_same_group_full_outcomes_required_and_missing_is_not_zero():
    data = rows()
    day = data[-1]['report_date']
    baseline = group(analyze(data))['validation']['trials'][-1]
    changed = [r for r in data if not(r['report_date']==day and r['seat_number']==508)]
    trial = group(analyze(changed))['validation']['trials'][-1]
    assert trial['predicted_coins'] == baseline['predicted_coins']
    assert trial['seat_numbers'] == baseline['seat_numbers']
    assert trial['actual_coins'] is None
    assert trial['unresolved_reason']


def test_low_activity_excludes_complete_group_not_just_that_member():
    data = [{**r, 'games':999 if r['seat_number']==508 else 4000} for r in rows()]
    p = group(analyze(data))
    assert p['candidate'] is None and p['paired_days']==0


def test_alias_duplicates_and_conflicting_outcomes():
    data = rows()
    dup = [{**r,'machine_name':'スマスロモンキーターン5'} for r in data]
    assert group(analyze(data+dup)) == group(analyze(data))
    conflicted = data+[{**data[-5],'diff_coins':9999}]
    assert group(analyze(conflicted))['validation']['trials'][-1]['actual_coins'] is None


def test_future_correction_not_backfilled_and_old_group_trials_preserved():
    baseline = analyze()
    mapping = maps()
    corrected = layout(known_at='2026-09-05')
    corrected['seats'][0]['x'] = 50
    for day in mapping:
        if day >= '2026-09-05':
            mapping[day] = [corrected]
    result = analyze(layouts_by_cutoff=mapping)
    assert group(result)['candidate'] is None
    old = next(p for p in result['retired_group_validations'] if p['kind']=='row3')
    assert old['validation']['trials'] == group(baseline)['validation']['trials']


def test_replacement_a_b_a_breaks_group_and_transition():
    data = rows()
    data += [{**data[0],'report_date':'2026-09-04','machine_name':'マイジャグラーV','diff_coins':None}]
    data += [{**r,'report_date':'2026-09-05'} for r in rows(1)]
    result = analyze(data)
    assert group(result)['candidate'] is None
    assert result['retired_group_validations']
    assert all(not(p['seat_number']==501 and p['date']=='2026-09-05')
               for t in result['transitions'] for p in t['observations'])


def test_paired_mae_and_temporal_cutoffs():
    v = group(analyze())['validation']
    answered = [t for t in v['trials'] if t['actual_coins'] is not None]
    assert v['candidate_mae_coins'] == round(mean(abs(t['actual_coins']-t['predicted_coins']) for t in answered),1)
    for t in v['trials']:
        assert date.fromisoformat(t['training_to']) <= date.fromisoformat(t['date'])-timedelta(days=2)


def test_consecutive_calendar_days_zero_separate_and_no_implied_setting():
    data = rows(3)
    data[0]['diff_coins'] = 0
    # A two-day gap must not count as "next day".
    data = [r for r in data if not(r['seat_number']==508 and r['report_date']=='2026-07-02')]
    result = analyze(data, layouts_by_cutoff={}, target=date(2026,7,5), as_of=date(2026,7,4))
    zero = next(t for t in result['transitions'] if t['previous_state']=='zero')
    assert zero['pairs'] == 1
    assert not any(p['seat_number']==508 for t in result['transitions'] for p in t['observations'])
    assert result['setting_evidence']['pairs']==0
    assert result['candidate_groups']==0


def test_exact_setting_values_require_dated_nonconflicting_evidence():
    evidence = [dict(hall_name=HALL,machine_name='LモンキーターンV',seat_number=501,
        date=d,known_at=d,setting_evidence_level='confirmed_exact',confirmed_setting=s)
        for d,s in [('2026-08-01',1),('2026-08-02',4),('2026-08-03',4),('2026-08-04',2)]]
    result = analyze(setting_records=evidence)['setting_evidence']
    assert (result['higher'],result['same_value'],result['lower']) == (1,1,1)
    assert '同設定への変更' in result['notice']
    future = [{**r,'known_at':'2026-10-01'} for r in evidence]
    assert analyze(setting_records=future)['setting_evidence']['pairs']==0
    hints = [{**r,'setting_evidence_level':'strong_hint'} for r in evidence]
    assert analyze(setting_records=hints)['setting_evidence']['pairs']==0
    conflict = evidence+[{**evidence[1],'confirmed_setting':5}]
    assert analyze(setting_records=conflict)['setting_evidence']['pairs']==1


def test_layout_schema_round_trip_and_invalid_row_positions(tmp_path, monkeypatch):
    from api.routers import layout as router
    monkeypatch.setattr(router,'HALL_REPORTS_DB',tmp_path/'map.db')
    m=layout()
    body=router.LayoutInput(hall_name=HALL,valid_from='2026-07-01',verification_status='確認済み',seats=m['seats'])
    saved=router.save_layout(body)
    assert saved['seats'][0]['row_name']=='通路側'
    assert saved['seats'][0]['row_order']==1
    for bad in [dict(row_name='通路側',row_order=None),dict(row_name='',row_order=1),dict(island_name='',row_name='通路側',row_order=1)]:
        with pytest.raises(ValueError):
            router.LayoutInput(hall_name=HALL,valid_from='2026-07-01',seats=[{**m['seats'][0],**bad}])
    with pytest.raises(ValueError):
        router.LayoutInput(hall_name=HALL,valid_from='2026-07-01',seats=[m['seats'][0],{**m['seats'][1],'row_order':1}])


def test_legacy_schema_migrates_without_deleting_data(tmp_path, monkeypatch):
    from api.routers import layout as router
    path=tmp_path/'legacy.db'
    con=sqlite3.connect(path)
    con.executescript("CREATE TABLE hall_layout_seat(id INTEGER PRIMARY KEY,layout_id INTEGER,seat_number INTEGER); INSERT INTO hall_layout_seat VALUES(1,1,101);")
    con.close()
    monkeypatch.setattr(router,'HALL_REPORTS_DB',path)
    con=router.init_layout_db()
    assert tuple(con.execute('SELECT seat_number,row_name,row_order FROM hall_layout_seat').fetchone())==(101,'',None)
    con.close()


def test_confirmed_snapshot_filters_later_edits_and_personal_data(tmp_path, monkeypatch):
    from records import models
    monkeypatch.setattr(models,'DB_PATH',tmp_path/'sessions.db')
    models.init_db()
    models.save_session(models.Session(date='2026-08-01',hall_name=HALL,machine_name='L北斗',seat_number=1,
        setting_evidence_level='confirmed_exact',confirmed_setting=4,notes='private',investment=5000))
    con=sqlite3.connect(models.DB_PATH)
    con.execute("UPDATE sessions SET created_at='2026-08-01',updated_at='2026-08-01'")
    con.commit()
    data=models.confirmed_setting_snapshot('2026-07-01','2026-08-03')
    assert len(data)==1 and 'notes' not in data[0] and 'investment' not in data[0]
    con.execute("UPDATE sessions SET updated_at='2026-09-01'")
    con.commit()
    con.close()
    assert models.confirmed_setting_snapshot('2026-07-01','2026-08-03')==[]
