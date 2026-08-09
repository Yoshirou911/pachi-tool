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

export function assessCandidate(profile, currentValue, riskCapacityYen) {
  if (!profile) {
    return { judgment: 'unknown', reason: '一致する狙い目ルールがありません', actionable: false };
  }
  const current = Number(currentValue);
  const start = Number(profile.start_threshold);
  if (!Number.isFinite(current)) {
    return { judgment: 'unknown', reason: '現在値を入力してください', actionable: false };
  }
  if (current < start) {
    return {
      judgment: 'wait',
      reason: `開始ラインまであと${(start - current).toLocaleString('ja-JP')}${profile.unit_label}`,
      actionable: false,
    };
  }
  if (!['official', 'verified'].includes(profile.confidence) || !profile.verified_on || !profile.source_name) {
    return { judgment: 'verify', reason: '出典または確認日が不十分です', actionable: false };
  }
  const point = curvePointAt(profile, current);
  const expected = point?.ev_yen ?? profile.expected_value_yen;
  const worst = point?.worst_case_yen ?? profile.worst_case_investment_yen;
  const minutes = point?.minutes ?? profile.estimated_play_minutes;
  if (expected == null) return { judgment: 'verify', reason: '期待値が未登録です', actionable: false };
  if (worst == null) return { judgment: 'verify', reason: '必要資金が未登録です', actionable: false };
  if (Number(worst) > Number(riskCapacityYen || 0)) {
    return {
      judgment: 'insufficient_funds',
      reason: `必要資金${money(worst)}に対し許容${money(riskCapacityYen || 0)}`,
      actionable: false,
      expected_value_yen: Number(expected),
      worst_case_investment_yen: Number(worst),
    };
  }
  return {
    judgment: 'target',
    reason: '登録条件と資金条件を満たしています',
    actionable: true,
    expected_value_yen: Number(expected),
    worst_case_investment_yen: Number(worst),
    estimated_play_minutes: minutes ? Number(minutes) : null,
    ev_per_hour_yen: minutes ? Math.round(Number(expected) * 60 / Number(minutes)) : null,
    matched_curve_value: point?.value ?? null,
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

  const assessment = {
    ...assessCandidate(profile, currentValue, Math.max(0, Number(riskCapacityYen || 0))),
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
  const estimated = assessment.estimated_play_minutes ?? profile.estimated_play_minutes;
  const required = estimated ? Number(estimated) + 30 : 120;
  assessment.required_minutes_with_buffer = required;
  if (assessment.judgment === 'target' && Number(minutesUntilClose) < required) {
    assessment.judgment = 'closing_risk';
    assessment.reason = estimated
      ? `消化目安${estimated}分＋余裕30分に対し、閉店まで${minutesUntilClose}分`
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
