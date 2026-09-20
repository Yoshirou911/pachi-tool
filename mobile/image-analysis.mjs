import {recognizeNumberFromFile} from './ocr.mjs?v=3.49.0';

const KIND_LABELS={data_lamp:'データランプ',store_material:'店舗資料',floor_map:'ホールマップ'};
const ALLOWED_MIME=new Set(['image/jpeg','image/png','image/webp']);

function escapeHtml(value){return String(value??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
function clamp(value){return Math.max(0,Math.min(1,Number(value)||0));}
function integer(value,min,max){
  if(value===''||value===null||value===undefined)return null;
  const parsed=Number(value);
  if(!Number.isInteger(parsed)||parsed<min||parsed>max)throw new Error(`${min}〜${max}の整数で確認してください`);
  return parsed;
}
export function uniqueSeatNumbers(value){
  const parts=String(value||'').normalize('NFKC').trim().split(/[\s,、]+/).filter(Boolean);
  if(parts.length>1000||parts.some(n=>!/^\d{1,5}$/.test(n)||Number(n)<1))throw new Error('台番号を1〜99999の整数で区切って入力してください');
  const numbers=parts.map(Number);
  if(new Set(numbers).size!==numbers.length)throw new Error('台番号が重複しています');
  return numbers;
}
function numbersFromText(text){return String(text||'').normalize('NFKC').match(/\d+/g)?.filter(n=>n.length<=6)||[];}

async function sha256(file){
  const bytes=await file.arrayBuffer();
  const digest=await crypto.subtle.digest('SHA-256',bytes);
  return [...new Uint8Array(digest)].map(value=>value.toString(16).padStart(2,'0')).join('');
}

async function decodeImage(file){
  if(typeof globalThis.createImageBitmap==='function'){
    const bitmap=await globalThis.createImageBitmap(file);
    return {source:bitmap,width:bitmap.width,height:bitmap.height,close:()=>bitmap.close?.()};
  }
  if(typeof globalThis.Image!=='function')throw new Error('このブラウザでは画像を開けません');
  const url=URL.createObjectURL(file);
  try{
    const image=new globalThis.Image();
    await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(new Error('画像を開けませんでした'));image.src=url;});
    return {source:image,width:image.naturalWidth,height:image.naturalHeight,close:()=>URL.revokeObjectURL(url)};
  }catch(error){URL.revokeObjectURL(url);throw error;}
}

export async function analyzeImageFile(file,{documentRef=document,textDetector=globalThis.TextDetector}={}){
  if(!file)throw new Error('画像を選んでください');
  if(!ALLOWED_MIME.has(file.type))throw new Error('JPEG・PNG・WebPの画像を選んでください');
  if(file.size>20_000_000)throw new Error('画像は20MB以下にしてください');
  const decoded=await decodeImage(file);
  const original={width:decoded.width,height:decoded.height};
  const scale=Math.min(1,1600/Math.max(decoded.width,decoded.height));
  const canvas=documentRef.createElement('canvas');
  canvas.width=Math.max(1,Math.round(decoded.width*scale));
  canvas.height=Math.max(1,Math.round(decoded.height*scale));
  canvas.getContext('2d',{willReadFrequently:true}).drawImage(decoded.source,0,0,canvas.width,canvas.height);
  decoded.close();
  let candidates=[];
  let method='manual-review';
  if(textDetector){
    try{
      const blocks=await new textDetector().detect(canvas);
      candidates=blocks.map(block=>String(block.rawValue||'').trim()).filter(Boolean).slice(0,500)
        .map(text=>({text,confidence:.5,kind:/^\D*\d[\d,\.]*\D*$/.test(text)?'number':'text'}));
      if(candidates.length)method='text-detector';
    }catch{/* fall through */}
  }
  if(!candidates.length){
    try{
      const result=await recognizeNumberFromFile(file);
      candidates=[{text:result.text,confidence:clamp(result.confidence),kind:'number'}];
      method=result.method;
    }catch{/* manual review remains available */}
  }
  return {method,candidates,previewUrl:canvas.toDataURL('image/jpeg',.78),image:{
    sha256:await sha256(file),mime:file.type,width:original.width,height:original.height,size:file.size,
  }};
}

