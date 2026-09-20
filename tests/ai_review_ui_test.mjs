import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {assetVersionPattern} from './asset_version.mjs';
import {renderAiReview,mountAiReview} from '../mobile/ai-review.mjs';

const review={app_version:'3.49.0',overall_label:'用途限定で採用',notice:'予測的中率ではない',
  active_explanation:'無料の統計説明',adopted_external_provider:null,
  verification:{valid:true,reason:'現在のコードと一致',verified_at:'2026-09-20',python_passed:10,ui_passed:2},
  features:[{label:'<img onerror=bad>',status:'assistance_only',status_label:'補助限定',scope:'元資料の照合が必要',next_step:'実機確認'}],
  providers:[{label:'Qwen',status_label:'保留',external_performance:'未審査',monthly_limit_usd:1,
    month_reserved_usd:0,remaining_usd:1,price_configured:true}],
  switch_policy:['外部AIを自動採用しない'],unverified:['実iPhoneの操作']};
const html=renderAiReview(review);
assert.match(html,/採用済み外部AI：なし/);
assert.match(html,/補助限定/);assert.match(html,/未審査/);
assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);
const stale=renderAiReview({...review,verification:{valid:false,reason:'再点検',changed_files:['<script>']}});
assert.doesNotMatch(stale,/Python 10件|<script>/);assert.match(stale,/再点検/);

class Element{
  dataset={};events={};disabled=false;value='';_html='';_text='';
  set innerHTML(v){this._html=v;this._text='';} get innerHTML(){return this._html;}
  set textContent(v){this._text=v;this._html='';} get textContent(){return this._text;}
  addEventListener(type,fn){this.events[type]=fn;}
}
const root=new Element(),button=new Element(),result=new Element(),calls=[];
root.querySelector=selector=>selector==='[data-refresh]'?button:result;
let next=async()=>({ok:true,json:async()=>review});
await mountAiReview(root,{apiUrl:path=>'https://api.example'+path,fetcher:async(url,options)=>{
  calls.push({url,options});return next();
}});
assert.equal(calls.length,1);assert.equal(calls[0].url,'https://api.example/api/ai/review');
assert.deepEqual(calls[0].options,{cache:'no-store'});
assert.match(result.innerHTML,/用途限定/);
await mountAiReview(root,{fetcher:()=>{throw Error('must not mount twice');}});
let resolve;
next=()=>new Promise(done=>{resolve=done;});
const waiting=button.events.click();
await button.events.click();
assert.equal(calls.length,2);assert.equal(button.disabled,true);
assert.equal(result.innerHTML,'','old approval cleared while refreshing');
resolve({ok:false,status:503});await waiting;
assert.equal(button.disabled,false);assert.match(result.textContent,/取得できません/);
assert.equal(result.innerHTML,'','failed refresh must not leave a prior approval');
next=async()=>{throw new Error('offline');};
await button.events.click();assert.equal(result.textContent,'offline');
assert.ok(calls.every(call=>!call.options.method),'read-only GETs only');

for(const [base,id] of [['mobile','mobile-ai-review-panel'],['web','desktop-ai-review-panel']]){
  const index=readFileSync(new URL(`../${base}/index.html`,import.meta.url),'utf8');
  assert.ok(index.includes(`id="${id}"`));
  const source=readFileSync(new URL(base==='mobile'?'../mobile/app.js':'../web/js/app.js',import.meta.url),'utf8');
  assert.match(source,assetVersionPattern('ai-review.mjs'));
}
assert.match(readFileSync(new URL('../mobile/sw.js',import.meta.url),'utf8'),assetVersionPattern('ai-review.mjs'));
console.log('AI adoption review UI tests passed');
