import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {assetVersionPattern} from './asset_version.mjs';
import {buildImageMapDraft,buildImageRecord,buildReviewedPayload,mountImageAnalysis,renderImageHistory,uniqueSeatNumbers} from '../mobile/image-analysis.mjs';

assert.deepEqual(uniqueSeatNumbers('５０１, 508\n509'),[501,508,509]);
for(const invalid of ['501 508 501','501 999999','501a','50.1','50/1','-5','0'])assert.throws(()=>uniqueSeatNumbers(invalid));
assert.deepEqual(buildReviewedPayload('floor_map',{seatNumbers:'501 508'}).seat_numbers,[501,508]);
assert.throws(()=>buildReviewedPayload('unknown',{}),/種類/);
assert.throws(()=>buildReviewedPayload('data_lamp',{}),/1つ以上/);
assert.throws(()=>buildReviewedPayload('store_material',{text:'  '}),/確認/);
const analysis={method:'text-detector',image:{sha256:'a'.repeat(64),mime:'image/jpeg',width:100,height:80,size:123},
  candidates:[{text:'<script>650</script>',confidence:.5,kind:'number'}]};
assert.throws(()=>buildImageRecord({analysis,kind:'data_lamp',values:{gameCount:650}}),/照合/);
const record=buildImageRecord({analysis,kind:'data_lamp',values:{gameCount:'650'},confirmed:true,saveOriginal:true});
assert.equal(record.reviewed.game_count,650);
assert.equal(record.save_original_on_device,true);
assert.equal('previewUrl' in record,false);
assert.equal('extracted' in record,false,'unreviewed OCR candidates must stay in the browser');
assert.equal(JSON.stringify(record).includes('data:image'),false);
const privateAnalysis={...analysis,candidates:[
  {text:'incorrect-private-ocr-candidate',confidence:.5,kind:'text'},
  {text:'deleted-private-ocr-candidate',confidence:.5,kind:'text'},
]};
const corrected=buildImageRecord({analysis:privateAnalysis,kind:'store_material',values:{text:'確認して訂正した文面'},confirmed:true});
assert.equal(corrected.reviewed.text,'確認して訂正した文面');
assert.equal('extracted' in corrected,false);
assert.equal(JSON.stringify(corrected).includes('private-ocr-candidate'),false);
const history=renderImageHistory([{kind:'data_lamp',hall_name:'<img src=x>',created_at:'2026-09-19',verification_status:'確認済み入力',image:{device_copy_requested:false}}]);
assert.doesNotMatch(history,/<img /);

class Element{
  value='';checked=false;disabled=false;innerHTML='';textContent='';files=[];events={};
  addEventListener(type,fn){this.events[type]=fn;}
}
const root=new Element();
const names=['privacy','form','kind','file','analyze','preview','editor','confirm','original','save','apply','message','history'];
const elements=Object.fromEntries(names.map(name=>[name,new Element()]));
elements.kind.value='data_lamp';
root.querySelector=selector=>{
  const match=selector.match(/data-image-el="([^"]+)/);
  return match?elements[match[1]]:null;
};
const calls=[];
await mountImageAnalysis(root,{fetcher:async(url,options)=>{
  calls.push({url,options});
  return {ok:true,json:async()=>url.includes('/status')?{external_image_ai_enabled:false}:[]};
}});
assert.equal(calls.filter(call=>call.options?.method==='POST').length,0,'opening must never upload or save');
assert.equal(elements.privacy.textContent,'画像送信なし');
assert.equal(elements.save.disabled,true);

