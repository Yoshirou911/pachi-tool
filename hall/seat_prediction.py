"""v3.28 same-machine seat ranking challenger; never a seating permission.

Replay reconstructs each day's observable tenure and candidate set. Missing
outcomes never replace the selected seat with the next available winner.
"""
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean

from hall.machine_scope import is_smartslot_machine, normalize_machine_key
from hall.names import canonical_hall_name
from hall.prediction_quality import audit_rows
from hall.seat_history import build_seat_history

POLICY = 'same-machine-seat-peer-shrink-v1'
WINDOW_DAYS = 56
MIN_PAIRS = 10
MIN_EVALUATIONS = 20
MAX_EVALUATION_DAYS = 60


def study_days(rows, cutoff):
    days = set()
    for r in rows:
        try:
            day = date.fromisoformat(str(r.get('report_date', '')))
            if day <= cutoff and is_smartslot_machine(r.get('machine_name', '')):
                days.add(day)
        except (TypeError, ValueError):
            continue
    return sorted(days)[-MAX_EVALUATION_DAYS:]


def _rank(profiles, field):
    # Equal predictions share a rank. The number is only a stable display order.
    values = [p['candidate'][field] for p in profiles]
    return {p['seat_number']: 1 + sum(v > p['candidate'][field] for v in values) for p in profiles}


def _snapshot(raw, clean, layouts, target, cutoff):
    history = build_seat_history(raw, layouts, cutoff=cutoff)
    keys = {(r['seat_number'], r['report_date'], normalize_machine_key(r['machine_name'])) for r in history['rows']}
    usable = [r for r in clean if (r['seat_number'], r['report_date'], normalize_machine_key(r['machine_name'])) in keys
              and target-timedelta(days=WINDOW_DAYS) <= date.fromisoformat(r['report_date']) <= cutoff
              and r.get('games') is not None and r['games'] >= 1000]
    by_day = defaultdict(dict)
    by_seat = defaultdict(list)
    for r in usable:
        key = normalize_machine_key(r['machine_name'])
        by_day[(r['report_date'], key)][r['seat_number']] = r
        by_seat[r['seat_number']].append(r)
    profiles = []
    for seat in history['seats']:
        # A non-smartslot replacement still breaks identity but is not a target.
        if seat['status'] != '情報矛盾' and not is_smartslot_machine(seat['machine_name']):
            continue
        number = seat['seat_number']
        key = normalize_machine_key(seat['machine_name'])
        pairs = []
        for r in by_seat[number]:
            peers = [v for n, v in by_day[(r['report_date'], key)].items() if n != number]
            if len(peers) >= 2:
                pairs.append({'day':date.fromisoformat(r['report_date']), 'actual':r['diff_coins'],
                              'peer':mean(p['diff_coins'] for p in peers)})
        blockers = []
        if not seat['usable']:
            blockers.append(seat['status'])
        if len(pairs) < MIN_PAIRS:
            blockers.append('現在の観測期間・直近56日で他の同機種2台以上と比較できた日が10日未満')
        if not pairs or (cutoff-pairs[-1]['day']).days > 14:
            blockers.append('直近14日以内の比較実績なし')
        candidate = None
        if not blockers:
            weekday = [p for p in pairs if p['day'].weekday() == target.weekday()]
            controls = weekday if len(weekday) >= 3 else pairs
            peer_estimate = mean(p['peer'] for p in controls)
            adjustment = max(-500, min(500, mean(p['actual']-p['peer'] for p in pairs)*len(pairs)/(len(pairs)+20)))
            candidate = {'predicted_coins':round(peer_estimate+adjustment, 1),
                         'baseline_coins':round(mean(p['actual'] for p in pairs), 1),
                         'peer_estimate_coins':round(peer_estimate, 1), 'adjustment_coins':round(adjustment, 1),
                         'training_days':len(pairs), 'training_start':pairs[0]['day'].isoformat(),
                         'training_end':pairs[-1]['day'].isoformat(), 'input_cutoff_date':cutoff.isoformat(),
                         'control_basis':'同曜日' if weekday is controls else '同期間'}
        sources = sorted({u for r in usable if r['seat_number'] == number for u in r.get('source_urls', [])})
        profiles.append({'seat_number':number, 'machine_key':key, 'machine_name':seat['machine_name'],
                         'segment_id':seat['segment_id'], 'training_from':seat['training_from'],
                         'latest_date':seat['last_observed'], 'identity_status':seat['status'],
                         'paired_days':len(pairs), 'usable_days':len(by_seat[number]),
                         'candidate':candidate, 'blockers':blockers, 'source_urls':sources,
                         'rank':None, 'baseline_rank':None, 'model_influence_eligible':False})
    return profiles, history


