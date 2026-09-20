import assert from 'node:assert/strict';
import {comparisonPayload, renderComparisonControls, renderComparisonResult, mountAiComparison} from '../mobile/ai-comparison.mjs';

const context = {hall_name:'店舗A',visit_date:'2026-09-20',days:'90'};
const payload = comparisonPayload(context,['qwen','claude'],['machine','weekday'],'preview');
assert.equal(payload.confirm_external,false);
assert.equal(payload.days,90);
assert.throws(()=>comparisonPayload({...context,hall_name:'全店舗'},['qwen'],['machine'],'preview'),/1店/);
assert.throws(()=>comparisonPayload(context,[],['machine'],'preview'),/1社/);
assert.throws(()=>comparisonPayload(context,['qwen'],['machine'],'external',false),/料金/);

const controls = renderComparisonControls({external_comparison_enabled:false,notice:'<script>危険</script>',providers:[
  {provider:'qwen',label:'Qwen<img>',model:'model',reason:'無効',price_configured:false}],cases:[
  {id:'machine',label:'機種',question:'<b>質問</b>'}]});
assert.doesNotMatch(controls,/<script>|<img>|<b>質問/);
assert.match(controls,/外部AIで実測する/);

const html = renderComparisonResult({mode:'external',notice:'<script>危険</script>',case_definitions:[
  {label:'機種',evidence_count:2,has_evidence:true}],provider_results:[{label:'Qwen',model:'m',status:'measured',summary:{
  contract_pass_rate_pct:100,evidence_match_pct:80,average_latency_ms:120,reported_input_tokens:10,
  reported_output_tokens:3,estimated_cost_usd:0.001},cases:[]} ]});
assert.match(html,/契約適合：100%/);
assert.match(html,/勝者の自動決定なし/);
assert.doesNotMatch(html,/<script>/);
const rejected=renderComparisonResult({provider_results:[{cases:[{case_id:'machine',
  answer_guard:{reasons:['<img>店舗が違う']}}],summary:{guard_rejected_cases:1,abstained_cases:2}}]});
assert.match(rejected,/回答点検で不採用：1件/);
assert.match(rejected,/回答を控えた：2件/);
assert.doesNotMatch(rejected,/<img>/);

class Element {
  dataset={};events={};value='';checked=false;disabled=false;innerHTML='';textContent='';
  addEventListener(type,fn){this.events[type]=fn;}
}
function fixture(){
  const root=new Element(),controls=new Element(),result=new Element();
  const confirm=new Element(),preview=new Element(),external=new Element(),plan=new Element();
  const providers=['qwen','claude'].map(value=>Object.assign(new Element(),{value,checked:true}));
  const cases=['machine','weekday'].map(value=>Object.assign(new Element(),{value,checked:true}));
  root.querySelector=selector=>({'[data-controls]':controls,'[data-result]':result})[selector];
  controls.querySelector=selector=>({'[data-confirm]':confirm,'[data-preview]':preview,'[data-external]':external,'[data-plan]':plan})[selector];
  controls.querySelectorAll=selector=>{
    const items=selector.startsWith('[data-provider]') ? providers : selector.startsWith('[data-case]') ? cases : [];
    return selector.endsWith(':checked') ? items.filter(item=>item.checked) : items;
  };
  return {root,controls,result,confirm,preview,external,plan,providers,cases};
}
const readyStatus={external_comparison_enabled:true,
  providers:['qwen','claude'].map(provider=>({provider,label:provider,model:'test-model',ready:true,price_configured:true})),
  cases:[{id:'machine',label:'機種'},{id:'weekday',label:'曜日'}],
  governance:{providers:['qwen','claude'].map(provider=>({provider,ready_for_paid_calls:true,remaining_usd:1}))}};
const answer={mode:'external',provider_results:[],case_definitions:[]};
const response=(data,status=200)=>({ok:status>=200 && status<300,status,json:async()=>data});
function consent(ui){ui.confirm.checked=true;ui.confirm.events.change();}

