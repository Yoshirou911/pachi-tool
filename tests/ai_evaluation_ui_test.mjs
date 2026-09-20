import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {assetVersionPattern} from './asset_version.mjs';
import {evaluationPayload,renderEvaluationReport,mountAiEvaluation} from '../mobile/ai-evaluation.mjs';

const catalog={suite_hash:'a'.repeat(64),suite_version:'1.0.0',cases:[{id:'one'}],runs:[],connection:{providers:[]},notice:'架空問題'};
assert.equal(evaluationPayload(catalog,'',2,false,'id').mode,'offline');
assert.throws(()=>evaluationPayload(catalog,'qwen',2,false,'id'),/確認/);
assert.throws(()=>evaluationPayload(catalog,'',0,false,'id'),/反復/);
const completed={id:'run-1',label:'ローカル参考方式（AI未使用）',mode:'offline',status:'completed',repeats:2,
  suite_version:'1.0.0',summary:{completed:2,total:2,exact_count:1,exact_match_pct:50,external_calls:0,consistency_pairs:1,planned_pairs:1},
  cases:[],samples:[],notice:'検証問題'};
const html=renderEvaluationReport({...completed,label:'<img src=x onerror=alert(1)>',notice:'<script>危険</script>',
  cases:[{id:'one',category:'機種',question:'質問',expected_ids:['E002'],reason:'理由',snapshot:{},excluded_inputs:[]}],
  samples:[{case_id:'one',attempt:1,schema_valid:false,error_kind:'<script>秘密</script>',selected_ids:[]} ]});
assert.doesNotMatch(html,/<script>|<img /);
assert.match(html,/外部AIの実力は未計測/);
assert.match(html,/費用概算：未計測/);
assert.match(html,/50%/);

class Element {
  dataset={}; events={}; value=''; checked=false; disabled=false; hidden=false; innerHTML=''; textContent='';
  addEventListener(type,fn){this.events[type]=fn;}
  querySelectorAll(){return [];}
}
function fixture(){
  const root=new Element();
  const elements=Object.fromEntries(['refresh','info','eval-form','provider','repeats','confirm','consent','plan','run','message','history','report'].map(k=>[k,new Element()]));
  elements.repeats.value='2';
  root.querySelector=selector=>elements[selector.slice(6,-1)];
  return {root,...elements};
}
const ui=fixture(),calls=[];
let posts=0,ids=0;
const fetcher=async(url,options)=>{
  calls.push({url,options});
  if(options?.method==='POST'){
    posts++;
    if(posts===1) throw new Error('lost response');
    return {ok:true,json:async()=>completed};
  }
  return {ok:true,json:async()=>url.endsWith('/runs/run-1') ? completed : catalog};
};
await mountAiEvaluation(ui.root,{fetcher,makeId:()=> `request-${++ids}`});
assert.equal(posts,0,'opening the panel must not start an evaluation');
assert.equal(calls.length,1);
const submit=()=>ui['eval-form'].events.submit({preventDefault(){}});
await submit();
assert.match(ui.message.textContent,/同じ実行ID/);
assert.equal(ui.provider.disabled,true);
assert.equal(ui.run.disabled,false);
await submit();
assert.equal(posts,2);
const payloads=calls.filter(c=>c.options?.method==='POST').map(c=>JSON.parse(c.options.body));
assert.deepEqual(payloads[0],payloads[1],'uncertain retries must reuse the same request ID and payload');
assert.equal(ids,1,'a retry must not allocate another request ID');
assert.equal(payloads[1].mode,'offline');
assert.match(ui.report.innerHTML,/保存済みの最終結果/);
assert.equal(ui.provider.disabled,false);

// Resume progress from the server without repeating the start request.
const second=fixture();
const callbacks=[];
let reportCalls=0;
await mountAiEvaluation(second.root,{schedule:fn=>{callbacks.push(fn);return 1;},cancel:()=>{},fetcher:async(url,options)=>{
  assert.equal(options,undefined);
  if(url.endsWith('/evaluation'))return {ok:true,json:async()=>({...catalog,active_run_id:'run-1'})};
  reportCalls++;
  return {ok:true,json:async()=>reportCalls===1 ? {...completed,status:'running',progress_pct:50,summary:{...completed.summary,completed:1}} : completed};
}});
assert.match(second.message.textContent,/1\/2回/);
assert.equal(second.run.disabled,true);
await callbacks[0]();
assert.equal(second.run.disabled,false);
assert.match(second.report.innerHTML,/保存済み/);

const readyCatalog={...catalog,connection:{providers:[{provider:'qwen',label:'Qwen',model:'test-model',ready:true}],
  governance:{providers:[{provider:'qwen',ready_for_paid_calls:true,remaining_usd:1,monthly_limit_usd:2,month_reserved_usd:1}]}}};
const response=(data,status=200)=>({ok:status>=200 && status<300,status,json:async()=>data});
function consent(target){target.confirm.checked=true;target.confirm.events.change();}
function selectExternal(target){target.provider.value='qwen';target.provider.events.change();}
const submitUi=target=>target['eval-form'].events.submit({preventDefault(){}});

