export const JUDGMENT_LABELS = {
  target: '打つ候補',
  wait: '見送り',
  verify: '要確認',
  unknown: '判定不能',
  insufficient_funds: '資金不足',
  condition_mismatch: '条件不一致',
  closing_risk: '閉店リスク',
};

export function money(value, signed = false) {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  const numeric = Number(value);
  const sign = signed && numeric > 0 ? '+' : '';
  return `${sign}${numeric.toLocaleString('ja-JP')}円`;
}

export function calculateSummary(state) {
  const results = Array.isArray(state.results) ? state.results : [];
  const investment = results.reduce((sum, row) => sum + Number(row.investment_yen || 0), 0);
  const returns = results.reduce((sum, row) => sum + Number(row.returns_yen || 0), 0);
  const net = returns - investment;
  const starting = Number(state.budget?.starting_bankroll || 0);
  const lossLimit = Number(state.budget?.loss_limit_yen || 0);
  const currentBankroll = Math.max(0, starting + net);
  const remainingLoss = Math.max(0, lossLimit - Math.max(0, -net));
  return {
    configured: starting > 0 && lossLimit > 0,
    starting_bankroll: starting,
    loss_limit_yen: lossLimit,
    investment_yen: investment,
    returns_yen: returns,
    net_profit_yen: net,
    current_bankroll: currentBankroll,
    remaining_loss_yen: remainingLoss,
    risk_capacity_yen: Math.min(currentBankroll, remainingLoss),
    plays: results.length,
  };
}

export function buildPerformanceSeries(results) {
  const tracked = (Array.isArray(results) ? [...results].reverse() : [])
    .filter(row => row.expected_value_yen !== null && row.expected_value_yen !== undefined && Number.isFinite(Number(row.expected_value_yen)));
  let cumulativeExpected = 0;
  let cumulativeActual = 0;
  const points = tracked.map((row, index) => {
    const expected = Number(row.expected_value_yen);
    const actual = Number(row.returns_yen || 0) - Number(row.investment_yen || 0);
    cumulativeExpected += expected;
    cumulativeActual += actual;
    return {
      index: index + 1,
      played_on: row.played_on || '',
      machine_name: row.machine_name || '',
      expected_yen: expected,
      actual_yen: actual,
      cumulative_expected_yen: cumulativeExpected,
      cumulative_actual_yen: cumulativeActual,
      gap_yen: cumulativeActual - cumulativeExpected,
    };
  });
  return {
    points,
    tracked_count: points.length,
    total_expected_yen: cumulativeExpected,
    total_actual_yen: cumulativeActual,
    gap_yen: cumulativeActual - cumulativeExpected,
  };
}

export function buildValidationSummary(results = []) {
  const groups = new Map();
  for (const result of results) {
    if (result.expected_value_yen == null || !Number.isFinite(Number(result.expected_value_yen))) continue;
    const key = result.catalog_key || result.machine_name || 'unknown';
    if (!groups.has(key)) groups.set(key, {
      catalog_key: result.catalog_key || '', machine_name: result.machine_name || '機種不明',
      count: 0, expected_yen: 0, actual_yen: 0, minutes: 0,
    });
    const group = groups.get(key);
    group.count += 1;
    group.expected_yen += Number(result.expected_value_yen);
    group.actual_yen += Number(result.returns_yen || 0) - Number(result.investment_yen || 0);
    group.minutes += Number(result.played_minutes || 0);
  }
  return [...groups.values()].map(group => ({
    ...group,
    gap_yen: group.actual_yen - group.expected_yen,
    avg_actual_yen: Math.round(group.actual_yen / group.count),
    avg_minutes: group.minutes ? Math.round(group.minutes / group.count) : null,
    sample_level: group.count >= 30 ? 'usable' : group.count >= 10 ? 'watch' : 'insufficient',
    sample_label: group.count >= 30 ? '検証可能' : group.count >= 10 ? '要観察' : 'データ不足',
  })).sort((a, b) => b.count - a.count || b.expected_yen - a.expected_yen);
}

