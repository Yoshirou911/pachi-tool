from datetime import date, timedelta
import sqlite3

from hall.event_prediction import analyze_hall_events, event_key, known_day, load_events


def fixture():
    start = date(2026, 1, 1)
    event_days = set(range(28, 168, 14))
    rows = [{'hall_name':'キコーナ四条畷店', 'report_date':(start+timedelta(days=i)).isoformat(),
             'machine_name':'L北斗', 'avg_diff_coins':600 if i in event_days else 0,
             'avg_games':5000, 'unit_count':10} for i in range(168)]
    events = [{'hall_name':'キコーナ四條畷店', 'event_date':(start+timedelta(days=i)).isoformat(),
               'event_title':'取材A', 'known_at':(start+timedelta(days=i-5)).isoformat(),
               'prediction_eligible':1, 'source_trust':'B', 'event_kind':'media_schedule',
               'source_url':'https://example.com/a'} for i in sorted(event_days|{168})]
    return rows, events, start+timedelta(days=168)


def study(rows, events, target):
    return analyze_hall_events(rows, events, hall_name='キコーナ四條畷店', target=target, as_of=target-timedelta(days=1))


def test_named_event_challenger_is_paired_and_never_auto_adopted():
    rows, events, target = fixture()
    result = study(rows, events, target)
    p = result['profiles'][0]
    assert p['paired_days'] == 10
    assert p['matched_lift_coins'] == 600
    assert p['validation']['days'] == 7
    assert p['validation']['candidate_mae_coins'] < p['validation']['baseline_mae_coins']
    assert p['predicted_coins'] == 300
    assert p['status'] == '比較上は改善・未採用'
    assert not result['model_influence_eligible'] and not p['model_influence_eligible']
    for trial in p['validation']['trials']:
        assert date.fromisoformat(trial['input_cutoff_date']) == date.fromisoformat(trial['date'])-timedelta(days=2)


def test_future_results_and_other_halls_do_not_change_predictions():
    rows, events, target = fixture()
    before = study(rows, events, target)['profiles']
    rows += [{**rows[0], 'report_date':target.isoformat(), 'avg_diff_coins':999999},
             {**rows[0], 'hall_name':'別の店', 'avg_diff_coins':999999}]
    events += [{**events[0], 'hall_name':'別の店', 'event_title':'他のイベント'}]
    assert study(rows, events, target)['profiles'] == before


def test_unknown_and_late_timestamps_cannot_backfill_validation():
    rows, events, target = fixture()
    unknown = [{**e, 'known_at':None} for e in events]
    p = study(rows, unknown, target)['profiles'][0]
    assert p['unknown_timestamp_records'] == 11 and p['predicted_coins'] is None
    late = [{**e, 'known_at':(target-timedelta(days=2)).isoformat()} for e in events]
    p = study(rows, late, target)['profiles'][0]
    assert p['validation']['days'] == 0  # retrospectively seen events are not preannouncements
    assert p['paired_days'] == 10
    future = [{**e, 'known_at':target.isoformat()} for e in events]
    assert study(rows, future, target)['profiles'] == []
    previous_night = [{**e, 'known_at':(target-timedelta(days=1)).isoformat()+'T23:00:00+09:00'} for e in events]
    assert study(rows, previous_night, target)['profiles'] == []


def test_duplicate_events_and_rank_suffix_do_not_multiply_days():
    rows, events, target = fixture()
    before = study(rows, events, target)['profiles'][0]
    events += [{**e, 'event_title':'取材A（Bランク）'} for e in events]
    after = study(rows, events, target)['profiles'][0]
    assert after['paired_days'] == before['paired_days']
    assert after['validation'] == before['validation']
    assert event_key('取材B') != event_key('取材A')


def test_multiple_event_day_and_distinct_names_not_pooled():
    rows, events, target = fixture()
    events.append({**events[-1], 'event_title':'取材B'})
    p = study(rows, events, target)['profiles'][0]
    assert p['predicted_coins'] is None and p['overlap_days'] == 1
    other = study(rows, events, target)['profiles'][1]
    assert other['paired_days'] == 0


def test_data_conflicts_missing_values_and_other_machine_scopes_excluded():
    rows, events, target = fixture()
    rows += [dict(rows[28]), {**rows[42], 'avg_diff_coins':-100},
             {**rows[0], 'machine_name':'マイジャグラーV', 'avg_diff_coins':999999}]
    p = study(rows, events, target)['profiles'][0]
    assert p['paired_days'] == 9  # duplicate deduped; conflicting day held out


def test_result_article_is_not_an_event_and_no_controls_is_not_zero():
    rows, events, target = fixture()
    blocked = [{**e, 'event_kind':'report_day'} for e in events]
    assert study(rows, blocked, target)['profiles'] == []
    rows = [r for r in rows if r['avg_diff_coins'] == 600]
    p = study(rows, events, target)['profiles'][0]
    assert p['control_days'] == 0 and p['predicted_coins'] is None


def test_timezone_and_legacy_schema_are_read_only():
    assert known_day('2026-08-01T16:00:00Z') == date(2026,8,2)
    assert known_day('bad') is None
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE hall_event(hall_name TEXT,event_date TEXT,event_title TEXT)')
    c.execute("INSERT INTO hall_event VALUES ('店','2026-08-01','取材')")
    before = c.total_changes
    items = load_events(c, date(2026,8,1), date(2026,8,2))
    assert items[0]['known_at'] is None and c.total_changes == before
    c.close()