// Selection changes invalidate one-shot consent, and a double submit starts one run.
const paid=fixture(),paidPosts=[];
let resolvePaid;
await mountAiEvaluation(paid.root,{makeId:()=> 'paid-id',fetcher:(url,options)=>{
  if(options){paidPosts.push(JSON.parse(options.body));return new Promise(resolve=>{resolvePaid=resolve;});}
  return Promise.resolve(response(url.endsWith('/runs/run-1') ? {...completed,mode:'external'} : readyCatalog));
}});
assert.equal(paidPosts.length,0,'mounting never starts an evaluation');
selectExternal(paid);assert.equal(paid.consent.hidden,false);assert.equal(paid.run.disabled,true);
consent(paid);assert.equal(paid.run.disabled,false);
paid.repeats.value='3';paid.repeats.events.change();
assert.equal(paid.confirm.checked,false);assert.equal(paid.run.disabled,true);
consent(paid);paid.provider.events.change();
assert.equal(paid.confirm.checked,false,'provider changes invalidate confirmation');
consent(paid);
// Even programmatic changes without an event cannot reuse stale consent.
paid.repeats.value='1';await submitUi(paid);
assert.equal(paidPosts.length,0);assert.equal(paid.confirm.checked,false);
consent(paid);
const paidWork=submitUi(paid);
await submitUi(paid);
assert.equal(paidPosts.length,1,'the busy guard rejects duplicate submits');
assert.equal(paid.provider.disabled,true);assert.equal(paid.repeats.disabled,true);assert.equal(paid.confirm.disabled,true);
assert.equal(paidPosts[0].confirm_public_data_only,true);
resolvePaid(response({...completed,mode:'external'}));await paidWork;
assert.equal(paid.confirm.checked,false,'successful starts consume consent');
assert.equal(paid.run.disabled,true,'another paid run requires fresh consent');
consent(paid);await paid.refresh.events.click();
assert.equal(paid.confirm.checked,false,'refreshing the catalog invalidates earlier consent');

// HTTP 500 is ambiguous just like a lost transport response; preserve all start conditions.
const retry=fixture(),retryPayloads=[];let retryIds=0;
await mountAiEvaluation(retry.root,{makeId:()=>`retry-${++retryIds}`,fetcher:async(url,options)=>{
  if(!options)return response(url.endsWith('/runs/run-1') ? {...completed,mode:'external'} : readyCatalog);
  retryPayloads.push(JSON.parse(options.body));
  return retryPayloads.length===1 ? response({detail:'server failure'},500) : response({...completed,mode:'external'});
}});
selectExternal(retry);consent(retry);await submitUi(retry);
assert.match(retry.message.textContent,/同じ実行ID/);
assert.equal(retry.run.disabled,false);
assert.equal(retry.provider.disabled,true);assert.equal(retry.repeats.disabled,true);
await submitUi(retry);
assert.deepEqual(retryPayloads[0],retryPayloads[1]);assert.equal(retryIds,1);
assert.equal(retry.confirm.checked,false);

// Definitive client rejection also consumes consent and unlocks the conditions.
const rejectedUi=fixture();let rejectedPosts=0;
await mountAiEvaluation(rejectedUi.root,{makeId:()=> 'rejected-id',fetcher:async(url,options)=>{
  if(!options)return response(readyCatalog);
  rejectedPosts++;return response({detail:'予算が不足しています。'},422);
}});
selectExternal(rejectedUi);consent(rejectedUi);await submitUi(rejectedUi);
assert.equal(rejectedPosts,1);assert.equal(rejectedUi.confirm.checked,false);
assert.equal(rejectedUi.provider.disabled,false);assert.equal(rejectedUi.run.disabled,true);
assert.match(rejectedUi.message.textContent,/予算が不足/);

// A missing or exhausted budget is blocked even when the handler is invoked directly.
const blocked=fixture();let blockedPosts=0;
await mountAiEvaluation(blocked.root,{fetcher:async(url,options)=>{
  if(options)blockedPosts++;
  return response({...readyCatalog,connection:{...readyCatalog.connection,governance:{providers:[]}}});
}});
selectExternal(blocked);consent(blocked);
assert.equal(blocked.run.disabled,true);await submitUi(blocked);assert.equal(blockedPosts,0);

for(const [base,id] of [['mobile','ai-evaluation-panel'],['web','desktop-ai-evaluation-panel']]){
  const html=readFileSync(new URL(`../${base}/index.html`,import.meta.url),'utf8');
  assert.ok(html.includes(`id="${id}"`));
  const js=readFileSync(new URL(base==='mobile'?'../mobile/app.js':'../web/js/app.js',import.meta.url),'utf8');
  assert.match(js,assetVersionPattern('ai-evaluation.mjs'));
}
assert.match(readFileSync(new URL('../mobile/sw.js',import.meta.url),'utf8'),assetVersionPattern('ai-evaluation.mjs'));
console.log('AI evaluation UI interaction tests passed');
