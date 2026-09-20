"""Fixed top-K diagnostics, separate prospective rules from post-hoc research.

Ranks are frozen before outcomes. Missing outcomes never cause substitution.
Neither the ranking nor this evaluation is a live seating recommendation.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from statistics import mean

from hall.prediction_benchmark import MODELS, subject_key
from hall.probability_validation import finite, read_samples, stamp
from hall.prediction_log import JST, POLICY

PROTOCOL = 'fixed-top-k-v1'
SPEC = {'protocol': PROTOCOL, 'top_k': [1, 3, 5], 'window_days': 84,
        'scope': ['machine', 'seat'], 'pools': ['all', 'eligible'],
        'cohort': '4方式すべて算出済みの同一対象。実戦候補だけでなく参考・見送りも保存',
        'ordering': '予測差枚降順→確率降順→店舗・機種・台番号昇順。結果による再選択なし',
        'eligible': '保存時の現行判定を通過した対象。同じ絞り込みを4方式へ適用',
        'label': '公開実績の差枚が0枚超。±0は非プラス。高設定的中率・本人勝率ではない',
        'minimum_days': 30, 'minimum_weeks': 8, 'minimum_resolution': .8,
        'freshness_days': 14, 'live_applied': False}


@lru_cache(maxsize=1)
def implementation_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def freeze_candidates(prediction):
    comparison = prediction['comparison']
    subjects, seen = [], set()
    for item in comparison['samples']:
        key = subject_key(item)
        if key in seen or key[0] not in SPEC['scope'] or item['target_date'] != prediction['visit_date']:
            raise ValueError('duplicate or invalid ranking subject')
        seen.add(key)
        valid = (item.get('comparable') is True and set(item['models']) == set(MODELS)
                 and all(finite(v.get('projected')) and finite(v.get('probability')) and 0 <= v['probability'] <= 1
                         for v in item['models'].values()))
        subjects.append((key, item, valid))
    groups = []
    for scope in SPEC['scope']:
        scoped = [s for s in subjects if s[0][0] == scope]
        for model in MODELS:
            rows = [{'key': list(key), 'projected': item['models'][model]['projected'],
                     'probability': item['models'][model]['probability'],
                     'eligible': bool(item.get('live_recommended'))} for key,item,valid in scoped if valid]
            rows.sort(key=lambda r: (-r['projected'], -r['probability'], tuple(r['key'])))
            groups.append({'scope':scope, 'model':model, 'total':len(scoped),
                           'excluded_missing_models':sum(not valid for _,_,valid in scoped), 'ranking':rows})
    return {'protocol':PROTOCOL, 'spec':SPEC.copy(), 'implementation_hash':implementation_hash(),
            'target_date':prediction['visit_date'], 'input_cutoff_date':prediction['input_cutoff_date'],
            'groups':groups, 'live_applied':False}


def select_case(group, actuals, *, target, pool):
    ranking = [r for r in group['ranking'] if pool == 'all' or r['eligible']]
    rows = [{**r, 'actual':actuals.get(tuple(r['key']), {}).get('actual'),
             'unresolved_reason':actuals.get(tuple(r['key']), {}).get('unresolved_reason', '結果待ち')} for r in ranking]
    # All K use this exact ranked list, not a list filtered on available results.
    return {'date':target, 'rows':rows, 'total':group['total'],
            'excluded_missing_models':group['excluded_missing_models'],
            'withheld':len(group['ranking'])-sum(r['eligible'] for r in group['ranking'])}


def aggregate(cases, *, k, end):
    selected = [(c, c['rows'][:k]) for c in cases]
    available = [(c, rows) for c,rows in selected if rows]
    completed = [(c, rows) for c,rows in available if all(r['actual'] is not None for r in rows)]
    # Shared comparison days require the entire pool's outcomes. Thus K=1/3/5
    # and the unranked baseline use identical days and a fully observed pool.
    common = [(c,rows) for c,rows in available if all(r['actual'] is not None for r in c['rows'])]
    count = sum(len(rows) for _,rows in available)
    answered = sum(r['actual'] is not None for _,rows in available for r in rows)
    dates = sorted(c['date'] for c,_ in completed)
    weeks = {date.fromisoformat(d).isocalendar()[:2] for d in dates}
    avg = lambda pairs, fn: mean([mean([fn(r) for r in rows]) for _,rows in pairs]) if pairs else None
    positive = lambda r: 100*int(r['actual'] > 0)
    reasons = []
    if len(dates) < SPEC['minimum_days'] or len(weeks) < SPEC['minimum_weeks']:
        reasons.append('候補全件を照合できた日が30日・8週に不足')
    if not count or answered/count < SPEC['minimum_resolution']:
        reasons.append('選択した候補の照合率が80%未満')
    if not dates or (end-date.fromisoformat(dates[-1])).days > SPEC['freshness_days']:
        reasons.append('直近14日内に候補全件を照合した日がない')
    ties = sum(len(c['rows']) > k and (c['rows'][k-1]['projected'],c['rows'][k-1]['probability']) ==
               (c['rows'][k]['projected'],c['rows'][k]['probability']) for c in cases)
    return {'k':k, 'saved_days':len(cases), 'candidate_days':len(available), 'no_candidate_days':len(cases)-len(available),
            'shortfall_days':sum(len(rows)<k for _,rows in available), 'tie_boundary_days':ties,
            'selected':count, 'answered':answered, 'pending':count-answered, 'complete_days':len(completed),
            'incomplete_days':len(available)-len(completed), 'weeks':len(weeks),
            'positive_pct':avg(completed,positive), 'avg_diff_coins':avg(completed,lambda r:r['actual']),
            'common_days':len(common), 'common_positive_pct':avg(common,positive),
            'common_avg_diff_coins':avg(common,lambda r:r['actual']),
            'pool_positive_pct':avg([(c,c['rows']) for c,_ in common],positive),
            'pool_avg_diff_coins':avg([(c,c['rows']) for c,_ in common],lambda r:r['actual']),
            'reasons':reasons, 'status':'hold' if reasons else 'descriptive_only',
            'unresolved_reasons':dict(Counter(r['unresolved_reason'] for _,rows in available for r in rows if r['actual'] is None))}


def candidate_report(conn, *, as_of=None):
    as_of = stamp((as_of or datetime.now(JST)).isoformat())
    end = as_of.date()-timedelta(days=1)
    start = end-timedelta(days=SPEC['window_days']-1)
    samples, exclusions = read_samples(conn,since=start,as_of=as_of)
    by_batch = defaultdict(list)
    for s in samples:
        by_batch[s['batch_id']].append(s)
    groups, recorded_dates = defaultdict(list), set()
    for batch_id, rows in by_batch.items():
        b = conn.execute('SELECT payload FROM prediction_batch WHERE id=?',(batch_id,)).fetchone()
        payload = json.loads(b['payload'])
        try:
            expected = freeze_candidates(payload)
        except (KeyError, TypeError, ValueError):
            exclusions['保存順位の整合性違反'] += 1
            continue
        frozen = payload.get('candidate_evaluation')
        mode = 'research'
        if frozen is not None:
            if not isinstance(frozen, dict) or frozen.get('protocol') != PROTOCOL or frozen.get('spec') != SPEC or frozen.get('implementation_hash') != implementation_hash():
                exclusions['異なる順位ルール・実装'] += 1
                continue
            if frozen != expected:
                exclusions['保存順位の整合性違反'] += 1
                continue
            mode = 'prospective'
        else:
            # Post-hoc rule choice over genuine old forecasts, not prospective K evidence.
            frozen = expected
        head = rows[0]
        recorded_dates.add(head['target_date'])
        for group in frozen['groups']:
            actuals = {tuple(r['key']):r for r in rows if r['scope'] == group['scope'] and r['model'] == group['model']}
            for pool in SPEC['pools']:
                key = (mode,head['version'],head['source_hash'],group['scope'],group['model'],pool)
                case = select_case(group,actuals,target=head['target_date'],pool=pool)
                groups[key].append(case)
    items = []
    for (mode,version,source,scope,model,pool), cases in sorted(groups.items()):
        metrics = [aggregate(cases,k=k,end=end) for k in SPEC['top_k']]
        if exclusions['記録の整合性・保存時点違反'] or exclusions['保存順位の整合性違反']:
            for metric in metrics:
                metric['status'] = 'hold'; metric['reasons'].append('保存記録に整合性の問題があるため保留')
        items.append({'mode':mode, 'version':version, 'source_hash':source, 'scope':scope, 'model':model, 'pool':pool,
                      'label':MODELS[model], 'metrics':metrics,
                      'excluded_missing_models':sum(c['excluded_missing_models'] for c in cases),
                      'withheld':sum(c['withheld'] for c in cases),
                      'recent':[{'date':c['date'], 'rows':c['rows'][:5]} for c in cases[-3:]]})
    return {'protocol':PROTOCOL, 'spec':SPEC, 'as_of':as_of.isoformat(), 'window_start':start.isoformat(), 'window_end':end.isoformat(),
            'recorded_days':len(recorded_dates), 'unassessed_calendar_days':SPEC['window_days']-len(recorded_dates),
            'items':items, 'exclusions':dict(exclusions), 'live_applied':False,
            'notice':'プラス率は選択候補の公開差枚が0枚超だった割合を日別に平均した値。高設定的中率・本人勝率・収益予測ではありません。',
            'missing_notice':'一部でも結果が不明な日は日別成績へ混ぜず、候補の差替えもしません。未保存日・条件不一致日は見送り成功として数えません。',
            'scope_notice':'4方式が揃った機種平均／台番号の比較用順位。既存画面や3.28の研究順位とは別で、実戦順位・着席判定は変更しません。'}
