"""Read-only, fixed-window monitoring of archived forecasts; never a live gate.

Alerts are descriptive review prompts, not significance tests or model adoption.
Use a common subject panel and fully observed days to avoid outcome cherry-picking.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import mean

from hall.prediction_log import JST
from hall.prediction_benchmark import MODELS
from hall.probability_validation import read_samples, stamp

PROTOCOL = 'forecast-monitor-v1'
SPEC = {
    'protocol': PROTOCOL, 'baseline_days': 56, 'recent_days': 28,
    'minimum_baseline_days': 28, 'minimum_baseline_weeks': 6,
    'minimum_recent_days': 14, 'minimum_recent_weeks': 3,
    'minimum_half_days': 7, 'minimum_resolution': .8,
    'minimum_panel_share': .8, 'freshness_days': 7,
    'brier_increase': .05, 'mae_increase_coins': 100, 'mae_increase_ratio': .2,
    'outcome_shift_coins': 500, 'positive_shift_points': 20,
    'weighting': '両期間共通の対象全件が保存・照合された日のみ。対象平均→日を等重み',
    'label': '公開差枚が0枚超（±0は非プラス）。高設定・本人勝率とは別',
    'live_applied': False,
}


def summarize_window(rows, panel, *, start, end):
    rows = [r for r in rows if start.isoformat() <= r['target_date'] <= end.isoformat()]
    selected = [r for r in rows if tuple(r['key']) in panel]
    by_day = defaultdict(list)
    for r in selected:
        by_day[r['target_date']].append(r)
    daily = []
    for day, members in sorted(by_day.items()):
        if {tuple(r['key']) for r in members} != panel or any(r['actual'] is None for r in members):
            continue
        daily.append({'date': day, 'subjects': len(members),
            'brier': mean((r['probability'] - int(r['actual'] > 0))**2 for r in members),
            'mae_coins': mean(abs(r['projected'] - r['actual']) for r in members),
            'avg_diff_coins': mean(r['actual'] for r in members),
            'positive_pct': mean(100*int(r['actual'] > 0) for r in members)})
    answered = sum(r['actual'] is not None for r in selected)
    saved_days = len({r['target_date'] for r in rows})
    days = (end-start).days+1
    return {'start': start.isoformat(), 'end': end.isoformat(), 'calendar_days': days,
        'saved_days': saved_days, 'unrecorded_days': days-saved_days,
        'saved_subjects': len(rows), 'panel_saved_subjects': len(selected),
        'panel_share': len(selected)/len(rows) if rows else None,
        'resolved': answered, 'pending': len(selected)-answered,
        'resolution': answered/len(selected) if selected else None,
        'complete_days': len(daily), 'incomplete_days': saved_days-len(daily),
        'weeks': len({date.fromisoformat(r['date']).isocalendar()[:2] for r in daily}),
        'latest_complete_date': daily[-1]['date'] if daily else None,
        'metrics': {k: mean(r[k] for r in daily) if daily else None
                    for k in ('brier', 'mae_coins', 'avg_diff_coins', 'positive_pct')},
        'unresolved_reasons': dict(Counter(r['unresolved_reason'] or '結果待ち' for r in selected if r['actual'] is None)),
        'daily': daily}


def compare_cohort(rows, *, end):
    recent_start = end-timedelta(days=SPEC['recent_days']-1)
    baseline_end = recent_start-timedelta(days=1)
    start = recent_start-timedelta(days=SPEC['baseline_days'])
    rows = [r for r in rows if start.isoformat() <= r['target_date'] <= end.isoformat()]
    previous = [r for r in rows if r['target_date'] < recent_start.isoformat()]
    recent = [r for r in rows if r['target_date'] >= recent_start.isoformat()]
    panel = {tuple(r['key']) for r in previous} & {tuple(r['key']) for r in recent}
    # Fail closed if a subject/date was repeated, even with identical values.
    identities = [(r['target_date'], tuple(r['key'])) for r in rows]
    duplicate = len(identities) != len(set(identities))
    safe_rows = [] if duplicate else rows
    baseline = summarize_window(safe_rows, panel, start=start, end=baseline_end)
    latest = summarize_window(safe_rows, panel, start=recent_start, end=end)
    middle = recent_start+timedelta(days=14)
    halves = [summarize_window(safe_rows, panel, start=recent_start, end=middle-timedelta(days=1)),
              summarize_window(safe_rows, panel, start=middle, end=end)]
    reasons = []
    if duplicate:
        reasons.append('同じ対象・日の保存が重複しているため集計を保留')
    if not panel:
        reasons.append('両期間に共通する機種・台番号の事前記録がない')
    for label, window, minimum_days, minimum_weeks in (
            ('以前56日', baseline, SPEC['minimum_baseline_days'], SPEC['minimum_baseline_weeks']),
            ('直近28日', latest, SPEC['minimum_recent_days'], SPEC['minimum_recent_weeks'])):
        if window['complete_days'] < minimum_days or window['weeks'] < minimum_weeks:
            reasons.append(f'{label}の全件照合が{minimum_days}日・{minimum_weeks}週に不足')
        if window['resolution'] is None or window['resolution'] < SPEC['minimum_resolution']:
            reasons.append(f'{label}の共通対象の照合率が80%未満')
        if window['panel_share'] is None or window['panel_share'] < SPEC['minimum_panel_share']:
            reasons.append(f'{label}で共通対象が保存件数の80%未満（対象構成の変化）')
    if any(h['complete_days'] < SPEC['minimum_half_days'] for h in halves):
        reasons.append('直近の前半・後半それぞれに全件照合7日が必要')
    last = latest['latest_complete_date']
    if last is None or (end-date.fromisoformat(last)).days > SPEC['freshness_days']:
        reasons.append('直近7日内に共通対象全件の照合がない')
    b, r = baseline['metrics'], latest['metrics']
    delta = {k: r[k]-b[k] if r[k] is not None and b[k] is not None else None for k in b}
    alerts = []
    if not reasons:
        half_metrics = [h['metrics'] for h in halves]
        # Require the same signal in both recent halves; thresholds are fixed,
        # not tuned against the displayed outcomes. No statistical claim.
        if all(h['brier']-b['brier'] >= SPEC['brier_increase'] for h in half_metrics):
            alerts.append({'kind': 'forecast', 'metric': 'brier', 'message': '確率予測の誤差が直近の前半・後半とも拡大'})
        mae_limit = max(SPEC['mae_increase_coins'], b['mae_coins']*SPEC['mae_increase_ratio'])
        if all(h['mae_coins']-b['mae_coins'] >= mae_limit for h in half_metrics):
            alerts.append({'kind': 'forecast', 'metric': 'mae_coins', 'message': '差枚予測の誤差が直近の前半・後半とも拡大'})
        for key, threshold in [('avg_diff_coins', SPEC['outcome_shift_coins']), ('positive_pct', SPEC['positive_shift_points'])]:
            shifts = [h[key]-b[key] for h in half_metrics]
            if all(v >= threshold for v in shifts) or all(v <= -threshold for v in shifts):
                alerts.append({'kind': 'observed', 'metric': key,
                    'message': '共通対象の公開実績に同方向の変化。営業方針の変更と断定はできません'})
    return {'status': 'insufficient_data' if reasons else 'review' if alerts else 'no_alert',
        'reasons': reasons, 'alerts': alerts, 'common_subjects': len(panel),
        'baseline': baseline, 'recent': latest, 'halves': halves, 'delta': delta, 'live_applied': False}


def monitor_report(conn, *, as_of=None):
    as_of = stamp((as_of or datetime.now(JST)).isoformat())
    end = as_of.date()-timedelta(days=1)
    start = end-timedelta(days=SPEC['baseline_days']+SPEC['recent_days']-1)
    samples, excluded = read_samples(conn, since=start, as_of=as_of)
    groups = defaultdict(list)
    for r in samples:
        groups[r['version'], r['source_hash'], r['hall_name'], r['scope'], r['model']].append(r)
    items = []
    for (version, source, hall, scope, model), rows in sorted(groups.items()):
        result = compare_cohort(rows, end=end)
        if excluded['記録の整合性・保存時点違反'] or source.startswith('legacy:'):
            result['status'] = 'insufficient_data'
            result['alerts'] = []
            result['reasons'].append('保存記録の整合性または実装識別を確認できないため保留')
        items.append({'version': version, 'source_hash': source, 'hall_name': hall,
                      'scope': scope, 'model': model, 'label': MODELS[model], **result})
    recorded = len({r['target_date'] for r in samples})
    return {'protocol': PROTOCOL, 'spec': SPEC, 'as_of': as_of.isoformat(),
        'window_start': start.isoformat(), 'window_end': end.isoformat(),
        'recorded_days': recorded, 'unassessed_calendar_days': 84-recorded,
        'items': items, 'exclusions': dict(excluded), 'live_applied': False,
        'notice': '保存した予測の事後点検です。警告は統計的な確定判定ではなく、方式・順位・着席判定を自動変更しません。',
        'scope_notice': '四條畷周辺の同じ版・実装・店舗・方式・機種平均／台番号を分離。両期間共通の対象だけを比較し、全対象の結果が揃わない日は成績に含めません。',
        'bias_notice': '公開・稼働・イベント・対象構成の偏りは残ります。差枚の変化だけで設定変更や回収店と断定しません。警告なしも精度や安全の保証ではありません。'}
