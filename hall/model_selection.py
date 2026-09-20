"""v3.30: fixed, prospective adoption review; never switches live predictions.

Retrospective trials are descriptive only. A review compares frozen forecasts
on the same subjects/dates, clusters by week, and retains all previous reports.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from statistics import mean

from hall.names import canonical_hall_name
from hall.machine_scope import normalize_machine_key
from hall.prediction_log import dumps

PROTOCOL = 'prospective-adoption-review-v1'
FAMILIES = {'event': '店舗×イベント', 'machine': '店舗×機種', 'seat': '台番号', 'placement': '並び・島'}
SPEC = {'protocol': PROTOCOL, 'window_days': 84, 'minimum_days': 30, 'minimum_weeks': 8, 'freshness_days': 14,
        'minimum_coverage': .8, 'minimum_improvement_coins': 50, 'minimum_improvement_ratio': .05,
        'bootstrap_draws': 2000, 'lower_quantile': .05 / (4 * 2), 'seed': 330,
        'unit': '1台当たり差枚の平均絶対誤差', 'aggregation': '対象→店舗日平均→日平均→週平均',
        'window': '直前に完了した日曜日までの固定84暦日。全登録対象、店舗や期間の選別なし',
        'scope': '四條畷周辺・同じ実装ハッシュの事前固定予測のみ',
        'adoption': '基準達成は採用可・未適用。確率や着席許可は変更しない'}


def finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


@lru_cache(maxsize=1)
def implementation_hash():
    root = Path(__file__).resolve().parents[1]
    paths = ['hall/model_selection.py', 'hall/event_prediction.py', 'hall/machine_prediction.py',
             'hall/seat_prediction.py', 'hall/placement_analysis.py', 'hall/seat_history.py',
             'hall/prediction_quality.py', 'hall/prediction_benchmark.py', 'hall/target_validation.py',
             'hall/machine_scope.py', 'hall/names.py', 'api/routers/hall.py', 'hall/prediction_log.py']
    return digest({p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths})


def profiles(prediction):
    for family, field in [('event', 'event_studies'), ('machine', 'machine_studies'),
                          ('seat', 'seat_ranking_studies'), ('placement', 'placement_studies')]:
        for study in prediction.get(field, []):
            if family == 'seat':
                items = [p for m in study.get('machines', []) for p in m.get('profiles', [])]
            else:
                items = study.get('profiles', [])
                if family == 'placement':
                    items = items + study.get('retired_group_validations', [])
            for p in items:
                identity = (p.get('event_name') if family == 'event' else p.get('group_id') if family == 'placement'
                            else [p.get('machine_key') or normalize_machine_key(p.get('machine_name', '')),
                                  p.get('seat_number', 0), p.get('segment_id')])
                yield family, canonical_hall_name(study['hall_name']), identity, p


def research_summary(prediction):
    """Reuse completed study trials; do not rerun the heavy search or replay."""
    groups = defaultdict(dict)
    conflicts = Counter()
    for family, hall, identity, p in profiles(prediction):
        for t in p.get('validation', {}).get('trials', []):
            try:
                day = date.fromisoformat(t['date'])
                valid = date.fromisoformat(t['input_cutoff_date']) <= day - timedelta(days=2)
            except (KeyError, ValueError, TypeError):
                valid = False
            key = dumps([hall, identity, t.get('date')])
            value = {**t, 'hall_name': hall, 'subject_id': key}
            if not valid or not all(finite(t.get(k)) for k in ('predicted_coins', 'baseline_coins')):
                conflicts[family] += 1
                continue
            if key in groups[family] and groups[family][key] != value:
                groups[family][key] = None
            elif key not in groups[family]:
                groups[family][key] = value
    result = []
    for family, label in FAMILIES.items():
        trials = [t for t in groups[family].values() if t is not None]
        answered = [t for t in trials if finite(t.get('actual_coins'))]
        days = defaultdict(list)
        for t in answered:
            days[(t['date'], t['hall_name'])].append(t)
        # Equal hall-day weight, explicitly not a prospective adoption score.
        errors = {name: [mean(abs(t[field] - t['actual_coins']) for t in group) for group in days.values()]
                  for name, field in [('candidate', 'predicted_coins'), ('baseline', 'baseline_coins')]}
        result.append({'family': family, 'label': label, 'subjects': len(trials), 'resolved': len(answered),
                       'days': len({t['date'] for t in answered}),
                       'unresolved': len(trials) - len(answered),
                       'invalid': conflicts[family] + sum(v is None for v in groups[family].values()),
                       'candidate_mae': round(mean(errors['candidate']), 1) if answered else None,
                       'baseline_mae': round(mean(errors['baseline']), 1) if answered else None,
                       'status': '研究参考・採用の根拠にしない'})
    return {'items': result, 'notice': '既存の研究試行を再利用。現在方式の同時点予測がないため、その優劣は判定しません。'}


def freeze_selection(prediction):
    target = prediction['visit_date']
    lookup = {}
    for s in prediction.get('forecast_subjects', []):
        key = (s['scope'], canonical_hall_name(s['hall_name']), normalize_machine_key(s['machine_name']), s.get('seat_number', 0))
        lookup[key] = s['projected'] if key not in lookup else None
    samples = {}
    for family, hall, identity, p in profiles(prediction):
        candidate = p if family == 'event' else p.get('candidate') or {}
        if not finite(candidate.get('predicted_coins')):
            continue
        cutoff = candidate.get('input_cutoff_date', prediction['input_cutoff_date'])
        if date.fromisoformat(cutoff) > date.fromisoformat(target) - timedelta(days=2):
            raise ValueError('model review input cutoff violation')
        if family == 'placement':
            members = [{'scope': 'seat', 'hall_name': hall, 'machine_key': normalize_machine_key(m['machine_name']),
                        'seat_number': m['seat_number'], 'segment_id': m.get('segment_id')} for m in p['members']]
        elif family == 'event':
            # The current engine has no identical hall-event target. Never invent
            # a hall forecast by averaging a selected subset of machine forecasts.
            members = []
        else:
            members = [{'scope': 'seat' if family == 'seat' else 'machine', 'hall_name': hall,
                        'machine_key': normalize_machine_key(p.get('machine_name') or p['machine_key']), 'seat_number': p.get('seat_number', 0),
                        'segment_id': p.get('segment_id')}]
        current = [lookup.get((m['scope'], hall, m['machine_key'], m['seat_number'])) for m in members]
        key = digest([family, hall, identity])
        sample = {'id': key, 'family': family, 'hall_name': hall, 'identity': identity, 'members': members,
                  'target_date': target, 'input_cutoff_date': cutoff,
                  'models': {'candidate': candidate['predicted_coins'], 'baseline': candidate.get('baseline_coins'),
                             'current': mean(current) if current and all(finite(v) for v in current) else None}}
        if key in samples:
            raise ValueError('duplicate model review subject')
        samples[key] = sample
    return {'protocol': PROTOCOL, 'spec': dict(SPEC), 'implementation_hash': implementation_hash(),
            'samples': list(samples.values()), 'research': research_summary(prediction)}


def _metrics(samples):
    # All models have exactly the same answered cohort. Avoid thousands of
    # correlated seats on a single day masquerading as thousands of trials.
    hall_days = defaultdict(list)
    for s in samples:
        hall_days[(s['target_date'], s['hall_name'])].append(s)
    daily = defaultdict(list)
    for (day, _), group in hall_days.items():
        daily[day].append({m: mean(abs(s['models'][m] - s['actual']) for s in group)
                           for m in ('candidate', 'baseline', 'current')})
    daily = {d: {m: mean(g[m] for g in groups) for m in groups[0]} for d, groups in sorted(daily.items())}
    weeks = defaultdict(list)
    for d, losses in daily.items():
        day = date.fromisoformat(d)
        weeks[(day-timedelta(days=day.weekday())).isoformat()].append(losses)
    weekly = [{m: mean(d[m] for d in group) for m in group[0]} for _, group in sorted(weeks.items())]
    comparisons = []
    for baseline in ('baseline', 'current'):
        deltas = [w[baseline]-w['candidate'] for w in weekly]
        lower = None
        if len(deltas) >= SPEC['minimum_weeks']:
            rng = random.Random(SPEC['seed'])
            boot = sorted(mean(rng.choices(deltas, k=len(deltas))) for _ in range(SPEC['bootstrap_draws']))
            lower = boot[int(SPEC['lower_quantile']*(len(boot)-1))]
        cut = len(weekly)//2
        comparisons.append({'against': baseline, 'mae': mean(w[baseline] for w in weekly) if weekly else None,
                            'improvement': mean(deltas) if deltas else None, 'lower_bound': lower,
                            'first_half': mean(deltas[:cut]) if cut else None,
                            'second_half': mean(deltas[cut:]) if cut else None})
    return {'days': len(daily), 'weeks': len(weekly), 'from': min(daily) if daily else None,
            'to': max(daily) if daily else None,
            'candidate_mae': mean(w['candidate'] for w in weekly) if weekly else None,
            'comparisons': comparisons}


def assess(samples, *, family, evaluation_end=None):
    raw = [s for s in samples if s['family'] == family]
    comparable = [s for s in raw if all(finite(s['models'].get(m)) for m in ('candidate', 'baseline', 'current'))]
    answered = [s for s in comparable if finite(s.get('actual'))]
    metrics = _metrics(answered)
    coverage = len(answered)/len(raw) if raw else 0
    reasons = []
    if not raw:
        reasons.append('事前保存した新方式の予測がありません')
    if len(comparable) != len(raw):
        reasons.append('現行方式・基準方式を同じ対象で比較できない予測があります')
    if coverage < SPEC['minimum_coverage']:
        reasons.append('全保存対象の照合率が80%未満')
    if metrics['days'] < SPEC['minimum_days'] or metrics['weeks'] < SPEC['minimum_weeks']:
        reasons.append('事前成績が30日・8週に満たない')
    if evaluation_end and (not metrics['to'] or (evaluation_end-date.fromisoformat(metrics['to'])).days > SPEC['freshness_days']):
        reasons.append('評価期間の末尾14日以内に照合済み予測がない')
    decision = 'hold'
    if not reasons:
        if any(c['improvement'] <= 0 for c in metrics['comparisons']):
            decision = 'reject'
            reasons.append('同じ対象・期間で現行方式または基準方式を改善していない')
        else:
            for c in metrics['comparisons']:
                if c['improvement'] < max(SPEC['minimum_improvement_coins'], c['mae']*SPEC['minimum_improvement_ratio']):
                    reasons.append('誤差改善が50枚・5%の両方を満たさない')
                if c['lower_bound'] is None or c['lower_bound'] <= 0:
                    reasons.append('週単位の再標本化で改善幅の下限が0を超えない')
                if c['first_half'] <= 0 or c['second_half'] <= 0:
                    reasons.append('前半・後半の両期間で改善していない')
            if not reasons:
                decision = 'adoptable'
                reasons.append('比較基準を満たしました。確率校正・切替確認前のため実戦へは未適用')
    return {'family': family, 'label': FAMILIES[family], 'decision': decision, 'reasons': sorted(set(reasons)),
            'saved': len(raw), 'comparable': len(comparable), 'resolved': len(answered),
            'pending': len(comparable)-len(answered), 'coverage': coverage, 'metrics': metrics,
            'unresolved_reasons': dict(Counter(s.get('unresolved_reason', '結果待ち') for s in comparable if not finite(s.get('actual')))),
            'live_applied': False}


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _resolve_sample(sample, actuals, identities):
    values = []
    if not sample['members']:
        return None, '同一対象の現行方式がない（店舗イベント）'
    for m in sample['members']:
        key = (m['scope'], m['hall_name'], m['machine_key'], m['seat_number'])
        outcome = actuals.get(key)
        if not outcome:
            return None, '結果欠損・矛盾・機種不一致'
        row = outcome['row']
        games = row.get('games' if m['scope'] == 'seat' else 'avg_games')
        if not finite(games) or games < 1000:
            return None, '結果日の稼働が1000G未満または不明'
        if m['scope'] == 'seat':
            identity = identities.get((m['hall_name'], m['seat_number']))
            if not m.get('segment_id') or not identity or not identity['usable'] or identity['segment_id'] != m['segment_id']:
                return None, '入替・配置変更・観測期間不一致'
        values.append(outcome['actual'])
    return mean(values), None


def prospective_review(conn, *, today, code_hash=None):
    code_hash = code_hash or implementation_hash()
    last = today - timedelta(days=1)
    end = last - timedelta(days=(last.weekday()+1) % 7)
    start = end - timedelta(days=SPEC['window_days']-1)
    samples, evidence, exclusions = [], [], Counter()
    tables = _tables(conn) if conn is not None else set()
    if {'prediction_batch', 'prediction_subject', 'prediction_outcome'} <= tables:
        from hall.prediction_log import POLICY
        from hall.seat_history import build_seat_history, layout_observations
        batches = conn.execute('SELECT * FROM prediction_batch WHERE region=? AND policy=? AND target_date BETWEEN ? AND ? ORDER BY target_date,id',
                               ('shijonawate', POLICY, start.isoformat(), end.isoformat())).fetchall()
        for original in batches:
            b = dict(original)
            payload = json.loads(b['payload'])
            frozen = payload.get('model_selection')
            if not frozen or frozen.get('protocol') != PROTOCOL or frozen.get('spec') != SPEC or frozen.get('implementation_hash') != code_hash:
                exclusions['旧方式・異なる実装・基準未保存'] += 1
                continue
            if digest(payload) != b['payload_sha256'] or b['saved_at'][:10] >= b['target_date']:
                exclusions['固定記録の整合性または保存時点違反'] += 1
                continue
            actuals = {}
            outcomes = conn.execute('SELECT s.scope,s.hall_name,s.machine_key,s.seat_number,o.actual,o.evidence FROM prediction_subject s JOIN prediction_outcome o ON o.subject_id=s.id WHERE s.batch_id=? AND substr(o.resolved_at,1,10)<=? ORDER BY s.id', (b['id'], today.isoformat())).fetchall()
            for row in outcomes:
                r = dict(row)
                actuals[(r['scope'], r['hall_name'], r['machine_key'], r['seat_number'])] = {'actual': r['actual'], 'row': json.loads(r['evidence']).get('row', {})}
            needs_seats = any(m['scope'] == 'seat' for s in frozen['samples'] for m in s['members'])
            identities = {}
            if needs_seats and 'hall_day_seat' in tables:
                rows = payload.get('frozen_inputs', {}).get('seat_ranking_observations', [])
                later = [dict(r) for r in conn.execute('SELECT * FROM hall_day_seat WHERE report_date>? AND report_date<=?', (b['cutoff_date'], b['target_date']))]
                cutoff = date.fromisoformat(b['target_date'])
                maps = layout_observations(conn, cutoff)
                history = build_seat_history(rows+later, maps, cutoff=cutoff)
                identities = {(s['hall_name'], s['seat_number']): s for s in history['seats']}
                evidence.append({'batch_id': b['id'], 'identity_hash': digest(history['seats'])})
            for s in frozen['samples']:
                if s['target_date'] != b['target_date'] or date.fromisoformat(s['input_cutoff_date']) > date.fromisoformat(b['target_date'])-timedelta(days=2):
                    exclusions['対象日・入力期限違反'] += 1
                    continue
                actual, reason = _resolve_sample(s, actuals, identities)
                samples.append({**s, 'actual': actual, 'unresolved_reason': reason, 'batch_id': b['id']})
            evidence.append({'batch_id': b['id'], 'payload_sha256': b['payload_sha256'], 'outcomes_sha256': digest([dict(r) for r in outcomes])})
    items = [assess(samples, family=f, evaluation_end=end) for f in FAMILIES]
    if exclusions['固定記録の整合性または保存時点違反'] or exclusions['対象日・入力期限違反']:
        for item in items:
            item['decision'] = 'hold'
            item['reasons'].append('固定記録の整合性に問題があるため審査保留')
    return {'protocol': PROTOCOL, 'spec': SPEC, 'implementation_hash': code_hash,
            'window_start': start.isoformat(), 'window_end': end.isoformat(),
            'evidence_sha256': digest({'records': evidence, 'exclusions': dict(exclusions)}),
            'exclusions': dict(exclusions), 'items': items,
            'samples': samples, 'evidence': evidence, 'live_model': '現行方式を維持',
            'notice': '差枚予測の誤差の審査です。高設定的中率・本人勝率ではありません。研究試行は採用成績へ混ぜません。週の再標本化も独立性や将来利益を保証しません。'}


def initialize(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS prediction_model_review(
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, protocol TEXT NOT NULL,
            window_end TEXT NOT NULL, implementation_hash TEXT NOT NULL,
            evidence_sha256 TEXT NOT NULL, payload_sha256 TEXT NOT NULL, payload TEXT NOT NULL,
            UNIQUE(protocol,window_end,implementation_hash,evidence_sha256));
        CREATE TRIGGER IF NOT EXISTS model_review_no_update BEFORE UPDATE ON prediction_model_review
          BEGIN SELECT RAISE(ABORT,'model review is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS model_review_no_delete BEFORE DELETE ON prediction_model_review
          BEGIN SELECT RAISE(ABORT,'model review is immutable'); END;
    ''')


def store_review(conn, review, *, created_at):
    initialize(conn)
    payload = dumps(review)
    with conn:
        conn.execute('INSERT OR IGNORE INTO prediction_model_review(created_at,protocol,window_end,implementation_hash,evidence_sha256,payload_sha256,payload) VALUES(?,?,?,?,?,?,?)',
                     (created_at, PROTOCOL, review['window_end'], review['implementation_hash'], review['evidence_sha256'], digest(review), payload))
    return conn.execute('SELECT id FROM prediction_model_review WHERE protocol=? AND window_end=? AND implementation_hash=? AND evidence_sha256=?',
                        (PROTOCOL, review['window_end'], review['implementation_hash'], review['evidence_sha256'])).fetchone()[0]


def latest_review(conn):
    if conn is None or 'prediction_model_review' not in _tables(conn):
        return None
    row = conn.execute('SELECT * FROM prediction_model_review WHERE protocol=? ORDER BY id DESC LIMIT 1', (PROTOCOL,)).fetchone()
    if row is None:
        return None
    row = dict(row)
    report = json.loads(row['payload'])
    if digest(report) != row['payload_sha256']:
        raise ValueError('保存済み審査レポートの整合性を確認できません')
    return {**{k: v for k, v in report.items() if k not in {'samples', 'evidence'}}, 'id': row['id'],
            'created_at': row['created_at'], 'current_implementation': report['implementation_hash'] == implementation_hash()}
