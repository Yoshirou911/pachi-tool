"""v3.26 hall × named-event challenger. Never automatically changes rankings.

Public results are retrospective labels, not known high settings or personal wins.
Prediction-day input ends two days before the target (previous-day freeze policy).
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import mean
import math
import re
import unicodedata

from hall.names import canonical_hall_name
from hall.target_validation import build_daily_points
from hall.prediction_quality import audit_rows
from hall.machine_scope import is_smartslot_machine

JST = timezone(timedelta(hours=9))
POLICY = "hall-event-matched-weekday-v1"


def event_key(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s*\([A-Z]ランク\)\s*$", "", value).strip()


def known_day(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        # Legacy SQLite localtime strings can originate on a JST PC or UTC
        # server. UTC interpretation is the later/conservative choice in JST.
        return (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp).astimezone(JST).date()
    except (TypeError, ValueError):
        return None


def load_events(conn, start, end):
    """No migrations/writes. Older schemas lacking timestamps stay reference-only."""
    result = []
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('hall_event', 'hall_event_evidence'):
        if table not in tables:
            continue
        cursor = conn.execute(f'SELECT * FROM {table} WHERE event_date BETWEEN ? AND ?', (start.isoformat(), end.isoformat()))
        keys = [c[0] for c in cursor.description]
        for record in cursor:
            r = dict(zip(keys, record))
            evidence = table == 'hall_event_evidence'
            result.append({**r, 'hall_name':canonical_hall_name(r['hall_name']),
                           'event_title':r.get('event_name') if evidence else r.get('event_title'),
                           'known_at':r.get('scraped_at') if evidence else r.get('created_at'),
                           'prediction_eligible':r.get('analysis_eligible', 0) if evidence else r.get('prediction_eligible', 0),
                           'event_kind':'result_evidence' if evidence else r.get('event_kind'),
                           'label_source':table})
    return result


def _calendar(records, as_of):
    days = defaultdict(set)
    for r in records:
        known = known_day(r.get('known_at'))
        if known is None or known > as_of or r.get('prediction_eligible') not in (1, True):
            continue
        if r.get('source_trust') not in {'A', 'B', 'C'} or r.get('event_kind') in {'report_day', 'web_candidate'}:
            continue
        try:
            day = date.fromisoformat(r['event_date'])
        except (ValueError, TypeError, KeyError):
            continue
        key = event_key(r.get('event_title'))
        if key:
            days[day].add(key)
    return days


def _controls(points, event_day, calendar):
    # Only earlier same-weekday days in an eight-week local window; never
    # label an unregistered day as a verified normal business day.
    return [(d, v) for d, v in points if 0 < (event_day-d).days <= 56
            and d.weekday() == event_day.weekday() and d not in calendar]


def _candidate(points, records, target, key, as_of):
    info = min(as_of, target-timedelta(days=1))
    # A date-only as_of cannot prove an announcement preceded the 13:00
    # freeze. Exclude that entire day's announcements rather than leak 23:00.
    calendar = _calendar(records, info-timedelta(days=1))
    points = [(d, v) for d, v in points if d <= min(info-timedelta(days=1), target-timedelta(days=2))]
    pairs = []
    for d, v in points:
        if calendar.get(d) != {key}:
            continue  # never pool other names / overlapping events
        controls = _controls(points, d, calendar)
        if len(controls) >= 2:
            pairs.append({'date':d.isoformat(), 'actual':v, 'baseline':mean(x[1] for x in controls),
                          'residual':v-mean(x[1] for x in controls), 'controls':[x[0].isoformat() for x in controls]})
    controls = _controls(points, target, calendar)
    adjustment = None
    baseline = mean(v for _, v in controls) if len(controls) >= 2 else None
    if len(pairs) >= 3 and baseline is not None:
        # Fixed conservative challenger; no parameter search on evaluation days.
        adjustment = max(-300, min(300, mean(p['residual'] for p in pairs) * len(pairs)/(len(pairs)+8)))
    target_known = calendar.get(target) == {key}
    return {'paired_days':len(pairs), 'pairs':pairs, 'control_days':len(controls),
            'baseline_coins':round(baseline, 1) if baseline is not None else None,
            'matched_lift_coins':round(mean(p['residual'] for p in pairs), 1) if pairs else None,
            'adjustment_coins':round(adjustment, 1) if adjustment is not None and target_known else None,
            'predicted_coins':round(baseline+adjustment, 1) if adjustment is not None and target_known else None,
            'target_known':target_known, 'input_cutoff_date':min(info-timedelta(days=1), target-timedelta(days=2)).isoformat()}


def analyze_hall_events(rows, records, *, hall_name, target, as_of):
    hall_name = canonical_hall_name(hall_name)
    info = min(as_of, target-timedelta(days=1))-timedelta(days=1)
    records = [dict(r) for r in records if canonical_hall_name(r.get('hall_name')) == hall_name
               and r.get('prediction_eligible') in (1, True)
               and r.get('source_trust') in {'A', 'B', 'C'}
               and r.get('event_kind') not in {'report_day', 'web_candidate'}
               and (known_day(r.get('known_at')) is None or known_day(r.get('known_at')) <= info)]
    rows = [dict(r) for r in rows if canonical_hall_name(r.get('hall_name')) == hall_name
            and is_smartslot_machine(r.get('machine_name', ''))]
    rows, input_audit = audit_rows(rows, cutoff=info, scope='machine')
    points = [(d, v) for d, v in build_daily_points(rows)
              if d <= min(as_of-timedelta(days=1), target-timedelta(days=2)) and math.isfinite(v)]
    profiles = []
    for key in sorted({event_key(r.get('event_title')) for r in records} - {''}):
        named = [r for r in records if event_key(r.get('event_title')) == key]
        candidate = _candidate(points, records, target, key, as_of)
        trials = []
        for day, actual in points:
            if _calendar(records, min(as_of, day-timedelta(days=1))-timedelta(days=1)).get(day) != {key}:
                continue
            trial = _candidate(points, records, day, key, as_of)
            if trial['predicted_coins'] is not None:
                trials.append({'date':day.isoformat(), 'actual_coins':actual,
                               'baseline_coins':trial['baseline_coins'], 'predicted_coins':trial['predicted_coins'],
                               'input_cutoff_date':trial['input_cutoff_date']})
        baseline_mae = mean(abs(t['actual_coins']-t['baseline_coins']) for t in trials) if trials else None
        candidate_mae = mean(abs(t['actual_coins']-t['predicted_coins']) for t in trials) if trials else None
        enough = len(trials) >= 5
        status = ('比較上は改善・未採用' if candidate_mae < baseline_mae else '比較上の改善なし') if enough else '検証不足'
        unknown = sum(known_day(r.get('known_at')) is None for r in named)
        blockers = []
        if not candidate['target_known']:
            blockers.append('対象日の単独イベント予定を事前確認できない（未登録・重複・取得時刻不明を含む）')
        if candidate['paired_days'] < 3:
            blockers.append('同曜日比較が可能な同名イベント実績が3日未満')
        if candidate['control_days'] < 2:
            blockers.append('対象日の同曜日比較日が2日未満')
        if not enough:
            blockers.append('先読みなしで比較できた日が5日未満')
        profiles.append({'event_name':key, **candidate, 'status':status,
                         'registered_dates':len({r['event_date'] for r in named}),
                         'unknown_timestamp_records':unknown,
                         'blockers':blockers,
                         'overlap_days':sum(key in names and len(names)>1 for names in _calendar(records, info).values()),
                         'validation':{'days':len(trials), 'baseline_mae_coins':round(baseline_mae,1) if trials else None,
                                       'candidate_mae_coins':round(candidate_mae,1) if trials else None, 'trials':trials},
                         'source_urls':sorted({r.get('source_url') for r in named if r.get('source_url')}),
                         'model_influence_eligible':False})
    return {'hall_name':hall_name, 'target_date':target.isoformat(), 'policy':POLICY, 'profiles':profiles,
            'information_date':info.isoformat(), 'input_audit':input_audit,
            'model_influence_eligible':False,
            'notice':'店舗×同名イベントの研究用比較。取得時刻不明・後日取得・重複日・別イベントを分離。比較対象は過去56日内の同曜日で当時確認できるイベント登録のない日（通常営業の確認済み日ではありません）。予定も実績も対象日の2日前までに制限。結果データの公開時刻・改訂履歴は未保全のため、本番成績とは区別。高設定・本人勝率ではなく、取得済みスマスロの平均差枚。3.30の採用審査まで順位への加点なし。'}
