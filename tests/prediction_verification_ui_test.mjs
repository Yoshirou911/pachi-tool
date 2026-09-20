import {assetVersionPattern} from './asset_version.mjs';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {qualityMarkup, comparisonMarkup, seatHistoryMarkup, eventStudyMarkup, machineStudyMarkup, seatRankingMarkup} from '../mobile/prediction-verification.mjs';

const html = qualityMarkup({input_cutoff_date: '2026-09-07', input_quality: {machine: {raw_rows: 3, usable_rows: 1, conflict_keys: 1}}, halls: [{hall_name: '<script>x</script>', data_quality: {input_audit: {label: '参考限定', sample_days: 1, calendar_coverage_pct: 2, games_known_pct: 0, blockers: ['不足']}}}]});
assert.match(html, /利用1件/);
assert.match(html, /&lt;script&gt;/);
assert.doesNotMatch(html, /<script>/);
assert.match(html, /2026-09-07/);
for (const file of ['../mobile/app.js', '../web/js/app.js']) {
  const app = readFileSync(new URL(file, import.meta.url), 'utf8');
  assert.match(app, assetVersionPattern('prediction-verification.mjs'));
  assert.match(app, /mountVerification/);
}
const sw = readFileSync(new URL('../mobile/sw.js', import.meta.url), 'utf8');
assert.match(sw, /pathname\.startsWith\('\/api\/'\)/);
console.log('prediction verification UI contract passed');

assert.match(comparisonMarkup(null), /まだありません/);
const comparison = comparisonMarkup({mode_notice:'研究用', legacy_batches_excluded:1, cohorts:[{
  scope:'seat', version:'<script>x</script>', common:2, total:3, excluded:1, resolved:0, days:0, pending:2,
  models:[{label:'現在の予測', brier:null, daily_brier:null, mae_coins:null, signal_count:0, signal_rate_pct:0, signal_resolved:0, signal_pending:0, signal_success_pct:null}],
  paired:[], breakdown:{hall_name:[{name:'<img onerror=x>', resolved:0, days:0, models:[]}], machine_key:[]},
}]});
assert.match(comparison, /未計測/);
assert.match(comparison, /結果待ち 2件/);
assert.match(comparison, /旧版の1日分は比較対象外/);
assert.doesNotMatch(comparison, /<script>|<img onerror/);
assert.match(comparison, /着席推奨とは別/);
const shared = readFileSync(new URL('../mobile/prediction-verification.mjs', import.meta.url), 'utf8');
assert.match(shared, /\/api\/predictions\/comparison/);
assert.match(shared, /\/api\/predictions\/benchmark/);
assert.match(shared, /<progress max="100"/);
assert.match(shared, /element\.isConnected/);
console.log('prediction comparison UI contract passed');

const history = seatHistoryMarkup({cutoff_date:'2026-09-08', excluded_records:2, notice:'履歴', seats:[{
  hall_name:'<script>x</script>', seat_number:101, machine_name:'L北斗', status:'情報矛盾',
  training_from:'2026-08-02', training_records:0, last_observed:'2026-08-03', changes:1,
  periods:[{machine_name:'<img onerror=x>', start_observed:'2026-08-02', last_observed:'2026-08-03',
    reason:'machine_changed', previous_observed:'2026-08-01', position:['1階','<script>'],
    source_urls:['javascript:alert(1)', 'https://example.com/" onclick="alert(1)']}],
}]});
assert.match(history, /台番号・配置・入替の観測履歴/);
assert.match(history, /機種変更を観測/);
assert.match(history, /101番台/);
assert.doesNotMatch(history, /<script>|<img onerror|href="javascript:|" onclick="/);
assert.match(history, /noopener noreferrer/);
assert.match(seatHistoryMarkup({seats:[]}), /観測履歴はまだありません/);
console.log('seat history UI contract passed');

const eventHtml = eventStudyMarkup([{hall_name:'<script>x</script>', information_date:'2026-08-01', notice:'研究用', profiles:[{
  event_name:'取材<img>', status:'検証不足', target_known:false, paired_days:0, matched_lift_coins:null,
  predicted_coins:null, adjustment_coins:null, validation:{days:0, trials:[]}, blockers:['データ不足'],
  unknown_timestamp_records:3, overlap_days:0, source_urls:['javascript:alert(1)','https://example.com/'],
}]}]);
assert.match(eventHtml, /店舗×イベントの比較/);
assert.match(eventHtml, /未計測/);
assert.match(eventHtml, /加点はありません/);
assert.doesNotMatch(eventHtml, /<script>|<img>|href="javascript:/);
console.log('hall-event UI contract passed');

const machineHtml = machineStudyMarkup([{hall_name:'<script>x</script>', input_cutoff_date:'2026-08-01',
  activity_excluded_rows:2, notice:'研究用', profiles:[{machine_name:'<img>', status:'検証不足',
    observed_days:5, usable_days:3, paired_days:0, recent_paired_days:0, relative_diff_coins:null,
    latest_date:'2026-08-01', installation_status:'未確認', candidate:null,
    validation:{days:0, unevaluated_days:3, trials:[]}, blockers:['比較日不足'],
    source_urls:['javascript:alert(1)','https://example.com/'],
  }]}]);
assert.match(machineHtml, /店舗×機種の比較/);
assert.match(machineHtml, /未計測/);
assert.match(machineHtml, /現在の順位や着席判定を変更しません/);
assert.doesNotMatch(machineHtml, /<script>|<img>|href="javascript:/);
assert.match(machineStudyMarkup([{hall_name:'店', profiles:[]}]), /弱い店、という判定ではありません/);
console.log('hall-machine UI contract passed');

const seatHtml = seatRankingMarkup([{hall_name:'<script>x</script>', target_date:'2026-09-11', input_cutoff_date:'2026-09-09',
  ranked_seats:2, observed_seats:3, installation_status:'未確認', machines:[{machine_name:'<img>', ranked_seats:2,
    profiles:[{rank:1, baseline_rank:2, seat_number:101, candidate:{predicted_coins:200}, validation_status:'検証不足',
      paired_days:11, usable_days:12, training_from:'2026-08-20', latest_date:'2026-09-08', identity_status:'観測中',
      blockers:[], validation:{days:0, unresolved_days:1, trials:[]}, source_urls:['javascript:alert(1)','https://example.com/']},
    {rank:null, seat_number:102, candidate:null, validation_status:'検証不足', blockers:['<script>材料不足'],
      validation:{days:0, unresolved_days:0, trials:[]}, source_urls:[]}],
    validation:{days:0, attempted_days:1, unresolved_days:1, status:'検証不足', trials:[{date:'2026-09-08',cohort_size:2,
      selected_seats:[101],baseline_selected_seats:[102],resolved:false,actual_coins:null,unresolved_seats:[101]}]}}]}]);
assert.match(seatHtml, /台番号ランキング（3.28・研究用）/);
assert.match(seatHtml, /研究順位 1位/);
assert.match(seatHtml, /101番台/);
assert.match(seatHtml, /順位なし・理由を見る/);
assert.match(seatHtml, /未計測/);
assert.match(seatHtml, /着席許可ではありません/);
assert.match(seatHtml, /全比較台の結果が揃った日/);
assert.doesNotMatch(seatHtml, /<script>|<img>|href="javascript:/);
assert.match(seatRankingMarkup([{hall_name:'店',machines:[]}]), /機種平均から番号を作ることはしません/);
assert.equal(seatRankingMarkup(undefined), '');
assert.match(shared, /seatRankingMarkup\(data.seat_ranking_studies\)/);
console.log('seat-ranking UI contract passed');
