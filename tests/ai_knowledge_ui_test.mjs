import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {assetVersionPattern} from './asset_version.mjs';
import {knowledgePayload,renderKnowledgeAnswer,mountKnowledge} from '../mobile/ai-knowledge.mjs';

const catalog={index_hash:'a'.repeat(64),counts:{glossary:8,machine:1,expectation:1,hall:1},machines:['機種A'],
  conditions:[{id:'ev:a',title:'機種A 等価',machine_name:'機種A'}]};
const context={hall_name:'店舗A',visit_date:'2026-09-19'};
const values={category:'glossary',question:'期待値とは',machine:'機種A',condition:'ev:a'};
assert.equal(knowledgePayload(catalog,context,values).machine_name,'');
assert.equal(knowledgePayload(catalog,context,values).condition_id,'');
assert.equal(knowledgePayload(catalog,context,{...values,category:'hall'}).hall_name,'店舗A');
assert.throws(()=>knowledgePayload(null,context,values),/資料一覧/);
assert.throws(()=>knowledgePayload(catalog,{...context,visit_date:''},values),/行く日/);
assert.throws(()=>knowledgePayload(catalog,context,{...values,question:' '}),/入力/);

const answer={knowledge_version:'1.0',index_hash:catalog.index_hash,contract_version:'1.0',engine:'専用知識検索',
  target_date:'2026-09-19',answer_status:'ai_disabled',notice:'引用・追加学習ではない',claims:[{evidence_id:'E001'}],
  evidence:[{id:'E001',kind:'knowledge_reference',knowledge:{document_id:'ev:a',revision:'1',title:'資料<img>',
    content:'<script>alert(1)</script>',conditions:{exchange_type:'equivalent'},source_locator:'data/catalog.json',
    reviewed_on:'2026-08-08',registered_on:'2026-09-19',verification:'再確認なし'},
    sources:[{url:'javascript:bad',label:'<img>'}],missing_information:['30日超']} ]};
const html=renderKnowledgeAnswer(answer);
assert.match(html,/交換条件：等価/);
assert.match(html,/確認日 2026-08-08/);
assert.match(html,/資料ID ev:a/);
assert.match(html,/30日超/);
assert.doesNotMatch(html,/<script>|<img>|href="javascript:/);
assert.match(renderKnowledgeAnswer({...answer,claims:[],evidence:[],summary:'条件を選んでください'}),/条件を選んでください/);

class Element{
  dataset={};events={};value='';disabled=false;hidden=false;innerHTML='';textContent='';
  addEventListener(type,fn){this.events[type]=fn;}
}
async function fixture(post){
  const root=new Element();
  const keys=['refresh','status','form','category','machine','condition','machine-label','condition-label','scope','question','search','result'];
  const e=Object.fromEntries(keys.map(k=>[k,new Element()]));
  e.category.value='glossary';e.question.value='期待値とは';
  root.querySelector=s=>e[s.match(/data-k="([^"]+)/)[1]];
  const externalContext=new Element(),calls=[];
  const state={context:{...context}};
  await mountKnowledge(root,{getContext:()=>state.context,contextElements:[externalContext],fetcher:async(url,opts)=>{
    calls.push({url,opts});
    if(opts?.method==='POST')return post?post(url,opts):{ok:true,json:async()=>answer};
    return {ok:true,json:async()=>catalog};
  }});
  return {e,calls,state,externalContext,submit:()=>e.form.events.submit({preventDefault(){}})};
}
const live=await fixture();
assert.equal(live.calls.length,1);assert.equal(live.calls[0].opts?.method,undefined,'mount loads only metadata');
await live.submit();
assert.match(live.e.result.innerHTML,/専用知識検索/);
assert.equal(live.e.search.disabled,false);
live.e.category.value='expectation';live.e.category.events.change();
assert.equal(live.e.condition.value,'');assert.equal(live.e['condition-label'].hidden,false);
assert.match(live.e.condition.innerHTML,/ev:a/);
live.e.condition.value='ev:a';live.e.machine.value='別機種';live.e.machine.events.change();
assert.equal(live.e.condition.value,'');assert.doesNotMatch(live.e.condition.innerHTML,/ev:a/);

let resolve;
const delayed=await fixture(()=>new Promise(done=>{resolve=done;}));
const request=delayed.submit();delayed.state.context.visit_date='2026-09-20';
resolve({ok:true,json:async()=>answer});await request;
assert.match(delayed.e.result.textContent,/古い検索結果/);
assert.equal(delayed.e.result.innerHTML,'');

const changed=delayed.submit();delayed.e.question.value='リセットとは';delayed.e.question.events.input();
resolve({ok:true,json:async()=>answer});await changed;
assert.equal(delayed.e.result.innerHTML,'');assert.equal(delayed.e.search.disabled,false);
delayed.externalContext.events.change();assert.equal(delayed.e.condition.value,'');

const stale=await fixture(async()=>({ok:false,status:409}));await stale.submit();
assert.match(stale.e.result.textContent,/資料の版が変わりました/);
assert.equal(stale.e.search.disabled,true);
await stale.e.refresh.events.click();assert.equal(stale.e.search.disabled,false);

for(const [base,id] of [['mobile','ai-knowledge-panel'],['web','desktop-ai-knowledge-panel']]){
  assert.ok(readFileSync(new URL(`../${base}/index.html`,import.meta.url),'utf8').includes(`id="${id}"`));
  const js=readFileSync(new URL(base==='mobile'?'../mobile/app.js':'../web/js/app.js',import.meta.url),'utf8');
  assert.match(js,assetVersionPattern('ai-knowledge.mjs'));
}
assert.match(readFileSync(new URL('../mobile/sw.js',import.meta.url),'utf8'),assetVersionPattern('ai-knowledge.mjs'));
console.log('AI knowledge UI tests passed');