export function curvePointAt(profile, currentValue) {
  const points = Array.isArray(profile?.curve_points)
    ? [...profile.curve_points].sort((a, b) => Number(a.value) - Number(b.value))
    : [];
  return [...points].reverse().find(point => Number(point.value) <= Number(currentValue)) || null;
}

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function finiteNumber(value, fallback = null) {
  if (value === '' || value === null || value === undefined) return fallback;
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

const DURATION_MODELS = {
  'hokuto-normal-equivalent-conservative-v1': [310, 13.5, 5, 39, 3, 0, 28],
  'hokuto-normal-56-current-conservative-v1': [255, 13.5, 5, 35, 3, 0, 28],
  'hokuto-reset-equivalent-conservative-v1': [285, 13.5, 5, 42, 4, 0, 30],
  'monkey5-normal-equivalent-conservative-v1': [285, 13.2, 7, 42, 7, 1, 35],
  'monkey5-normal-56-cash-conservative-v1': [235, 13.2, 7, 39, 6, 1, 32],
  'tokyo-ghoul-cz-equivalent-safe-v1': [160, 13.2, 8, 27, 7, 1, 30],
  'tokyo-ghoul-at-56-cash-safe-v1': [340, 13.2, 8, 47, 8, 1, 40],
  'god-eater-resurrection-normal-equivalent-v1': [278, 13.3, 6, 18, 4, 1, 28],
  'god-eater-resurrection-reset-equivalent-v1': [207, 13.3, 6, 19, 4, 1, 26],
  'kaguya-sama-big-after-equivalent-v1': [305, 13.2, 6, 37, 7, 1, 35],
  'kaguya-sama-reg-after-equivalent-v1': [365, 13.2, 6, 40, 8, 1, 38],
  'monster-hunter-rise-normal-equivalent-v1': [350, 13, 10, 53, 9, 1, 45],
  'karakuri-circus-cz-equivalent-v1': [300, 13.2, 9, 27, 5, 2, 38],
  'karakuri-circus-cz-56-cash-v1': [235, 13.2, 9, 26, 5, 2, 35],
  'tokyo-revengers-normal-equivalent-v1': [330, 13, 8, 48, 7, 1, 40],
  'tokyo-revengers-reset-equivalent-v1': [225, 13, 8, 46, 7, 1, 38],
  'bakemonogatari-normal-equivalent-v1': [260, 13, 7, 41, 6, 1, 35],
  'bakemonogatari-reset-equivalent-v1': [170, 13, 7, 38, 6, 1, 32],
  'million-god-kiseki-normal-equivalent-v1': [520, 13, 8, 52, 8, 2, 50],
  'million-god-kiseki-normal-56-cash-v1': [390, 13, 8, 52, 8, 2, 48],
  'valvrave1-bonus-gap-equivalent-v1': [424, 13, 9, 42, 6, 2, 45],
  'valvrave1-pullback-66g-v1': [33, 13, 0, 7, 0, 0, 12],
  'saint-seiya-kaiou-normal-equivalent-v1': [282, 13, 9, 47, 8, 1, 45],
  'toloveru-normal-equivalent-v1': [310, 13.2, 7, 38, 8, 1, 38],
  'toloveru-reset-equivalent-v1': [255, 13.2, 7, 35, 8, 1, 35],
};

export function estimateDuration(profile, currentValue = null) {
  const objectModel = profile?.duration_model;
  const packed = DURATION_MODELS[profile?.catalog_key];
  const model = objectModel && Object.keys(objectModel).length ? objectModel : (packed ? {
    normal_games: packed[0], normal_games_per_minute: packed[1], cz_minutes: packed[2],
    at_bonus_minutes: packed[3], pullback_minutes: packed[4], variable_speed_minutes: packed[5],
    safe_buffer_minutes: packed[6],
  } : null);
  if (!model) {
    const fixed = finiteNumber(profile?.estimated_play_minutes);
    return { estimated_play_minutes: fixed, safe_play_minutes: fixed ? fixed + 30 : 120, confidence: fixed ? 'legacy' : 'unknown', breakdown: {} };
  }
  const start = finiteNumber(profile?.start_threshold, 0);
  const extra = Math.max(0, finiteNumber(currentValue, start) - start);
  const baseNormal = Math.max(0, finiteNumber(model.normal_games, 0));
  const remainingNormal = Math.max(baseNormal * .25, baseNormal - extra * finiteNumber(model.remaining_reduction_per_input, .55));
  const breakdown = {
    normal: Math.ceil(remainingNormal / Math.max(1, finiteNumber(model.normal_games_per_minute, 13.2))),
    cz_forecast: Math.max(0, finiteNumber(model.cz_minutes, 0)),
    at_bonus: Math.max(0, finiteNumber(model.at_bonus_minutes, 0)),
    pullback: Math.max(0, finiteNumber(model.pullback_minutes, 0)),
    variable_speed: Math.max(0, finiteNumber(model.variable_speed_minutes, 0)),
  };
  const mean = Math.max(1, Math.ceil(Object.values(breakdown).reduce((sum, value) => sum + value, 0)));
  const buffer = Math.max(0, finiteNumber(model.safe_buffer_minutes, 30));
  return { estimated_play_minutes: mean, safe_play_minutes: mean + buffer, safe_buffer_minutes: buffer, confidence: model.confidence || 'modeled', breakdown };
}

export function calculateMachineAdjustment(profile, context = {}) {
  const name = String(profile?.machine_name || '');
  const key = String(profile?.catalog_key || '');
  const start = finiteNumber(profile?.start_threshold, 0);
  if (name.includes('からくりサーカス')) {
    if (finiteNumber(context.cz_misses, 0) >= 4) return { active: true, verified: true, adjusted_start_threshold: 0, reason: 'CZ4スルー後は次回CZがAT直撃へ書き換わる公開条件です', rule: 'karakuri_cz_4_misses' };
    if (finiteNumber(context.at_gap, 0) >= 2500) return { active: true, verified: true, adjusted_start_threshold: 0, reason: 'AT間2500G到達はAT＋成功確定の激情ジャッジ条件です', rule: 'karakuri_at_gap_2500' };
    if (context.fate_progress === 'ready') return { active: true, verified: true, adjusted_start_threshold: start, reason: '運命の一劇権利あり。通常ボーダーは浅くせず状態を保持します', rule: 'karakuri_fate_ready' };
  }
  if ((name.includes('ゴッドイーター') || name.includes('かぐや様')) && context.section_reset_confirmed) {
    return { active: true, verified: true, adjusted_start_threshold: start, reason: '有利区間リセット確認済み。未公開の差枚ライン推測では浅くしません', rule: 'cut_confirmed' };
  }
  if (name.includes('ヴァルヴレイヴ')) {
    if (key === 'valvrave1-pullback-66g-v1') return { active: true, verified: true, adjusted_start_threshold: 0, reason: 'ボーナス/AT終了後66G以内の引き戻し確認条件です', rule: 'valvrave_66g_pullback' };
    if (finiteNumber(context.cz_misses, 0) >= 7) return { active: true, verified: true, adjusted_start_threshold: 0, reason: 'CZ7スルー後は次回CZ成功確定です', rule: 'valvrave_cz_7_misses' };
    if (finiteNumber(context.decision_bonus_streak, 0) >= 4) return { active: true, verified: true, adjusted_start_threshold: 0, reason: '決戦ボーナス4連続後は次回革命ボーナス確定です', rule: 'valvrave_decision_bonus_4' };
  }
  if (name.includes('聖闘士星矢')) {
    if (finiteNumber(context.gb_misses, 0) >= 8) return { active: true, verified: true, adjusted_start_threshold: 0, reason: 'GB8連続スルー後は次回GBでAT当選します', rule: 'seiya_gb_8_misses' };
    if (context.futsu_hint === 'max') return { active: true, verified: true, adjusted_start_threshold: 0, reason: '不屈MAX示唆を確認済みです', rule: 'seiya_futsu_max' };
  }
  if (context.sectionDifferenceCoins !== null && context.sectionDifferenceCoins !== undefined && context.sectionDifferenceCoins !== '') {
    return { active: false, verified: false, reason: '強制切断差枚ラインを公開情報で確認できないため自動補正しません', rule: 'unverified_difference_only' };
  }
  return { active: false };
}

export function calculateSectionAdjustment(profile, sectionDifferenceCoins, context = {}) {
  const difference = finiteNumber(sectionDifferenceCoins);
  const enabled = profile?.section_cut_enabled === true || profile?.section_cut_enabled === 1
    || profile?.section_cut_verified === true || profile?.section_cut_verified === 1
    || profile?.section_cut_target_coins !== undefined;
  if (!enabled) return calculateMachineAdjustment(profile, { ...context, sectionDifferenceCoins });
  if (difference === null) return { active: false };

  const target = finiteNumber(profile?.section_cut_target_coins, 2400);
  const windowCoins = Math.max(1, finiteNumber(profile?.section_cut_window_coins, 1200));
  const maxReduction = Math.max(0, finiteNumber(profile?.section_cut_max_border_reduction, 100));
  const remaining = target - difference;
  if (remaining <= 0 || remaining > windowCoins) {
    return { active: false, current_difference_coins: difference, remaining_to_cut_coins: Math.max(0, remaining), target_coins: target };
  }

  const proximity = clamp(1 - remaining / windowCoins, 0, 1);
  const reduction = Math.floor((maxReduction * proximity) / 10) * 10;
  return {
    active: reduction > 0,
    verified: profile?.section_cut_verified === true || profile?.section_cut_verified === 1,
    current_difference_coins: difference,
    remaining_to_cut_coins: Math.round(remaining),
    target_coins: target,
    proximity,
    border_reduction: reduction,
  };
}

export function calculateReplayAdjustment({
  profile,
  exchangeType,
  fundingMode,
  replayLimitMedals,
  replayUsedMedals,
  exchangeRate,
}) {
  const rate = finiteNumber(exchangeRate, exchangeType === '56' ? 5.6 : null);
  const limit = Math.max(0, finiteNumber(replayLimitMedals, 0));
  const used = Math.max(0, finiteNumber(replayUsedMedals, 0));
  if (exchangeType === 'equivalent' || fundingMode !== 'medals' || !rate || rate <= 5 || limit <= 0) {
    return { active: false, remaining_replay_medals: Math.max(0, limit - used), cash_gap_yen: 0 };
  }

  const remainingReplay = Math.max(0, limit - used);
  const cashCostPerMedal = 20;
  const storedMedalValue = 100 / rate;
  const gapPerMedal = Math.max(0, cashCostPerMedal - storedMedalValue);
  const curve = Array.isArray(profile?.curve_points)
    ? [...profile.curve_points].sort((a, b) => Number(a.value) - Number(b.value))
    : [];
  const targetEv = Math.max(0, finiteNumber(profile?.expected_value_yen, 0));
  const adjustedPoints = curve.map(point => {
    const worstYen = Math.max(0, finiteNumber(point.worst_case_yen, profile?.worst_case_investment_yen || 0));
    const requiredMedals = Math.ceil(worstYen / cashCostPerMedal);
    const cashMedals = Math.max(0, requiredMedals - remainingReplay);
    const cashGap = Math.round(cashMedals * gapPerMedal);
    return { ...point, required_medals: requiredMedals, cash_medals: cashMedals, cash_gap_yen: cashGap, adjusted_ev_yen: Number(point.ev_yen || 0) - cashGap };
  });
  const thresholdPoint = adjustedPoints.find(point => point.adjusted_ev_yen >= targetEv);
  return {
    active: true,
    exchange_rate: rate,
    replay_limit_medals: limit,
    replay_used_medals: used,
    remaining_replay_medals: remainingReplay,
    gap_per_cash_medal_yen: gapPerMedal,
    adjusted_start_threshold: thresholdPoint ? Number(thresholdPoint.value) : null,
    adjusted_points: adjustedPoints,
  };
}

export function assessCandidate(profile, currentValue, riskCapacityYen, context = {}) {
  if (!profile) {
    return { judgment: 'unknown', reason: '一致する狙い目ルールがありません', actionable: false };
  }
  const current = Number(currentValue);
  const start = Number(profile.start_threshold);
  if (!Number.isFinite(current)) {
    return { judgment: 'unknown', reason: '現在値を入力してください', actionable: false };
  }
  const section = calculateSectionAdjustment(profile, context.sectionDifferenceCoins, context);
  const sectionStart = section.active && section.adjusted_start_threshold !== undefined
    ? Math.max(0, Number(section.adjusted_start_threshold))
    : (section.active ? Math.max(0, start - section.border_reduction) : start);
  const replay = calculateReplayAdjustment({ profile, ...context });
  const replayStart = replay.active && replay.adjusted_start_threshold !== null
    ? replay.adjusted_start_threshold
    : null;
  if (replay.active && replayStart === null) {
    return {
      judgment: 'verify',
      reason: '現金ギャップを含めた安全な着席ラインを算出できません',
      actionable: false,
      base_start_threshold: start,
      adjusted_start_threshold: null,
      section_adjustment: section,
      replay_adjustment: replay,
    };
  }
  const adjustedStart = replayStart === null ? sectionStart : Math.max(sectionStart, replayStart);
  if (current < adjustedStart) {
    return {
      judgment: 'wait',
      reason: `補正後ラインまであと${(adjustedStart - current).toLocaleString('ja-JP')}${profile.unit_label}`,
      actionable: false,
      base_start_threshold: start,
      adjusted_start_threshold: adjustedStart,
      section_adjustment: section,
      replay_adjustment: replay,
    };
  }
  if (!['official', 'verified'].includes(profile.confidence) || !profile.verified_on || !profile.source_name) {
    return { judgment: 'verify', reason: '出典または確認日が不十分です', actionable: false };
  }
  const point = curvePointAt(profile, current);
  const baseExpected = point?.ev_yen ?? profile.expected_value_yen;
  const worst = point?.worst_case_yen ?? profile.worst_case_investment_yen;
  const duration = estimateDuration(profile, current);
  const minutes = point?.minutes ?? duration.estimated_play_minutes;
  if (baseExpected == null) return { judgment: 'verify', reason: '期待値が未登録です', actionable: false };
  if (worst == null) return { judgment: 'verify', reason: '必要資金が未登録です', actionable: false };
  let cashGapYen = 0;
  if (replay.active) {
    const adjustedPoint = [...(replay.adjusted_points || [])].reverse()
      .find(item => Number(item.value) <= current);
    const requiredMedals = Math.ceil(Number(worst) / 20);
    const cashMedals = Math.max(0, requiredMedals - Number(replay.remaining_replay_medals || 0));
    cashGapYen = adjustedPoint?.cash_gap_yen
      ?? Math.round(cashMedals * Number(replay.gap_per_cash_medal_yen || 0));
  }
  const expected = Number(baseExpected) - cashGapYen;
  if (expected <= 0) {
    return {
      judgment: 'wait', reason: '現金ギャップ反映後の期待値がプラスになりません', actionable: false,
      base_expected_value_yen: Number(baseExpected), expected_value_yen: expected,
      cash_gap_yen: cashGapYen, base_start_threshold: start, adjusted_start_threshold: adjustedStart,
      section_adjustment: section, replay_adjustment: replay,
    };
  }
  if (Number(worst) > Number(riskCapacityYen || 0)) {
    return {
      judgment: 'insufficient_funds',
      reason: `必要資金${money(worst)}に対し許容${money(riskCapacityYen || 0)}`,
      actionable: false,
      expected_value_yen: Number(expected),
      base_expected_value_yen: Number(baseExpected),
      cash_gap_yen: cashGapYen,
      worst_case_investment_yen: Number(worst),
      base_start_threshold: start,
      adjusted_start_threshold: adjustedStart,
      section_adjustment: section,
      replay_adjustment: replay,
    };
  }
  const sectionOnlyTarget = current < start && section.active;
  const sectionNeedsVerification = sectionOnlyTarget && !section.verified;
  return {
    judgment: sectionNeedsVerification ? 'verify' : 'target',
    reason: sectionNeedsVerification
      ? '有利区間差枚でボーダーが浅くなりますが、機種固有の補正値が未検証です'
      : (section.active && section.reason ? section.reason : (cashGapYen > 0 ? '現金ギャップを差し引いても条件を満たしています' : '登録条件と資金条件を満たしています')),
    actionable: !sectionNeedsVerification,
    expected_value_yen: Number(expected),
    base_expected_value_yen: Number(baseExpected),
    cash_gap_yen: cashGapYen,
    worst_case_investment_yen: Number(worst),
    estimated_play_minutes: minutes ? Number(minutes) : null,
    safe_play_minutes: duration.safe_play_minutes,
    duration_breakdown: duration.breakdown,
    duration_confidence: duration.confidence,
    ev_per_hour_yen: minutes ? Math.round(Number(expected) * 60 / Number(minutes)) : null,
    matched_curve_value: point?.value ?? null,
    base_start_threshold: start,
    adjusted_start_threshold: adjustedStart,
    section_adjustment: section,
    replay_adjustment: replay,
  };
}

export function validateProfileInputs(profile, extraInputs = {}) {
  const fields = Array.isArray(profile?.input_fields) ? profile.input_fields : [];
  const requirements = Array.isArray(profile?.requirements) ? profile.requirements : [];
  const errors = [];
  for (const field of fields) {
    const value = extraInputs[field.id];
    if (field.required && (value === '' || value === null || value === undefined)) {
      errors.push(`${field.label}が未入力`);
    }
  }
  for (const rule of requirements) {
    const raw = extraInputs[rule.field];
    if (raw === '' || raw === null || raw === undefined) continue;
    const numeric = Number(raw);
    const expected = rule.value;
    let matched = true;
    if (rule.operator === 'gte') matched = Number.isFinite(numeric) && numeric >= Number(expected);
    else if (rule.operator === 'lte') matched = Number.isFinite(numeric) && numeric <= Number(expected);
    else if (rule.operator === 'eq') matched = String(raw) === String(expected);
    else if (rule.operator === 'in') matched = Array.isArray(expected) && expected.map(String).includes(String(raw));
    if (!matched) errors.push(rule.message || `${rule.field}が条件外`);
  }
  return errors;
}

export function assessQuick({
  profile,
  currentValue,
  riskCapacityYen,
  exchangeType,
  fundingMode,
  resetStatus,
  minutesUntilClose,
  extraInputs = {},
  sectionDifferenceCoins = null,
  replayLimitMedals = 0,
  replayUsedMedals = 0,
  exchangeRate = null,
  today = new Date(),
}) {
  if (!profile) return { judgment: 'unknown', reason: '一致する狙い目ルールがありません', actionable: false, warnings: [] };
  const mismatches = [];
  const ruleExchange = profile.exchange_type || 'unknown';
  const ruleFunding = profile.funding_mode || 'any';
  const ruleReset = profile.reset_status || 'unknown';
  if (ruleExchange === 'unknown') mismatches.push('ルールの交換条件が未登録');
  else if (ruleExchange !== exchangeType) mismatches.push('交換条件がルールと不一致');
  if (!['any', fundingMode].includes(ruleFunding)) mismatches.push('現金／持ちメダル条件が不一致');
  if (resetStatus === 'unknown') mismatches.push('据え置き／リセット状況が未確認');
  else if (ruleReset === 'unknown') mismatches.push('ルールのリセット条件が未登録');
  else if (!['any', resetStatus].includes(ruleReset)) mismatches.push('リセット条件がルールと不一致');
  mismatches.push(...validateProfileInputs(profile, extraInputs));
  if (mismatches.length) {
    return { judgment: 'condition_mismatch', reason: mismatches.join('・'), actionable: false, warnings: [] };
  }

  const assessmentContext = {
    sectionDifferenceCoins,
    replayLimitMedals,
    replayUsedMedals,
    exchangeRate,
    exchangeType,
    fundingMode,
    ...extraInputs,
  };
  const assessment = {
    ...assessCandidate(profile, currentValue, Math.max(0, Number(riskCapacityYen || 0)), assessmentContext),
    warnings: [],
    minutes_until_close: Number(minutesUntilClose || 0),
  };
  const verifiedOn = profile.verified_on ? new Date(`${profile.verified_on}T00:00:00`) : null;
  if (verifiedOn && !Number.isNaN(verifiedOn.getTime())) {
    const ageDays = Math.floor((today.getTime() - verifiedOn.getTime()) / 86400000);
    if (ageDays > 180 && assessment.actionable) {
      assessment.judgment = 'verify';
      assessment.reason = '情報確認から180日を超えています';
      assessment.actionable = false;
    }
  }
  if (Number(minutesUntilClose || 0) <= 0) {
    assessment.judgment = 'closing_risk';
    assessment.reason = '閉店時刻を過ぎています';
    assessment.actionable = false;
    return assessment;
  }
  const estimated = assessment.estimated_play_minutes ?? estimateDuration(profile, currentValue).estimated_play_minutes;
  const required = assessment.safe_play_minutes ?? estimateDuration(profile, currentValue).safe_play_minutes;
  assessment.required_minutes_with_buffer = required;
  if (assessment.judgment === 'target' && Number(minutesUntilClose) < required) {
    assessment.judgment = 'closing_risk';
    assessment.reason = estimated
      ? `平均${estimated}分・安全側${required}分に対し、閉店まで${minutesUntilClose}分`
      : `消化時間が未登録のため、閉店2時間以内は見送り（残り${minutesUntilClose}分）`;
    assessment.actionable = false;
  } else if (assessment.judgment === 'target' && !estimated) {
    assessment.warnings.push('消化時間未登録：閉店リスクは安全側の120分基準');
  }
  return assessment;
}

export function buildGuideRows(profiles, summary, mode = 'targets', search = '') {
  const query = search.trim().toLowerCase();
  const rows = [];
  for (const profile of profiles || []) {
    if (query && !`${profile.machine_name} ${profile.condition_label || ''}`.toLowerCase().includes(query)) continue;
    const curves = Array.isArray(profile.curve_points)
      ? [...profile.curve_points].sort((a, b) => Number(a.value) - Number(b.value))
      : [];
    const exact = curves.find(point => Number(point.value) === Number(profile.start_threshold));
    const next = curves.find(point => Number(point.value) >= Number(profile.start_threshold));
    const points = mode === 'all' && curves.length ? curves : [exact || next || {
      value: profile.start_threshold,
      ev_yen: profile.expected_value_yen,
      worst_case_yen: profile.worst_case_investment_yen,
    }];
    for (const point of points) {
      const assessment = assessCandidate(profile, point.value, summary.risk_capacity_yen);
      rows.push({ profile, point, assessment });
    }
  }
  const priority = { target: 0, verify: 1, insufficient_funds: 2, wait: 3, unknown: 4 };
  return rows.sort((a, b) => mode === 'targets'
    ? (priority[a.assessment.judgment] - priority[b.assessment.judgment]) || Number(b.point.ev_yen || 0) - Number(a.point.ev_yen || 0)
    : a.profile.machine_name.localeCompare(b.profile.machine_name, 'ja') || Number(a.point.value) - Number(b.point.value));
}

export function minutesUntilClosing(timeValue, now = new Date()) {
  const match = /^(\d{2}):(\d{2})$/.exec(timeValue || '');
  if (!match) return 0;
  const close = new Date(now);
  close.setHours(Number(match[1]), Number(match[2]), 0, 0);
  return Math.max(0, Math.ceil((close.getTime() - now.getTime()) / 60000));
}