export function buildReviewedPayload(kind,values){
  if(kind==='data_lamp'){
    const reviewed={game_count:integer(values.gameCount,0,999999),seat_number:integer(values.seatNumber,1,99999),
      bb_count:integer(values.bbCount,0,9999),rb_count:integer(values.rbCount,0,9999),text:'',seat_numbers:[]};
    if(Object.values(reviewed).slice(0,4).every(value=>value===null))throw new Error('確認できた数値を1つ以上入力してください');
    return reviewed;
  }
  if(kind==='store_material'){
    const text=String(values.text||'').trim();
    if(!text)throw new Error('読み取った文字を確認・訂正してください');
    if(text.length>10000)throw new Error('確認文字は10,000字以下にしてください');
    return {game_count:null,seat_number:null,bb_count:null,rb_count:null,text,seat_numbers:[]};
  }
  if(kind!=='floor_map')throw new Error('画像の種類を選び直してください');
  const seatNumbers=uniqueSeatNumbers(values.seatNumbers);
  if(!seatNumbers.length)throw new Error('確認できた台番号を1台以上入力してください');
  return {game_count:null,seat_number:null,bb_count:null,rb_count:null,text:'',seat_numbers:seatNumbers};
}

export function buildImageMapDraft(numbers,record){
  const seats=uniqueSeatNumbers(numbers.join(' '));
  if(!seats.length||!record.hall_name||!record.id)throw new Error('店舗と保存済み記録を確認してください');
  // A separate name prevents saving an OCR draft over a confirmed map's unique key.
  const columns=Math.max(14,Math.ceil(seats.length/40));
  const editorSeats=seats.map((seat_number,index)=>({seat_number,machine_name:'',island_name:'',row_name:'',row_order:null,
    x:55+(index%columns)*64,y:70+Math.floor(index/columns)*100,width:52,height:44,rotation:0}));
  return {hall_name:record.hall_name,status:'画像から仮配置',
    notice:'表示位置は仮です。実際の位置・島・隣接を現地で確認してください。',data_coverage:{history_rows:0,seat_count:0},
    layout:{id:null,hall_name:record.hall_name,floor_name:`画像下書き-${record.id}`,
      valid_from:record.observed_on||record.created_at.slice(0,10),width:Math.max(1000,columns*64+100),
      height:Math.max(420,170+Math.ceil(seats.length/columns)*100),source_url:'',
      source_label:'画像抽出（要現地確認）',source_kind:'uploaded',verification_status:'未確認',
      notes:`画像抽出記録 ${record.id}。位置・島・隣接は未確認。`,generated:false},
    seats:editorSeats.map(seat=>({...seat,score:null,color:'#64748b',heat_level:'データ不足',
      reasons:['画像抽出・配置確認待ち'],sample_days:0}))};
}

export function buildImageRecord({analysis,kind,values,context={},confirmed=false,saveOriginal=false}){
  if(!analysis)throw new Error('先に画像を読み取ってください');
  if(!confirmed)throw new Error('元画像と入力値を照合した確認にチェックしてください');
  return {kind,hall_name:String(context.hallName||'').slice(0,120),observed_on:context.observedOn||null,
    image:analysis.image,extraction_method:analysis.method,
    reviewed:buildReviewedPayload(kind,values),review_confirmed:true,save_original_on_device:Boolean(saveOriginal)};
}

async function keepOriginalOnDevice(file,record){
  if(!globalThis.indexedDB)throw new Error('このブラウザは元画像の端末保存に対応していません');
  const db=await new Promise((resolve,reject)=>{
    const request=indexedDB.open('pachi-tool-image-originals',1);
    request.onupgradeneeded=()=>request.result.createObjectStore('originals',{keyPath:'recordId'});
    request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
  });
  await new Promise((resolve,reject)=>{
    const tx=db.transaction('originals','readwrite');
    tx.objectStore('originals').put({recordId:record.id,createdAt:record.created_at,sha256:record.image.sha256,blob:file});
    tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);
  });
  db.close();
}

function editorHtml(kind,analysis){
  const joined=analysis.candidates.map(item=>item.text).join('\n');
  const numbers=analysis.candidates.flatMap(item=>numbersFromText(item.text));
  if(kind==='data_lamp')return `<div class="image-review-grid"><label>現在ゲーム数<input data-image-field="game-count" type="number" min="0" max="999999" inputmode="numeric" value="${escapeHtml(numbers[0]||'')}"></label><label>台番号<input data-image-field="seat-number" type="number" min="1" max="99999" inputmode="numeric"></label><label>BB回数<input data-image-field="bb-count" type="number" min="0" max="9999" inputmode="numeric"></label><label>RB回数<input data-image-field="rb-count" type="number" min="0" max="9999" inputmode="numeric"></label></div>`;
  if(kind==='store_material')return `<label>確認・訂正した文字<textarea data-image-field="text" rows="6" maxlength="10000">${escapeHtml(joined)}</textarea></label>`;
  return `<label>確認できた台番号<textarea data-image-field="seat-numbers" rows="5" placeholder="例：501, 502, 503">${escapeHtml([...new Set(numbers.map(Number).filter(n=>n>=1&&n<=99999))].join(', '))}</textarea></label><p class="fine-print">画像だけでは島・隣接・位置を確定しません。保存後の反映先は「未確認マップ」です。</p>`;
}