// Opening only reads readiness; double-clicking during a request starts one comparison.
const ui=fixture(),posts=[];
let active={...context},resolvePost;
await mountAiComparison(ui.root,{getContext:()=>active,makeId:()=> 'comparison-id',fetcher:(url,options)=>{
  if(!options)return Promise.resolve(response(readyStatus));
  posts.push(JSON.parse(options.body));
  return new Promise(resolve=>{resolvePost=resolve;});
}});
assert.equal(posts.length,0);
assert.equal(ui.external.disabled,true);
consent(ui);
assert.equal(ui.external.disabled,false);
assert.match(ui.plan.textContent,/最大4回/);
const running=ui.external.events.click();
await ui.external.events.click();
await ui.preview.events.click();
assert.equal(posts.length,1,'busy guard blocks paid and preview starts');
assert.equal(ui.confirm.disabled,true);
assert.equal(ui.providers[0].disabled,true);
resolvePost(response(answer));await running;
assert.equal(ui.confirm.checked,false,'one successful run consumes confirmation');
assert.equal(ui.external.disabled,true);

// Selection changes clear consent; parent scope changes are checked again at execution.
consent(ui);ui.providers[1].checked=false;ui.providers[1].events.change();
assert.equal(ui.confirm.checked,false);
consent(ui);ui.cases[1].checked=false;ui.cases[1].events.change();
assert.equal(ui.confirm.checked,false);
assert.match(ui.plan.textContent,/最大1回/);
consent(ui);active={...context,visit_date:'2026-09-21'};
await ui.external.events.click();
assert.equal(posts.length,1);
assert.equal(ui.confirm.checked,false);
assert.match(ui.result.textContent,/この1回/);

// Uncertain starts keep provider/case conditions locked and reuse the complete payload.
for(const failure of ['transport','server']){
  const retry=fixture(),payloads=[];
  let ids=0,retryContext={...context};
  await mountAiComparison(retry.root,{getContext:()=>retryContext,makeId:()=>`request-${++ids}`,fetcher:async(url,options)=>{
    if(!options)return response(readyStatus);
    payloads.push(JSON.parse(options.body));
    if(payloads.length===1){
      if(failure==='transport')throw new Error('lost response');
      return response({detail:'server error'},500);
    }
    return response(answer);
  }});
  consent(retry);await retry.external.events.click();
  assert.match(retry.result.textContent,/同じ実行ID/);
  assert.equal(retry.external.disabled,false);
  assert.equal(retry.preview.disabled,true);
  assert.equal(retry.confirm.disabled,true);
  assert.equal(retry.cases[0].disabled,true);
  await retry.preview.events.click();
  assert.equal(payloads.length,1,'pending paid requests cannot switch to preview');
  retryContext={...context,hall_name:'店舗B'};
  await retry.external.events.click();
  assert.deepEqual(payloads[0],payloads[1],`${failure} retry must preserve the request ID and payload`);
  assert.equal(ids,1);
  assert.equal(retry.confirm.checked,false);
  assert.match(retry.result.textContent,/古い比較結果/);
}

// Paid execution fails closed when any selected provider has no remaining budget.
const blocked=fixture();let blockedPosts=0;
await mountAiComparison(blocked.root,{getContext:()=>context,fetcher:async(url,options)=>{
  if(options)blockedPosts++;
  return response({...readyStatus,governance:{providers:readyStatus.governance.providers.map(item=>({...item,remaining_usd:0}))}});
}});
consent(blocked);assert.equal(blocked.external.disabled,true);
await blocked.external.events.click();
assert.equal(blockedPosts,0);

const rejectedUi=fixture();let rejectedPosts=0;
await mountAiComparison(rejectedUi.root,{getContext:()=>context,makeId:()=> 'rejected-id',fetcher:async(url,options)=>{
  if(!options)return response(readyStatus);
  rejectedPosts++;return response({detail:'予算が不足しています。'},422);
}});
consent(rejectedUi);await rejectedUi.external.events.click();
assert.equal(rejectedPosts,1);assert.equal(rejectedUi.confirm.checked,false);
assert.equal(rejectedUi.providers[0].disabled,false);assert.equal(rejectedUi.external.disabled,true);
assert.match(rejectedUi.result.textContent,/予算が不足/);

console.log('AI comparison UI tests passed');
