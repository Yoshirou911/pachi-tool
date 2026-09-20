import {assetVersionPattern} from './asset_version.mjs';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {reviewMarkup, researchMarkup} from '../mobile/model-review.mjs';

assert.match(reviewMarkup(null), /まだ審査していません/);
const html = reviewMarkup({id:1, window_start:'2026-06-15',window_end:'2026-09-06',current_implementation:false,
  items:[{label:'<script>機種', decision:'adoptable',reasons:['<img>理由'],saved:40,comparable:40,resolved:0,pending:40,
    coverage:0,metrics:{days:0,weeks:0,candidate_mae:null,comparisons:[]}}]});
assert.match(html, /採用可・未適用/);
assert.match(html, /現在のコードには適用できません/);
assert.match(html, /待ち 40件/);
assert.match(html, /未計測/);
assert.doesNotMatch(html, /<script>|<img>/);
assert.match(researchMarkup({items:[{label:'台番号',resolved:0,days:0,baseline_mae:null,candidate_mae:null}]}), /未計測/);
const src = readFileSync(new URL('../mobile/model-review.mjs',import.meta.url),'utf8');
assert.match(src, /30日・8週/);
assert.match(src, /50枚以上かつ5%以上/);
assert.match(src, /if \(busy\) return/);
assert.doesNotMatch(src, /\/api\/hall\/target_search|\/benchmark|setInterval/);
assert.match(readFileSync(new URL('../mobile/prediction-verification.mjs',import.meta.url),'utf8'), /mountModelReview/);
assert.match(readFileSync(new URL('../mobile/sw.js',import.meta.url),'utf8'), assetVersionPattern('model-review.mjs'));
console.log('model review UI tests passed');
