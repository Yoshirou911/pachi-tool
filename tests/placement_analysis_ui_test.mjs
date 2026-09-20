import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {applyPlacement, serializePlacement, placementStudyMarkup} from '../mobile/placement-analysis.mjs';

const seats = [501,508,503].map(seat_number => ({seat_number,island_name:'A'}));
const text = '501,A,通路側,1\n508,A,通路側,2\n503,A,通路側,3';
const updated = applyPlacement(seats,text);
assert.equal(serializePlacement(updated),text);
assert.equal(seats[0].row_name,undefined);
assert.ok(applyPlacement(updated,'').every(s=>s.row_order==null && s.row_name===''));
for(const bad of ['502,A,列,1','501,A,列,1\n501,A,列,2','501,A,列,1\n508,A,列,1','501,A,列,1.5','501,A,,1','501,,列,1']) {
  assert.throws(()=>applyPlacement(seats,bad));
}
assert.equal(placementStudyMarkup(undefined),'');
const html = placementStudyMarkup([{hall_name:'<img onerror=x>',candidate_groups:0,layout_issues:{'不足<script>':1},
  profiles:[{name:'<script>',kind:'row3',seat_numbers:[501,508,503],paired_days:0,blockers:['<script>'],
    source_url:'javascript:alert(1)',validation:{days:0,unresolved_days:0,trials:[]}}],
  transitions:[{machine_name:'北斗',previous_state:'negative',pairs:2,days:1,positive_rate_pct:50,
    average_next_coins:0,status:'検証不足',observations:[{date:'2026-08-02',seat_number:501,
      previous_coins:-100,actual_coins:0,source_urls:['https://example.com/" onclick="alert(1)','javascript:x']}]}],
  setting_evidence:{pairs:0},retired_group_validations:[]}]);
assert.match(html,/並び・島・翌日の傾向/);
assert.match(html,/501番/);
assert.match(html,/前日マイナス/);
assert.match(html,/翌日プラス 50%/);
assert.match(html,/未計測/);
assert.match(html,/両日1000G以上/);
assert.doesNotMatch(html,/<script>|<img|href="javascript:|" onclick="/);
assert.match(html,/noopener noreferrer/);
const shared=readFileSync(new URL('../mobile/prediction-verification.mjs',import.meta.url),'utf8');
assert.match(shared,/placementStudyMarkup\(data.placement_studies\)/);
for(const [file,id] of [['../web/index.html','desktop-layout-rows'],['../mobile/index.html','floor-rows']]) {
  assert.ok(readFileSync(new URL(file,import.meta.url),'utf8').includes(`id="${id}"`));
}
console.log('placement analysis UI and input tests passed');
