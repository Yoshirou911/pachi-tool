import assert from 'node:assert/strict';
import {hallQuestionPayload, renderHallAnswer, mountHallAi} from '../mobile/hall-ai.mjs';

const context = {hall_name:'店舗A',visit_date:'2026-09-20',days:'90'};
assert.equal(hallQuestionPayload(context, '曜日は？', 'weekday', '').days, 90);
assert.throws(() => hallQuestionPayload({...context,hall_name:'全店舗'},'','',''), /1店/);
assert.throws(() => hallQuestionPayload({...context,visit_date:''},'','',''), /行く日/);
const html = renderHallAnswer({scope:'<img onerror=alert(1)>',target_date:'2026-09-20',
  summary:'<script>危険</script>',topic_labels:['曜日'],contract_version:'1.0',evidence:[]});
assert.match(html, /根拠・出典を見る/);
assert.doesNotMatch(html, /<script>|<img /);

class Element {
  dataset = {};
  events = {};
  value = '';
  innerHTML = '';
  textContent = '';
  disabled = false;
  checked = false;
  hidden = false;
  addEventListener(name, fn) { this.events[name] = fn; }
}
function fixture() {
  const form = new Element();
  form.requestSubmit = () => form.events.submit({preventDefault(){}});
  const input = new Element();
  const topic = new Element(); topic.value = 'auto';
  const machine = new Element();
  const result = new Element();
  const send = new Element();
  const useExternal = new Element(), confirm = new Element(), externalConfirm = new Element(), budget = new Element();
  const chip = new Element(); chip.dataset = {topic:'machine',question:'どの機種を強く扱っている？'};
  const root = new Element();
  root.querySelector = selector => ({form,'[data-question-input]':input,'[data-topic-select]':topic,
    '[data-machine]':machine,'[data-result]':result,'[data-send]':send,
    '[data-use-external]':useExternal,'[data-confirm]':confirm,
    '[data-external-confirm]':externalConfirm,'[data-budget]':budget})[selector];
  root.querySelectorAll = selector => selector === 'button' ? [send,chip] : [chip];
  return {root,form,input,topic,machine,result,send,chip,useExternal,confirm,externalConfirm,budget};
}

// No request on mounting, and PC/mobile use the caller's currently selected scope.
const ui = fixture();
let active = {...context};
let respond;
const calls = [];
mountHallAi(ui.root, {getContext:()=>active,apiUrl:path=>'https://local.example'+path,
  fetcher: (url, options) => { calls.push({url,payload:JSON.parse(options.body)}); return new Promise(resolve => {respond=resolve;}); }});
assert.equal(calls.length,0);
const work = ui.form.requestSubmit();
await ui.form.requestSubmit();
assert.equal(calls.length,1,'double submission must not issue another request');
assert.equal(calls[0].payload.hall_name,'店舗A');
assert.equal(calls[0].payload.visit_date,'2026-09-20');
assert.equal(ui.send.disabled,true);
active = {...context,hall_name:'店舗B'};
respond({ok:true,json:async()=>({summary:'古い店舗の回答'})});
await work;
assert.match(ui.result.textContent,/古い回答は表示しません/);
assert.doesNotMatch(ui.result.innerHTML,/古い店舗の回答/);
assert.equal(ui.send.disabled,false);

// Changing a machine while waiting also invalidates the reply.
active = {...context};
const second = ui.form.requestSubmit();
ui.machine.value = '新しい機種';
respond({ok:true,json:async()=>({summary:'古い機種'})});
await second;
assert.match(ui.result.textContent,/質問条件が変更/);
assert.equal(ui.send.disabled,false);

// Quick buttons select the proper topic and use the same endpoint.
ui.machine.value = '';
ui.chip.events.click();
assert.equal(calls.at(-1).payload.topic,'machine');
assert.match(calls.at(-1).url,/\/api\/ai\/hall_ask$/);
respond({ok:true,json:async()=>({summary:'同日比較の回答',scope:'店舗A',target_date:'2026-09-20',
  topic_labels:['機種'],contract_version:'1.0',evidence:[]})});
await new Promise(resolve => setTimeout(resolve,0));
assert.match(ui.result.innerHTML,/同日比較の回答/);
assert.equal(ui.send.disabled,false);

const readyStatus={available:true,provider:'qwen',provider_label:'Qwen',model:'test-model',
  governance:{providers:[{provider:'qwen',ready_for_paid_calls:true,remaining_usd:1,monthly_limit_usd:2,month_reserved_usd:1}]}};
const answer={summary:'確認済み',scope:context.hall_name,target_date:context.visit_date,evidence:[]};
const response=(data,status=200)=>({ok:status>=200 && status<300,status,json:async()=>data});
async function enableExternal(target){target.useExternal.checked=true;await target.useExternal.events.change();}
function consent(target){target.confirm.checked=true;target.confirm.events.change();}