export function renderImageHistory(records=[]){
  if(!records.length)return '<p class="fine-print">確認済みの画像抽出記録はまだありません。</p>';
  return records.slice(0,5).map(row=>`<div class="image-history-row"><b>${escapeHtml(KIND_LABELS[row.kind]||row.kind)}</b><span>${escapeHtml(row.hall_name||'店舗未指定')}・${escapeHtml(row.observed_on||row.created_at?.slice(0,10)||'')}</span><small>${escapeHtml(row.verification_status)}／元画像：${row.image?.device_copy_requested?'端末保存を選択':'未保存'}（サーバー未送信）</small></div>`).join('');
}

export async function mountImageAnalysis(root,{apiUrl=path=>path,fetcher=fetch,analyzer=analyzeImageFile,getContext=()=>({}),onApplyDataLamp=()=>{},onApplyMap=()=>{}}={}){
  if(!root)return;
  root.innerHTML=`<div class="section-bar"><div><span class="page-step">LOCAL IMAGE · 確認して反映</span><h2>画像を読み取る</h2></div><span data-image-el="privacy" class="rule-badge">画像送信なし</span></div>
    <p class="fine-print">端末内で候補を抽出します。必ず元画像と照合して訂正してください。保存時は確認済みの文字・数値だけをPACHI TOOLへ送ります。</p>
    <form data-image-el="form"><label>画像の種類<select data-image-el="kind"><option value="data_lamp">データランプ</option><option value="store_material">店舗資料</option><option value="floor_map">ホールマップ</option></select></label>
    <label class="image-file-button">画像を選ぶ<input data-image-el="file" type="file" accept="image/jpeg,image/png,image/webp" capture="environment"></label>
    <button data-image-el="analyze" class="secondary-button" type="button">端末内で読み取る</button>
    <div data-image-el="preview" class="image-analysis-preview"></div><div data-image-el="editor"></div>
    <label class="image-confirm"><input data-image-el="confirm" type="checkbox"> 元画像と入力値を自分で照合した</label>
    <label class="image-confirm"><input data-image-el="original" type="checkbox"> 元画像もこの端末だけに保存する</label>
    <button data-image-el="save" class="primary-button" type="submit" disabled>確認した抽出値を保存</button></form>
    <button data-image-el="apply" class="secondary-button" type="button" hidden disabled></button>
    <p data-image-el="message" class="fine-print" aria-live="polite">画像の読取りは端末内で行います。</p><div data-image-el="history"></div>`;
  const el=name=>root.querySelector(`[data-image-el="${name}"]`);
  let analysis=null;
  let currentFile=null;
  let generation=0, saving=false, analyzedContext=null, pendingApply=null, saved=false;
  el('save').disabled=true;
  const loadHistory=async()=>{
    try{const res=await fetcher(apiUrl('/api/image-analysis/records?limit=5'));if(res.ok)el('history').innerHTML=renderImageHistory(await res.json());}catch{/* offline history is optional */}
  };
  const resetApply=()=>{pendingApply=null;saved=false;el('apply').hidden=true;el('apply').disabled=true;};
  const invalidate=()=>{generation++;analysis=null;currentFile=null;resetApply();el('confirm').checked=false;el('editor').innerHTML='';el('preview').innerHTML='';el('save').disabled=true;};
  el('kind').addEventListener('change',invalidate);
  el('file').addEventListener('change',invalidate);
  el('editor').addEventListener('input',()=>{resetApply();el('confirm').checked=false;el('save').disabled=true;});
  el('confirm').addEventListener('change',()=>{el('save').disabled=saving||saved||!(analysis&&el('confirm').checked);});
  el('apply').addEventListener('click',()=>{
    if(!pendingApply)return;
    if(analyzedContext!==JSON.stringify(getContext())){invalidate();el('message').textContent='店舗・日付が変わりました。反映は止めました。';return;}
    try{
      const {payload,data}=pendingApply;
      if(payload.kind==='data_lamp')onApplyDataLamp(payload.reviewed);
      else onApplyMap(payload.reviewed.seat_numbers,data);
      pendingApply=null;el('apply').disabled=true;
      el('message').textContent=payload.kind==='data_lamp'?'ハイエナの入力欄へ反映しました。機種とゲーム数の種類（AT間・ボーナス間など）も確認してください。':'未確認の別名下書きへ反映しました。配置は未確定です。';
    }catch(error){el('message').textContent=error.message;}
  });
  el('analyze').addEventListener('click',async()=>{
    if(saving)return;
    const token=++generation, kind=el('kind').value;
    analyzedContext=JSON.stringify(getContext());analysis=null;resetApply();el('editor').innerHTML='';el('preview').innerHTML='';el('confirm').checked=false;el('save').disabled=true;
    currentFile=el('file').files?.[0]||null;el('message').textContent='端末内で読み取り中…';el('analyze').disabled=true;
    try{
      const result=await analyzer(currentFile);
      if(token!==generation||kind!==el('kind').value)return;
      analysis=result;
      const preview=/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(analysis.previewUrl||'')?`<img src="${analysis.previewUrl}" alt="選択画像の確認用プレビュー">`:'';
      el('preview').innerHTML=`${preview}<p>${analysis.candidates.length}件の候補／${escapeHtml(analysis.method)}</p>`;
      el('editor').innerHTML=editorHtml(el('kind').value,analysis);el('confirm').checked=false;el('save').disabled=true;
      el('message').textContent=analysis.candidates.length?'候補を抽出しました。誤読を訂正してください。':'自動抽出できませんでした。画像を見ながら手入力してください。';
    }catch(error){if(token===generation){analysis=null;el('message').textContent=error.message;el('save').disabled=true;}}finally{el('analyze').disabled=false;}
  });
  el('form').addEventListener('submit',async event=>{
    event.preventDefault();
    if(saving||saved)return;
    if(analyzedContext!==JSON.stringify(getContext())){invalidate();el('message').textContent='店舗・日付が変わりました。画像を読み直してください。';return;}
    const values={gameCount:root.querySelector('[data-image-field="game-count"]')?.value,seatNumber:root.querySelector('[data-image-field="seat-number"]')?.value,bbCount:root.querySelector('[data-image-field="bb-count"]')?.value,rbCount:root.querySelector('[data-image-field="rb-count"]')?.value,text:root.querySelector('[data-image-field="text"]')?.value,seatNumbers:root.querySelector('[data-image-field="seat-numbers"]')?.value};
    let payload;
    try{payload=buildImageRecord({analysis,kind:el('kind').value,values,context:getContext(),confirmed:el('confirm').checked,saveOriginal:el('original').checked});}
    catch(error){el('message').textContent=error.message;return;}
    const token=generation, savedFile=currentFile;
    saving=true;el('file').disabled=true;el('kind').disabled=true;el('analyze').disabled=true;
    el('editor').querySelectorAll?.('input,textarea').forEach(field=>{field.disabled=true;});
    el('save').disabled=true;el('message').textContent='確認した抽出値を保存中…';
    try{
      const res=await fetcher(apiUrl('/api/image-analysis/records'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data=await res.json();if(!res.ok)throw new Error(data.detail?.[0]?.msg||data.detail||'保存できませんでした');
      let originalNote='元画像は保存していません。';
      if(payload.save_original_on_device){
        try{await keepOriginalOnDevice(savedFile,data);originalNote='元画像はこの端末だけに保存しました。';}
        catch{originalNote='抽出値は保存済みですが、元画像の端末保存だけ失敗しました。';}
      }
      if(token!==generation||analyzedContext!==JSON.stringify(getContext())){el('message').textContent='変更前の店舗・日付で保存済みです。現在の画面への反映は止めました。';return;}
      saved=true;
      // Saving observations does not authorize overwriting another mode's inputs or map editor.
      if(payload.kind==='data_lamp'||payload.kind==='floor_map'){
        pendingApply={payload,data};el('apply').hidden=false;el('apply').disabled=false;
        el('apply').textContent=payload.kind==='data_lamp'?'ハイエナの入力欄へ反映（現在値を置換）':'別名の未確認マップとして編集欄へ反映（編集中の下書きを置換）';
      }
      el('message').textContent=`保存しました。${originalNote} 他の入力欄にはまだ反映していません。`;
      await loadHistory();
    }catch(error){el('message').textContent=error.message;el('save').disabled=false;}
    finally{saving=false;el('file').disabled=false;el('kind').disabled=false;el('analyze').disabled=false;
      el('editor').querySelectorAll?.('input,textarea').forEach(field=>{field.disabled=false;});}
  });
  try{
    const response=await fetcher(apiUrl('/api/image-analysis/status'));
    if(response.ok){const status=await response.json();el('privacy').textContent=status.external_image_ai_enabled?'外部画像AI有効':'画像送信なし';}
  }catch{/* local extraction still works */}
  await loadHistory();
}
