"""Prospective calibration/interval diagnostics, never a live decision input.

Fit only on outcomes available before the prediction started, grouped by exact
source implementation, model and scope. No retrospective shadow backfill.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from statistics import mean

from hall.prediction_benchmark import MODELS, PROTOCOL as COMPARISON_PROTOCOL, SPEC as COMPARISON_SPEC, subject_key
from hall.prediction_log import JST, POLICY, dumps

PROTOCOL = 'probability-interval-shadow-v1'
SPEC = {'protocol': PROTOCOL, 'training_days': 120, 'evaluation_days': 84,
        'minimum_training_dates': 30, 'minimum_bin_dates': 10, 'prior_days': 10,
        'bins': 5, 'interval_level': .8, 'minimum_evaluation_dates': 30,
        'minimum_evaluation_weeks': 8, 'minimum_coverage': .8,
        'freshness_days': 14, 'minimum_class_dates': 5,
        'weighting': '対象→店舗日→日を等重み。予測幅は日ごとの最大絶対誤差から算出',
        'label': '公開実績の差枚 > 0（±0は非プラス）。高設定・本人収支とは別',
        'live_applied': False}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


@lru_cache(maxsize=1)
def implementation_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result.astimezone(JST)


def valid_interval(value):
    return (isinstance(value, dict) and value.get('level_pct') == 80
            and finite(value.get('low')) and finite(value.get('high')) and value['low'] <= value['high'])


def cohort(sample):
    return sample['version'], sample['source_hash'], sample['scope'], sample['model']


def day_values(rows, value):
    halls, days = defaultdict(list), defaultdict(list)
    for r in rows:
        halls[r['target_date'], r['hall_name']].append(value(r))
    for (day, _), values in halls.items():
        days[day].append(mean(values))
    return {day: mean(values) for day, values in sorted(days.items())}


def average(rows, value):
    values = day_values(rows, value)
    return mean(values.values()) if values else None


def read_samples(conn, *, since, as_of):
    """Read immutable archives and ledger outcomes, never query/rewrite raw results."""
    as_of = stamp(as_of.isoformat())
    excluded, samples = Counter(), []
    if conn is None:
        return samples, excluded
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'prediction_batch', 'prediction_subject', 'prediction_outcome'} <= tables:
        return samples, excluded
    batches = conn.execute('SELECT * FROM prediction_batch WHERE region=? AND policy=? AND target_date>=? AND target_date<? ORDER BY target_date,id',
                           ('shijonawate', POLICY, since.isoformat(), as_of.date().isoformat())).fetchall()
    for original in batches:
        b = dict(original)
        try:
            payload = json.loads(b['payload'])
            target = date.fromisoformat(b['target_date'])
            saved, started = stamp(b['saved_at']), stamp(b['started_at'])
            comparison = payload.get('comparison', {})
            if comparison.get('protocol') != COMPARISON_PROTOCOL or comparison.get('spec') != COMPARISON_SPEC:
                excluded['比較条件が未保存・旧方式'] += 1
                continue
            if (digest(payload) != b['payload_sha256'] or not started <= saved < as_of
                    or saved.date() >= target or date.fromisoformat(b['cutoff_date']) >= started.date()
                    or comparison.get('input_cutoff_date') != b['cutoff_date']
                    or date.fromisoformat(b['cutoff_date']) > target-timedelta(days=2)):
                raise ValueError('archive integrity/date')
            subjects = {subject_key(json.loads(r['payload'])): dict(r) for r in conn.execute(
                'SELECT s.*,o.actual,o.resolved_at,o.evidence FROM prediction_subject s LEFT JOIN prediction_outcome o ON o.subject_id=s.id WHERE s.batch_id=?', (b['id'],))}
            shadow = payload.get('probability_validation', {})
            shadow_valid = (shadow.get('protocol') == PROTOCOL and shadow.get('spec') == SPEC
                            and shadow.get('implementation_hash') == implementation_hash())
            frozen = {(tuple(s['key']), s['model']): s for s in shadow.get('samples', [])} if shadow_valid else {}
            source_hash = payload.get('model_selection', {}).get('implementation_hash') or f"legacy:{b['version']}"
            seen, batch_rows = set(), []
            for item in comparison.get('samples', []):
                key = subject_key(item)
                if key in seen or item['target_date'] != b['target_date'] or key[0] not in {'machine', 'seat'}:
                    raise ValueError('duplicate/date/scope')
                seen.add(key)
                subject = subjects.get(key)
                if subject is None:
                    raise ValueError('missing subject')
                current = item['models']['current']
                if current['probability'] != subject['probability'] or current['projected'] != subject['projected']:
                    raise ValueError('subject mismatch')
                actual, known_at, reason = None, None, '結果待ち'
                if subject['resolved_at'] is not None:
                    known = stamp(subject['resolved_at'])
                    if known.date() <= target:
                        raise ValueError('premature outcome')
                    if known <= as_of:
                        evidence = json.loads(subject['evidence']).get('row', {})
                        actual_value = evidence.get('avg_diff_coins' if key[0] == 'machine' else 'diff_coins')
                        games = evidence.get('avg_games' if key[0] == 'machine' else 'games')
                        if (subject_key({**evidence, 'scope': key[0]}) != key or evidence.get('report_date') != b['target_date']
                                or not finite(actual_value) or actual_value != subject['actual']):
                            raise ValueError('outcome mismatch')
                        if finite(games) and games >= 1000:
                            actual, known_at, reason = actual_value, known.isoformat(), None
                        else:
                            reason = '稼働不明・1000G未満'
                original_subject = json.loads(subject['payload'])
                for model, values in item['models'].items():
                    p, projected = values.get('probability'), values.get('projected')
                    if model not in MODELS or not finite(p) or not 0 <= p <= 1 or not finite(projected):
                        raise ValueError('invalid forecast')
                    s = frozen.get((key, model))
                    if s is not None and (s.get('probability') != p or s.get('projected') != projected
                                          or s.get('source_hash') != source_hash):
                        raise ValueError('shadow mismatch')
                    batch_rows.append({'key': list(key), 'hall_name': key[1], 'scope': key[0], 'model': model,
                        'version': b['version'], 'source_hash': source_hash, 'target_date': b['target_date'],
                        'started_at': started.isoformat(), 'batch_id': b['id'], 'subject_id': subject['id'],
                        'probability': p, 'projected': projected, 'actual': actual, 'known_at': known_at,
                        'unresolved_reason': reason, 'shadow': s,
                        'existing_interval': original_subject.get('forecast_interval_coins') if model == 'current' else None})
            samples.extend(batch_rows)
        except (ValueError, TypeError, KeyError, OverflowError):
            excluded['記録の整合性・保存時点違反'] += 1
    return samples, excluded


def fit(history):
    """Daily weights for probability bins; daily maxima for conservative ranges."""
    dates = sorted({r['target_date'] for r in history})
    bins, radius = [], None
    for index in range(SPEC['bins']):
        rows = [r for r in history if min(4, int(r['probability']*5)) == index]
        rates = day_values(rows, lambda r: float(r['actual'] > 0))
        bins.append({'days': len(rates), 'success_days': sum(rates.values())})
    if len(dates) >= SPEC['minimum_training_dates']:
        maxima = defaultdict(float)
        for r in history:
            maxima[r['target_date']] = max(maxima[r['target_date']], abs(r['actual']-r['projected']))
        errors = sorted(maxima.values())
        rank = math.ceil((len(errors)+1)*SPEC['interval_level'])
        radius = errors[rank-1] if rank <= len(errors) else None
    return {'days': len(dates), 'bins': bins, 'radius': radius,
            'first_date': dates[0] if dates else None, 'last_date': dates[-1] if dates else None,
            'last_known_at': max((r['known_at'] for r in history), default=None),
            'evidence_sha256': digest([(r['batch_id'], r['subject_id'], r['model'], r['actual'], r['known_at']) for r in history])}


def predict(values, fitted):
    p = values['probability']
    band = fitted['bins'][min(4, int(p*5))]
    sufficient = fitted['days'] >= SPEC['minimum_training_dates'] and band['days'] >= SPEC['minimum_bin_dates']
    corrected = (p*SPEC['prior_days']+band['success_days'])/(SPEC['prior_days']+band['days']) if sufficient else None
    radius = fitted['radius']
    return {'calibrated_probability': corrected, 'interval': None if radius is None else {
        'level_pct': 80, 'low': values['projected']-radius, 'high': values['projected']+radius},
        'bin_days': band['days'], 'reason': None if sufficient else '補正用の日数不足（全体30日・確率帯10日が必要）'}


def freeze_calibration(conn, prediction, *, version, started_at):
    from hall.model_selection import implementation_hash as source_implementation
    started_at = stamp(started_at.isoformat())
    history, excluded = read_samples(conn, since=started_at.date()-timedelta(days=SPEC['training_days']), as_of=started_at)
    source_hash = source_implementation()
    fitted, snapshots = {}, []
    for item in prediction['comparison']['samples']:
        for model, values in item['models'].items():
            if not finite(values.get('probability')) or not 0 <= values['probability'] <= 1 or not finite(values.get('projected')):
                raise ValueError('invalid probability/point forecast')
            key = (version, source_hash, item['scope'], model)
            if key not in fitted:
                eligible = [r for r in history if cohort(r) == key and r['actual'] is not None
                            and stamp(r['known_at']) < started_at and r['target_date'] <= prediction['input_cutoff_date']]
                fitted[key] = fit(eligible)
            result = predict(values, fitted[key])
            if excluded['記録の整合性・保存時点違反']:
                result = {'calibrated_probability': None, 'interval': None, 'bin_days': 0, 'reason': '過去記録の整合性を要確認'}
            snapshots.append({'key': list(subject_key(item)), 'model': model, 'source_hash': source_hash,
                              **values, **result})
    return {'protocol': PROTOCOL, 'spec': SPEC.copy(), 'implementation_hash': implementation_hash(),
            'training': [{'version': k[0], 'source_hash': k[1], 'scope': k[2], 'model': k[3], **v} for k, v in fitted.items()],
            'exclusions': dict(excluded), 'samples': snapshots, 'live_applied': False}


def interval_metrics(rows, field):
    usable = [r for r in rows if valid_interval(r.get(field))]
    def score(r):
        low, high, actual = r[field]['low'], r[field]['high'], r['actual']
        return high-low + 10*max(low-actual, actual-high, 0)
    return {'count': len(usable), 'days': len({r['target_date'] for r in usable}),
            'coverage_pct': average(usable, lambda r: 100*int(r[field]['low'] <= r['actual'] <= r[field]['high'])),
            'mean_width': average(usable, lambda r: r[field]['high']-r[field]['low']),
            'interval_score': average(usable, score)}


def summarize(rows, *, end):
    answered = [r for r in rows if r['actual'] is not None]
    paired = [r for r in answered if r['shadow'] and finite(r['shadow'].get('calibrated_probability'))
              and 0 <= r['shadow']['calibrated_probability'] <= 1]
    def brier(r, corrected=False):
        p = r['shadow']['calibrated_probability'] if corrected else r['probability']
        return (p-int(r['actual'] > 0))**2
    days = sorted({r['target_date'] for r in paired})
    weeks = {date.fromisoformat(d).isocalendar()[:2] for d in days}
    improvement = day_values(paired, lambda r: brier(r)-brier(r, True))
    halves = [mean(list(improvement.values())[a:b]) if list(improvement.values())[a:b] else None
              for a,b in [(0,len(days)//2),(len(days)//2,len(days))]]
    reasons = []
    if len(days) < SPEC['minimum_evaluation_dates'] or len(weeks) < SPEC['minimum_evaluation_weeks']:
        reasons.append('補正前後の照合が30日・8週に不足')
    issued = [r for r in rows if r['shadow'] and r['shadow'].get('calibrated_probability') is not None]
    if not issued or len(paired)/len(issued) < SPEC['minimum_coverage']:
        reasons.append('補正を事前保存した対象の照合率が80%未満')
    if not days or (end-date.fromisoformat(days[-1])).days > SPEC['freshness_days']:
        reasons.append('直近14日内の照合なし')
    if any(len({r['target_date'] for r in paired if (r['actual'] > 0) == positive}) < SPEC['minimum_class_dates'] for positive in (True,False)):
        reasons.append('プラス・非プラスそれぞれ5日の実績が必要')
    improved = all(v is not None and v > 0 for v in halves)
    if not reasons and not improved:
        reasons.append('前半・後半の両方で確率誤差が改善していない')
    bands = []
    for index in range(5):
        members = [r for r in answered if min(4,int(r['probability']*5)) == index]
        bands.append({'low_pct': index*20, 'high_pct': (index+1)*20, 'count': len(members),
                      'days': len({r['target_date'] for r in members}),
                      'predicted_pct': average(members, lambda r: 100*r['probability']),
                      'actual_pct': average(members, lambda r: 100*int(r['actual'] > 0))})
    interval_rows = [{**r, 'candidate_interval': (r['shadow'] or {}).get('interval')} for r in answered]
    interval_pairs = [r for r in interval_rows if valid_interval(r['candidate_interval']) and valid_interval(r['existing_interval'])]
    return {'saved': len(rows), 'resolved': len(answered), 'pending': len(rows)-len(answered),
            'days': len({r['target_date'] for r in answered}), 'paired': len(paired), 'paired_days': len(days), 'paired_weeks': len(weeks),
            'shadow_saved': len(issued), 'reasons': reasons, 'status': 'hold' if reasons else 'reference_improved',
            'raw_brier': average(answered, brier), 'paired_before_brier': average(paired, brier),
            'paired_after_brier': average(paired, lambda r: brier(r,True)), 'half_improvements': halves,
            'bands': bands, 'unresolved_reasons': dict(Counter(r['unresolved_reason'] for r in rows if r['actual'] is None)),
            'candidate_interval': interval_metrics(interval_rows, 'candidate_interval'),
            'existing_interval': interval_metrics(interval_rows, 'existing_interval'),
            'paired_intervals': {'before': interval_metrics(interval_pairs, 'existing_interval'), 'after': interval_metrics(interval_pairs, 'candidate_interval')},
            'live_applied': False}


def validation_report(conn, *, as_of=None):
    as_of = stamp((as_of or datetime.now(JST)).isoformat())
    end = as_of.date()-timedelta(days=1)
    start = end-timedelta(days=SPEC['evaluation_days']-1)
    samples, excluded = read_samples(conn, since=start, as_of=as_of)
    groups = defaultdict(list)
    for row in samples:
        groups[cohort(row)].append(row)
    items = []
    for (version, source, scope, model), rows in sorted(groups.items()):
        item = summarize(rows, end=end)
        if excluded['記録の整合性・保存時点違反']:
            item['status'] = 'hold'
            item['reasons'].append('保存記録の整合性を確認できないため保留')
        items.append({'version': version, 'source_hash': source, 'scope': scope, 'model': model,
                      'label': MODELS[model], **item})
    return {'protocol': PROTOCOL, 'spec': SPEC, 'as_of': as_of.isoformat(), 'window_start': start.isoformat(),
            'window_end': end.isoformat(), 'items': items, 'exclusions': dict(excluded), 'live_applied': False,
            'notice': '現行（既存補正を含む）と試験補正を、同じ事前保存対象・結果で比較。旧予測へ補正値や幅を後付けしません。',
            'scope_notice': '機種平均と台番号、版・実装・方式を分離。新しいイベント・機種研究・台順位・並びモデルには同一定義の確率出力がないため対象外。',
            'interval_notice': '80%は予測幅の目標値で保証ではありません。日別最大誤差を使うため広めになります。時系列相関・掲載の偏りは残り、本人の収支幅ではありません。'}
