import assert from 'node:assert/strict';
import { assessQuick, buildGuideRows, buildPerformanceSeries, buildValidationSummary, calculateSummary } from '../mobile/core.mjs';

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

const inputMismatch = assessQuick({
  profile: {
    ...profile,
    input_fields: [{ id: 'counter', label: '確認したカウンター', required: true }],
    requirements: [{ field: 'counter', operator: 'eq', value: 'at', message: 'AT間を確認してください' }],
  },
  currentValue: 600,
  riskCapacityYen: 30000,
  exchangeType: 'equivalent',
  fundingMode: 'cash',
  resetStatus: 'normal',
  minutesUntilClose: 180,
  extraInputs: { counter: 'cz' },
  today: new Date('2026-08-09T12:00:00'),
});
assert.equal(inputMismatch.judgment, 'condition_mismatch');
assert.match(inputMismatch.reason, /AT間/);

const rows = buildGuideRows([profile], summary, 'all');
assert.equal(rows.length, 3);
assert.equal(rows.at(-1).assessment.judgment, 'target');

const performance = buildPerformanceSeries([
  { played_on: '2026-08-02', expected_value_yen: 2000, investment_yen: 8000, returns_yen: 5000 },
  { played_on: '2026-08-01', expected_value_yen: 1500, investment_yen: 5000, returns_yen: 9000 },
]);
assert.equal(performance.tracked_count, 2);
assert.equal(performance.total_expected_yen, 3500);
assert.equal(performance.total_actual_yen, 1000);
assert.equal(performance.gap_yen, -2500);
assert.deepEqual(performance.points.map(point => point.cumulative_expected_yen), [1500, 3500]);

const legacyPerformance = buildPerformanceSeries([{ investment_yen: 1000, returns_yen: 2000 }]);
assert.equal(legacyPerformance.tracked_count, 0);

const validation = buildValidationSummary(Array.from({ length: 10 }, () => ({
  catalog_key: 'test-v1', machine_name: 'テスト台', expected_value_yen: 1000,
  investment_yen: 5000, returns_yen: 6200, played_minutes: 60,
})));
assert.equal(validation[0].count, 10);
assert.equal(validation[0].sample_level, 'watch');
assert.equal(validation[0].gap_yen, 2000);

console.log('mobile core tests passed');