// Explicit consent is tied to the current question and caller-owned context.
const externalUi=fixture(),externalCalls=[];
let externalContext={...context};
mountHallAi(externalUi.root,{getContext:()=>externalContext,makeId:()=> 'external-id',fetcher:async(url,options)=>{
  externalCalls.push({url,options});
  return response(options ? answer : readyStatus);
}});
assert.equal(externalCalls.length,0,'mount must not even fetch external status');
await enableExternal(externalUi);
assert.equal(externalUi.externalConfirm.hidden,false);
assert.equal(externalUi.send.disabled,true,'external calls require one-shot confirmation');
consent(externalUi);
assert.equal(externalUi.send.disabled,false);
externalUi.chip.events.click();
assert.equal(externalCalls.filter(call=>call.options).length,0,'external quick chips must never submit');
assert.equal(externalUi.confirm.checked,false,'quick chips change the consented question');
consent(externalUi);
externalUi.input.value='別の質問';externalUi.input.events.input();
assert.equal(externalUi.confirm.checked,false);
consent(externalUi);
externalContext={...context,days:'30'};
await externalUi.form.requestSubmit();
assert.equal(externalCalls.filter(call=>call.options).length,0,'a changed parent context invalidates consent');
assert.equal(externalUi.confirm.checked,false);
consent(externalUi);
const paidWork=externalUi.form.requestSubmit();
await externalUi.form.requestSubmit();
await paidWork;
assert.equal(externalCalls.filter(call=>call.options).length,1,'paid double submission starts one request');
const paidPayload=JSON.parse(externalCalls.find(call=>call.options).options.body);
assert.equal(paidPayload.use_external_ai,true);
assert.equal(paidPayload.confirm_public_data_only,true);
assert.equal(paidPayload.days,30);
assert.equal(externalUi.confirm.checked,false,'success consumes confirmation');
assert.equal(externalUi.useExternal.checked,false,'external execution is opt-in for each question');

// Both a lost response and HTTP 500 keep the exact original request, even if the parent scope changes.
for(const failure of ['transport','server']){
  const retryUi=fixture(),payloads=[];
  let ids=0,retryContext={...context};
  mountHallAi(retryUi.root,{getContext:()=>retryContext,makeId:()=>`request-${++ids}`,fetcher:async(url,options)=>{
    if(!options)return response(readyStatus);
    payloads.push(JSON.parse(options.body));
    if(payloads.length===1){
      if(failure==='transport')throw new Error('lost response');
      return response({detail:'server error'},500);
    }
    return response(answer);
  }});
  await enableExternal(retryUi);consent(retryUi);
  await retryUi.form.requestSubmit();
  assert.match(retryUi.result.textContent,/同じ実行ID/);
  assert.equal(retryUi.send.disabled,false);
  for(const field of ['input','topic','machine','useExternal','confirm','chip'])assert.equal(retryUi[field].disabled,true);
  retryContext={...context,hall_name:'店舗B'};
  retryUi.chip.events.click();
  assert.equal(payloads.length,1,'chips cannot change an uncertain request');
  await retryUi.form.requestSubmit();
  assert.deepEqual(payloads[0],payloads[1],`${failure} retries preserve the entire payload`);
  assert.equal(ids,1,'retry must not generate a new request ID');
  assert.equal(retryUi.confirm.checked,false);
  assert.match(retryUi.result.textContent,/古い回答/);
}

// Missing budget capacity cannot be bypassed by firing the form handler directly.
const blockedUi=fixture();let blockedPosts=0;
mountHallAi(blockedUi.root,{getContext:()=>context,fetcher:async(url,options)=>{
  if(options)blockedPosts++;
  return response({...readyStatus,governance:{providers:[{provider:'qwen',ready_for_paid_calls:true,remaining_usd:0}]}});
}});
await enableExternal(blockedUi);consent(blockedUi);
assert.equal(blockedUi.send.disabled,true);
await blockedUi.form.requestSubmit();
assert.equal(blockedPosts,0);

// A definitive rejection unlocks inputs but does not carry consent into another paid request.
const rejectedUi=fixture();let rejectedPosts=0;
mountHallAi(rejectedUi.root,{getContext:()=>context,makeId:()=> 'rejected-id',fetcher:async(url,options)=>{
  if(!options)return response(readyStatus);
  rejectedPosts++;return response({detail:'予算が不足しています。'},422);
}});
await enableExternal(rejectedUi);consent(rejectedUi);await rejectedUi.form.requestSubmit();
assert.equal(rejectedPosts,1);assert.equal(rejectedUi.confirm.checked,false);
assert.equal(rejectedUi.useExternal.checked,false);assert.equal(rejectedUi.input.disabled,false);
assert.match(rejectedUi.result.textContent,/予算が不足/);
console.log('Hall AI UI tests passed');
