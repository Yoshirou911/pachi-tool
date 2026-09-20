import {assetVersionPattern} from './asset_version.mjs';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {probabilityMarkup} from '../mobile/probability-validation.mjs';

assert.match(probabilityMarkup(null), /まだ確認できていません/);
assert.match(probabilityMarkup({items:[]}), /旧結果を後付け/);
const item = {scope:'seat',label:'現在の予測<script>',version:'3.31',status:'reference_improved',
  reasons:[],bands:[{low_pct:80,high_pct:100,count:1,days:1,predicted_pct:100,actual_pct:0}],
  paired_before_brier:0,paired_after_brier:null,
  existing_interval:{count:0,days:0,coverage_pct:null},candidate_interval:{count:1,days:1,coverage_pct:0}};
const html = probabilityMarkup({items:[item]});
assert.match(html,/参考改善あり・実戦未適用/);
assert.match(html,/0\.0000 → 未計測/);
assert.match(html,/&lt;script&gt;/);
assert.doesNotMatch(html,/<script>/);
assert.match(html,/value="100"/);
assert.match(html,/実績 0\.0%/);
assert.match(html,/幅内 0\.0%/);
assert.match(html,/幅内 未計測/);
const module = readFileSync(new URL('../mobile/probability-validation.mjs',import.meta.url),'utf8');
assert.doesNotMatch(module,/setInterval|setTimeout|method:\s*['"]POST/);
assert.match(module,/busy = true/);
assert.match(module,/finally/);
const shared = readFileSync(new URL('../mobile/prediction-verification.mjs',import.meta.url),'utf8');
assert.match(shared,/mountProbabilityValidation\(probability, request\)/);
const sw = readFileSync(new URL('../mobile/sw.js',import.meta.url),'utf8');
assert.match(sw,assetVersionPattern('probability-validation.mjs'));
console.log('probability validation UI tests passed');
