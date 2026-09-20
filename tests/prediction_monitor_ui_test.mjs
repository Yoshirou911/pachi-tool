import {assetVersionPattern} from './asset_version.mjs';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {monitorMarkup,mountPredictionMonitor} from '../mobile/prediction-monitor.mjs';

assert.match(monitorMarkup(null),/まだ取得/);
assert.match(monitorMarkup({items:[]}),/比較できる事前記録がまだありません/);
const w={metrics:{brier:0,mae_coins:null,avg_diff_coins:0,positive_pct:0},resolution:0,panel_share:1,complete_days:0};
const html=monitorMarkup({items:[{hall_name:'店<script>',status:'insufficient_data',scope:'seat',label:'現在',
  version:'3.33',reasons:['不足<script>'],baseline:w,recent:w,halves:[w,w],delta:{brier:0},alerts:[]}]});
assert.match(html,/0\.000/); assert.match(html,/未計測/); assert.match(html,/データ不足・判定保留/);
assert.match(html,/&lt;script&gt;/); assert.doesNotMatch(html,/<script>/);
assert.match(html,/台番号/); assert.match(html,/横スクロール/);
const source=readFileSync(new URL('../mobile/prediction-monitor.mjs',import.meta.url),'utf8');
assert.doesNotMatch(source,/setInterval|setTimeout|method:\s*['"]POST/);
assert.match(source,/常時通知ではありません/);
const shared=readFileSync(new URL('../mobile/prediction-verification.mjs',import.meta.url),'utf8');
assert.match(shared,/mountPredictionMonitor\(monitor, request\)/);
assert.match(readFileSync(new URL('../mobile/sw.js',import.meta.url),'utf8'),assetVersionPattern('prediction-monitor.mjs'));

// Exercise request failure, retry, double-tap guard and detached completion.
const nodes=Object.fromEntries(['body','refresh','message'].map(k=>[`[data-monitor-${k}]`,{}]));
const element={isConnected:true,querySelector:s=>nodes[s]};
let calls=0,finish;
await mountPredictionMonitor(element,async ()=>{calls++; throw new Error('<network>');});
assert.match(nodes['[data-monitor-message]'].textContent,/<network>/);
assert.equal(nodes['[data-monitor-refresh]'].disabled,false);
const mounting=mountPredictionMonitor(element,async path=>{calls++;assert.equal(path,'/api/predictions/monitor');return new Promise(r=>{finish=r;});});
await nodes['[data-monitor-refresh]'].onclick();
assert.equal(calls,2);
finish({items:[]}); await mounting;
assert.match(nodes['[data-monitor-body]'].innerHTML,/事前記録/);
const pending=mountPredictionMonitor(element,async ()=>new Promise(r=>{finish=r;}));
element.isConnected=false; nodes['[data-monitor-body]'].innerHTML='unchanged';
finish({items:[]}); await pending;
assert.equal(nodes['[data-monitor-body]'].innerHTML,'unchanged');
console.log('prediction monitor UI tests passed');
