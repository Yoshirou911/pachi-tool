import {assetVersionPattern} from './asset_version.mjs';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {candidateMarkup} from '../mobile/candidate-evaluation.mjs';

assert.match(candidateMarkup(null),/まだ取得/);
assert.match(candidateMarkup({items:[]}),/記録はまだありません/);
const metric={k:1,common_days:1,candidate_days:1,complete_days:1,pending:0,common_positive_pct:0,pool_positive_pct:null,common_avg_diff_coins:0,status:'hold',reasons:['不足<script>']};
const html=candidateMarkup({items:[{mode:'research',scope:'seat',pool:'all',label:'現在<script>',version:'3.32',metrics:[metric],
  recent:[{date:'2026-09-10',rows:[{key:['seat','店','機種',123],actual:null,projected:0}]}]}]});
assert.match(html,/0\.0%/); assert.match(html,/未計測/);
assert.match(html,/予測値自体は事前保存済み/);
assert.match(html,/123番台/); assert.match(html,/過去の評価対象/);
assert.match(html,/&lt;script&gt;/); assert.doesNotMatch(html,/<script>/);
assert.match(html,/同じ日だけ/);
const module=readFileSync(new URL('../mobile/candidate-evaluation.mjs',import.meta.url),'utf8');
assert.doesNotMatch(module,/setTimeout|setInterval|method:\s*['"]POST/);
assert.match(module,/if \(busy\) return/); assert.match(module,/finally/);
const shared=readFileSync(new URL('../mobile/prediction-verification.mjs',import.meta.url),'utf8');
assert.match(shared,/mountCandidateEvaluation\(candidates, request\)/);
assert.match(readFileSync(new URL('../mobile/sw.js',import.meta.url),'utf8'),assetVersionPattern('candidate-evaluation.mjs'));
console.log('candidate evaluation UI tests passed');
