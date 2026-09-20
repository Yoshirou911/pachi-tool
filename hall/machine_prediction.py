"""v3.27 leave-one-machine-out hall context challenger (research only).

One sample is one published day, not a machine count. No target-day peer outcome
is ever used to predict that day. This is not a setting or personal-win model.
"""
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean

from hall.machine_scope import is_smartslot_machine, normalize_machine_key
from hall.names import canonical_hall_name
from hall.prediction_quality import audit_rows

POLICY = 'hall-machine-peer-shrink-v1'
WINDOW_DAYS = 56
MIN_PAIRS = 10
MIN_EVALUATIONS = 20


def _predict(pairs, target, cutoff):
    cutoff = min(cutoff, target-timedelta(days=2))
    training = [p for p in pairs if target-timedelta(days=WINDOW_DAYS) <= p['day'] <= cutoff]
    if len(training) < MIN_PAIRS:
        return None
    if (cutoff-training[-1]['day']).days > 14:
        return None
    weekday = [p for p in training if p['day'].weekday() == target.weekday()]
    controls = weekday if len(weekday) >= 3 else training
    # Estimated store condition comes solely from past peer-machine results.
    peer_estimate = mean(p['peers'] for p in controls)
    residual = mean(p['actual']-p['peers'] for p in training)
    adjustment = max(-500, min(500, residual*len(training)/(len(training)+20)))
    return {'predicted_coins':round(peer_estimate+adjustment, 1),
            'baseline_coins':round(mean(p['actual'] for p in training), 1),
            'peer_estimate_coins':round(peer_estimate, 1), 'adjustment_coins':round(adjustment, 1),
            'training_days':len(training), 'control_days':len(controls),
            'control_basis':'同曜日' if controls is weekday else '同期間',
            'input_cutoff_date':cutoff.isoformat(),
            'training_start':training[0]['day'].isoformat(), 'training_end':training[-1]['day'].isoformat()}


def analyze_hall_machines(rows, *, hall_name, target, as_of, installation=None):
    hall_name = canonical_hall_name(hall_name)
    cutoff = min(as_of-timedelta(days=1), target-timedelta(days=2))
    scoped = [dict(r) for r in rows if canonical_hall_name(r.get('hall_name')) == hall_name
              and is_smartslot_machine(r.get('machine_name', ''))]
    clean, audit = audit_rows(scoped, cutoff=cutoff, scope='machine')
    # Unknown / low play-count rows are visible in coverage, not treated as
    # equally informative observations of a store's treatment of a machine.
    usable = [r for r in clean if r.get('avg_games') is not None and r['avg_games'] >= 1000]
    by_day = defaultdict(dict)
    by_machine = defaultdict(list)
    for r in usable:
        key = normalize_machine_key(r['machine_name'])
        by_day[date.fromisoformat(r['report_date'])][key] = r
        by_machine[key].append(r)
    observed = defaultdict(list)
    for r in clean:
        observed[normalize_machine_key(r['machine_name'])].append(r)
    profiles = []
    for key, observations in sorted(observed.items()):
        name = observations[-1]['machine_name']
        pairs = []
        for row in by_machine[key]:
            day = date.fromisoformat(row['report_date'])
            peers = [p for k, p in by_day[day].items() if k != key]
            if len(peers) < 2:
                continue
            units = sum(p['unit_count'] for p in peers)
            peer_avg = sum(p['avg_diff_coins']*p['unit_count'] for p in peers)/units
            pairs.append({'day':day, 'actual':row['avg_diff_coins'], 'peers':peer_avg, 'peer_machines':len(peers)})
        candidate = _predict(pairs, target, cutoff)
        trials = []
        # Evaluate ALL observed target-machine outcomes, not just days when
        # peer outcomes happen to be published on the evaluation day.
        outcomes = by_machine[key][-60:]
        for row in outcomes:
            day = date.fromisoformat(row['report_date'])
            trial = _predict(pairs, day, cutoff)
            if trial is not None:
                trials.append({**trial, 'date':day.isoformat(), 'actual_coins':row['avg_diff_coins']})
        mae = mean(abs(t['predicted_coins']-t['actual_coins']) for t in trials) if trials else None
        baseline_mae = mean(abs(t['baseline_coins']-t['actual_coins']) for t in trials) if trials else None
        enough = len(trials) >= MIN_EVALUATIONS
        status = ('比較上は改善・未採用' if mae < baseline_mae else '比較上の改善なし') if enough else '検証不足'
        installation_status = '現在の設置は未確認'
        not_installed = False
        if installation:
            try:
                age = (cutoff-date.fromisoformat(installation['snapshot_date'])).days
                if 0 <= age <= 21:
                    not_installed = key not in {normalize_machine_key(n) for n in installation['machines']}
                    installation_status = '設置一覧に記録なし' if not_installed else '設置一覧と照合済み'
            except (KeyError, TypeError, ValueError):
                pass
        blockers = []
        recent_pairs = [p for p in pairs if target-timedelta(days=WINDOW_DAYS) <= p['day'] <= cutoff]
        if len(recent_pairs) < MIN_PAIRS:
            blockers.append('直近56日で他機種2種類以上と比較できた日が10日未満')
        if not pairs or (cutoff-pairs[-1]['day']).days > 14:
            blockers.append('直近14日以内の比較実績なし')
        if not enough:
            blockers.append('同じ日で方式を比較できた日が20日未満')
        if not_installed:
            blockers.append('期限内の設置一覧にないため現在候補は表示しない')
            candidate = None
        profiles.append({'machine_key':key, 'machine_name':name,
                         'observed_days':len(observations), 'usable_days':len(by_machine[key]),
                         'paired_days':len(pairs), 'recent_paired_days':len(recent_pairs),
                         'relative_diff_coins':round(mean(p['actual']-p['peers'] for p in pairs),1) if pairs else None,
                         'latest_date':observations[-1]['report_date'], 'installation_status':installation_status,
                         'candidate':candidate, 'status':status, 'blockers':blockers,
                         'validation':{'days':len(trials), 'eligible_outcome_days':len(outcomes),
                                       'unevaluated_days':len(outcomes)-len(trials),
                                       'candidate_mae_coins':round(mae,1) if trials else None,
                                       'baseline_mae_coins':round(baseline_mae,1) if trials else None,
                                       'trials':trials},
                         'source_urls':sorted({u for r in observations for u in r.get('source_urls', [])}),
                         'model_influence_eligible':False})
    return {'hall_name':hall_name, 'target_date':target.isoformat(), 'input_cutoff_date':cutoff.isoformat(),
            'policy':POLICY, 'profiles':profiles, 'input_audit':audit,
            'activity_excluded_rows':len(clean)-len(usable), 'model_influence_eligible':False,
            'notice':'全スマスロに同じ基準を適用。対象機種を除いた同日・同店の他機種2種類以上（台数加重）と比較します。差枚・台数が確認でき、平均1000G以上の行だけを使用。未収集機種は判定不能。対象日の2日前までの履歴で予測し、直近最大60実績日を順に比較。新方式の本番採用は3.30で審査。公開機種・稼働の偏りや結果の公開時刻・改訂履歴は未解決の研究用比較です。高設定・本人勝率・実物の同一台を示すものではありません。'}