def analyze_hall_seats(rows, *, hall_name, target, as_of, layouts_by_cutoff=None, installation=None):
    """layouts_by_cutoff contains independently known maps at EACH cutoff.

    Never reuse today's corrected map in an earlier replay. Missing snapshots
    mean no known layout, not an inferred adjacency from consecutive numbers.
    """
    hall_name = canonical_hall_name(hall_name)
    cutoff = min(as_of-timedelta(days=1), target-timedelta(days=2))
    raw = [dict(r) for r in rows if canonical_hall_name(dict(r).get('hall_name')) == hall_name]
    clean, audit = audit_rows(raw, cutoff=cutoff, scope='seat')
    layouts_by_cutoff = layouts_by_cutoff or {}

    def maps(day):
        return [r for r in layouts_by_cutoff.get(day.isoformat(), [])
                if canonical_hall_name(r.get('hall_name')) == hall_name]

    profiles, current_history = _snapshot(raw, clean, maps(cutoff), target, cutoff)
    current_installation = '現在の設置一覧は未確認'
    if installation:
        try:
            age = (cutoff-date.fromisoformat(installation['snapshot_date'])).days
            if 0 <= age <= 21:
                installed = {normalize_machine_key(n) for n in installation['machines']}
                current_installation = '機種の設置一覧と照合済み（番号の現存保証ではありません）'
                for p in profiles:
                    if p['machine_key'] not in installed:
                        p['candidate'] = None
                        p['blockers'].append('期限内の設置機種一覧に記録なし')
        except (ValueError, TypeError, KeyError):
            pass

    # Rebuild past candidate sets before looking up any outcome. Do not restrict
    # history to survivors in today's machine list or today's seat tenure.
    seat_trials = defaultdict(list)
    rank_trials = defaultdict(list)
    actuals = {(r['report_date'], r['seat_number'], normalize_machine_key(r['machine_name'])):r for r in clean}
    days = study_days(raw, cutoff)
    for day in days:
        past_cutoff = day-timedelta(days=2)
        past, _ = _snapshot(raw, clean, maps(past_cutoff), day, past_cutoff)
        outcome_history = build_seat_history(raw, maps(day), cutoff=day)
        actual_identity = {s['seat_number']:s for s in outcome_history['seats']}
        groups = defaultdict(list)
        for p in past:
            if p['candidate'] is None:
                continue
            key = p['machine_key']
            r = actuals.get((day.isoformat(), p['seat_number'], key))
            identity = actual_identity.get(p['seat_number'])
            reason = None
            if not identity or identity['segment_id'] != p['segment_id'] or not identity['usable']:
                reason = '入替・配置変更・観測期間の不一致'
            elif r is None:
                reason = '実績欠損・矛盾'
            elif r.get('games') is None or r['games'] < 1000:
                reason = '稼働不明・1000G未満'
            trial = {**p['candidate'], 'date':day.isoformat(), 'seat_number':p['seat_number'],
                     'segment_id':p['segment_id'], 'actual_coins':None if reason else r['diff_coins'],
                     'unresolved_reason':reason}
            seat_trials[(key, p['seat_number'], p['segment_id'])].append(trial)
            groups[key].append({**p, 'trial':trial})
        for key, group in groups.items():
            if len(group) < 2:
                continue
            ranks = _rank(group, 'predicted_coins')
            baselines = _rank(group, 'baseline_coins')
            selected = [p for p in group if ranks[p['seat_number']] == 1]
            base_selected = [p for p in group if baselines[p['seat_number']] == 1]
            complete = all(p['trial']['actual_coins'] is not None for p in group)
            actual = mean(p['trial']['actual_coins'] for p in selected) if complete else None
            base_actual = mean(p['trial']['actual_coins'] for p in base_selected) if complete else None
            best = max(p['trial']['actual_coins'] for p in group) if complete else None
            rank_trials[key].append({'date':day.isoformat(), 'input_cutoff_date':past_cutoff.isoformat(),
                                    'cohort_size':len(group),
                                    'selected_seats':[p['seat_number'] for p in selected],
                                    'baseline_selected_seats':[p['seat_number'] for p in base_selected],
                                    'resolved':complete, 'actual_coins':actual, 'baseline_actual_coins':base_actual,
                                    'regret_coins':best-actual if complete else None,
                                    'baseline_regret_coins':best-base_actual if complete else None,
                                    'unresolved_seats':[p['seat_number'] for p in group if p['trial']['actual_coins'] is None],
                                    'predictions':[p['trial'] for p in group]})

    machines = defaultdict(list)
    for p in profiles:
        trials = seat_trials[(p['machine_key'], p['seat_number'], p['segment_id'])]
        answered = [t for t in trials if t['actual_coins'] is not None]
        p['validation'] = {'days':len(answered), 'unresolved_days':len(trials)-len(answered),
                           'candidate_mae_coins':round(mean(abs(t['predicted_coins']-t['actual_coins']) for t in answered),1) if answered else None,
                           'baseline_mae_coins':round(mean(abs(t['baseline_coins']-t['actual_coins']) for t in answered),1) if answered else None,
                           'trials':trials}
        p['validation_status'] = '比較20日以上・未採用' if len(answered) >= MIN_EVALUATIONS else '検証不足'
        machines[p['machine_key']].append(p)
    results = []
    # Retired machines remain in retrospective reporting, avoiding survivor bias.
    for key in sorted(set(machines) | set(rank_trials)):
        group = machines[key]
        ranked = [p for p in group if p['candidate'] is not None]
        if len(ranked) >= 2:
            ranks = _rank(ranked, 'predicted_coins')
            baselines = _rank(ranked, 'baseline_coins')
            for p in ranked:
                p.update(rank=ranks[p['seat_number']], baseline_rank=baselines[p['seat_number']])
        elif ranked:
            ranked[0]['blockers'].append('現在比較できる台が1台のみのため順位なし')
        trials = rank_trials[key]
        answered = [t for t in trials if t['resolved']]
        results.append({'machine_key':key, 'machine_name':group[0]['machine_name'] if group else key,
                        'profiles':sorted(group, key=lambda p:(p['rank'] is None, p['rank'] or 0, p['seat_number'])),
                        'ranked_seats':sum(p['rank'] is not None for p in group),
                        'validation':{'days':len(answered), 'attempted_days':len(trials),
                                      'unresolved_days':len(trials)-len(answered),
                                      'candidate_regret_coins':round(mean(t['regret_coins'] for t in answered),1) if answered else None,
                                      'baseline_regret_coins':round(mean(t['baseline_regret_coins'] for t in answered),1) if answered else None,
                                      'status':'比較20日以上・未採用' if len(answered)>=MIN_EVALUATIONS else '検証不足',
                                      'trials':trials}})
    return {'hall_name':hall_name, 'target_date':target.isoformat(), 'input_cutoff_date':cutoff.isoformat(),
            'policy':POLICY, 'machines':results, 'input_audit':audit,
            'observed_seats':len(profiles), 'ranked_seats':sum(m['ranked_seats'] for m in results),
            'identity_excluded_rows':current_history['excluded_records'],
            'activity_excluded_rows':sum(r.get('games') is None or r['games']<1000 for r in clean),
            'installation_status':current_installation, 'evaluation_calendar_days':len(days),
            'model_influence_eligible':False,
            'notice':'取得済みの同店・同機種内だけの研究順位。対象日の2日前まで、現在の観測期間・前56日で比較10日以上が必要です。同機種の他2台以上との差を縮小し、±500枚に制限。同点は同順位。直近最大60実績日を当時の対象集合で検証し、全比較台の結果が揃わない日は順位成績へ混ぜません。公開台の偏り・結果公開時刻・改訂履歴の未整備が残ります。角・隣接・設定・本人収支は断定しません。採用審査は3.30で、着席推奨には反映しません。'}
