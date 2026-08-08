import assert from 'node:assert/strict';
import { assessQuick, buildGuideRows, calculateSummary } from '../mobile/core.mjs';

const profile = {
  id: 1,
  machine_name: 'テスト台',
  condition_label: '通常・等価',
  exchange_type: 'equivalent',
  funding_mode: 'any',
  reset_status: 'normal',
  metric_name: '現在ゲーム数',
  unit_label: 'G',
  start_threshold: 500,
  expected_value_yen: 1200,
  worst_case_investment_yen: 10000,
  source_name: '確認済み資料',
  verified_on: '2026-08-08',
  confidence: 'verified',
  curve_points: [
    { value: 400, ev_yen: -100 },
    { value: 500, ev_yen: 1200, worst_case_yen: 10000 },
    { value: 600, ev_yen: 2200, worst_case_yen: 7000 },
  ],
};

const summary = calculateSummary({
  budget: { starting_bankroll: 50000, loss_limit_yen: 30000 },
  results: [{ investment_yen: 10000, returns_yen: 15000 }],
});
assert.equal(summary.current_bankroll, 55000);
assert.equal(summary.risk_capacity_yen, 30000);

const target = assessQuick({
  profile,
  currentValue: 600,
  riskCapacityYen: summary.risk_capacity_yen,
  exchangeType: 'equivalent',
  fundingMode: 'cash',
  resetStatus: 'normal',
  minutesUntilClose: 180,
  today: new Date('2026-08-09T12:00:00'),
});
assert.equal(target.judgment, 'target');
assert.equal(target.expected_value_yen, 2200);

const mismatch = assessQuick({
  profile,
  currentValue: 600,
  riskCapacityYen: 30000,
  exchangeType: '56',
  fundingMode: 'cash',
  resetStatus: 'normal',
  minutesUntilClose: 180,
  today: new Date('2026-08-09T12:00:00'),
});
assert.equal(mismatch.judgment, 'condition_mismatch');

const closing = assessQuick({
  profile,
  currentValue: 600,
  riskCapacityYen: 30000,
  exchangeType: 'equivalent',
  fundingMode: 'cash',
  resetStatus: 'normal',
  minutesUntilClose: 60,
  today: new Date('2026-08-09T12:00:00'),
});
assert.equal(closing.judgment, 'closing_risk');

const rows = buildGuideRows([profile], summary, 'all');
assert.equal(rows.length, 3);
assert.equal(rows.at(-1).assessment.judgment, 'target');

console.log('mobile core tests passed');