// Reviewed image records must not overwrite live fields merely by being saved.
async function fixture(options={}){
  const root=new Element();
  const e=Object.fromEntries(names.map(name=>[name,new Element()]));
  e.kind.value='data_lamp';
  const fields={'game-count':{value:'650'},'seat-numbers':{value:'501 508'}};
  root.querySelector=selector=>{
    const name=selector.match(/data-image-el="([^"]+)/)?.[1];
    return name?e[name]:fields[selector.match(/data-image-field="([^"]+)/)?.[1]];
  };
  const state={context:{hallName:'店舗A',observedOn:'2026-09-19'},posts:[],lamp:[],map:[]};
  const fetcher=async(url,opts)=>{
    if(opts?.method==='POST'){
      const payload=JSON.parse(opts.body);state.posts.push(payload);
      if(options.onPost)await options.onPost();
      return {ok:true,json:async()=>({...payload,id:'image-1',created_at:'2026-09-19T00:00:00Z'})};
    }
    return {ok:true,json:async()=>url.endsWith('/status')?{external_image_ai_enabled:false}:[]};
  };
  await mountImageAnalysis(root,{fetcher,analyzer:options.analyzer||(async()=>({...analysis,previewUrl:'data:image/jpeg;base64,YQ=='})),
    getContext:()=>state.context,onApplyDataLamp:v=>state.lamp.push(v),onApplyMap:(n,r)=>state.map.push(buildImageMapDraft(n,r))});
  const prepare=async()=>{e.file.files=[{name:'one.jpg'}];await e.analyze.events.click();e.confirm.checked=true;e.confirm.events.change();};
  const submit=()=>e.form.events.submit({preventDefault(){}});
  return {e,state,prepare,submit,fields};
}
const live=await fixture();
await live.prepare();await live.submit();
assert.equal(live.state.posts.length,1);
assert.equal('extracted' in live.state.posts[0],false,'submitted records must omit raw candidates');
assert.equal(live.state.lamp.length,0);
assert.equal(live.e.apply.disabled,false);
await live.submit();assert.equal(live.state.posts.length,1,'saved unchanged fields must not save twice');
live.e.apply.events.click();assert.equal(live.state.lamp[0].game_count,650);
live.e.apply.events.click();assert.equal(live.state.lamp.length,1);

const edited=await fixture();await edited.prepare();
edited.e.editor.events.input();await edited.submit();
assert.equal(edited.state.posts.length,0,'editing requires renewed visual confirmation');
edited.e.confirm.checked=true;await edited.submit();
edited.state.context.hallName='店舗B';edited.e.apply.events.click();
assert.equal(edited.state.lamp.length,0,'saved values must not apply in changed context');

const changed=await fixture();await changed.prepare();changed.state.context.observedOn='2026-09-20';
await changed.submit();assert.equal(changed.state.posts.length,0);

let finishRead;
const race=await fixture({analyzer:()=>new Promise(resolve=>{finishRead=resolve;})});
race.e.file.files=[{}];const reading=race.e.analyze.events.click();
race.e.file.events.change();finishRead(analysis);await reading;
assert.equal(race.e.editor.innerHTML,'');assert.equal(race.e.save.disabled,true);

let finishPost;
const network=await fixture({onPost:()=>new Promise(resolve=>{finishPost=resolve;})});
await network.prepare();const saving=network.submit();await network.submit();
assert.equal(network.state.posts.length,1,'double submit during network delay is ignored');
network.state.context.hallName='店舗B';finishPost();await saving;
assert.equal(network.state.lamp.length,0);assert.equal(network.e.apply.disabled,true);
assert.match(network.e.message.textContent,/反映は止めました/);

const map=await fixture();map.e.kind.value='floor_map';await map.prepare();await map.submit();
assert.equal(map.state.map.length,0);map.e.apply.events.click();
assert.equal(map.state.map[0].layout.verification_status,'未確認');
assert.notEqual(map.state.map[0].layout.floor_name,'スロットフロア');
assert.equal(map.state.map[0].layout.source_url,'');
const large=buildImageMapDraft(Array.from({length:1000},(_,i)=>i+1),{id:'large',hall_name:'店舗A',observed_on:'2026-09-19'});
assert.ok(large.layout.height<=5000&&large.layout.width<=5000);
assert.ok(large.seats.every(s=>s.x+s.width<=large.layout.width&&s.y+s.height<=large.layout.height&&!s.island_name&&!s.row_name));
assert.throws(()=>buildImageMapDraft([501],{id:'x',hall_name:''}),/店舗/);

for(const [base,id] of [['mobile','image-analysis-panel'],['web','desktop-image-analysis-panel']]){
  const html=readFileSync(new URL(`../${base}/index.html`,import.meta.url),'utf8');
  assert.ok(html.includes(`id="${id}"`));
  const js=readFileSync(new URL(base==='mobile'?'../mobile/app.js':'../web/js/app.js',import.meta.url),'utf8');
  assert.match(js,assetVersionPattern('image-analysis.mjs'));
}
assert.match(readFileSync(new URL('../mobile/sw.js',import.meta.url),'utf8'),assetVersionPattern('image-analysis.mjs'));
console.log('Image analysis UI tests passed');
