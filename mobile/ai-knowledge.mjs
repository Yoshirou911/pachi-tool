import {renderAiEvidence} from './ai-evidence.mjs?v=3.49.0';

const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const CATEGORIES={glossary:'用語・使い方',machine:'機種の登録資料',expectation:'期待値条件',hall:'店舗の参照先'};
const CONDITIONS={exchange_type:'交換条件',funding_mode:'投資方法',reset_status:'リセット条件',metric_name:'数える項目',unit_label:'単位',condition_label:'個別条件'};
const VALUES={equivalent:'等価',medals:'持ちメダル',cash:'現金',any:'この交換条件内の共通条件',normal:'通常',reset_confirmed:'リセット確定',unknown:'未確認','56':'5.6枚交換'};

export function knowledgePayload(status,context,values){
  if(!status?.index_hash)throw new Error('資料一覧を更新してください。');
  if(!/^\d{4}-\d{2}-\d{2}$/.test(context.visit_date||''))throw new Error('上の欄で行く日を選んでください。');
  if(!values.question.trim())throw new Error('調べたい用語・内容を入力してください。');
  return {index_hash:status.index_hash,target_date:context.visit_date,question:values.question.trim(),category:values.category,
    machine_name:['machine','expectation'].includes(values.category)?values.machine:'',
    hall_name:values.category==='hall'?(context.hall_name||''):'',condition_id:values.category==='expectation'?values.condition:''};
}

export function renderKnowledgeAnswer(data){
  const selected=new Set((data.claims||[]).map(c=>c.evidence_id));
  const facts=(data.evidence||[]).filter(f=>selected.has(f.id));
  return `<p><b>${esc(data.notice)}</b></p><p>${esc(data.engine)} · 対象日 ${esc(data.target_date)}</p>
    ${facts.length?facts.map(f=>{const d=f.knowledge;return `<article class="card" style="margin:12px 0;padding:14px;overflow-wrap:anywhere">
      <h4>[${esc(f.id)}] ${esc(d.title)}</h4><p style="white-space:pre-wrap">${esc(d.content)}</p>
      ${Object.keys(d.conditions||{}).length?`<p><b>この資料の適用条件</b></p><ul>${Object.entries(d.conditions).map(([k,v])=>`<li>${esc(CONDITIONS[k]||k)}：${esc(VALUES[v]||v)}</li>`).join('')}</ul>`:''}
      <p>確認日 ${esc(d.reviewed_on||'未記録')} · ${esc(d.verification)}</p>
      ${(f.missing_information||[]).map(t=>`<p>${esc(t)}</p>`).join('')}</article>`;}).join(''):
      `<p style="white-space:pre-wrap">${esc(data.summary||'該当資料はありません。')}</p>`}
    ${renderAiEvidence(data)}<small>資料集合 ${esc(data.index_hash?.slice(0,12))} / ${esc(data.knowledge_version)}</small>`;
}

export async function mountKnowledge(root,{getContext,apiUrl=p=>p,fetcher=fetch,contextElements=[]}){
  if(!root||root.dataset.mounted)return;
  root.dataset.mounted='true';
  root.innerHTML=`<h3>専用知識を調べる</h3><p>登録資料の内容を、条件・確認日・引用元と一緒に表示します。追加学習ではありません。外部AIへの送信・課金なし。</p>
    <button type="button" data-k="refresh">資料一覧を更新</button><p data-k="status"></p>
    <form data-k="form" style="display:grid;gap:12px">
      <label>資料の種類<select data-k="category" style="width:100%;min-height:44px">${Object.entries(CATEGORIES).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></label>
      <label data-k="machine-label" hidden>機種<select data-k="machine" style="width:100%;min-height:44px"></select></label>
      <label data-k="condition-label" hidden>引用する条件<select data-k="condition" style="width:100%;min-height:44px"></select></label>
      <p data-k="scope"></p>
      <label>用語・調べたい内容<input data-k="question" value="期待値とは" maxlength="1000" style="width:100%;min-height:44px"></label>
      <button data-k="search" type="submit" disabled style="min-height:44px">資料を検索して引用</button>
    </form><div data-k="result" role="status" aria-live="polite" style="overflow-wrap:anywhere"></div>`;
  const el=k=>root.querySelector(`[data-k="${k}"]`);
  let catalog=null,generation=0,controller;
  const values=()=>({category:el('category').value,machine:el('machine').value,condition:el('condition').value,question:el('question').value});
  const invalidate=()=>{generation++;controller?.abort();el('result').textContent='条件を確認して検索してください。';el('search').disabled=!catalog;};
  const configure=()=>{
    const category=el('category').value;
    el('machine-label').hidden=!['machine','expectation'].includes(category);
    el('condition-label').hidden=category!=='expectation';
    const options=(catalog?.conditions||[]).filter(d=>!el('machine').value||d.machine_name===el('machine').value);
    el('condition').innerHTML='<option value="">条件を選択（未選択では数値を引用しません）</option>'+options.map(d=>`<option value="${esc(d.id)}">${esc(d.title)}</option>`).join('');
    el('condition').value='';
    el('scope').textContent=category==='hall'?`上で選んだ店舗：${getContext().hall_name||'未選択'}。登録参照先であり、強さや開催の判定ではありません。`:'期待値資料は登録時の条件の引用です。現在の着席判断は行いません。';
    invalidate();
  };
  async function refresh(){
    invalidate();catalog=null;el('search').disabled=true;el('refresh').disabled=true;el('status').textContent='資料一覧を確認中…';
    const token=generation;
    const active=new AbortController();controller=active;
    const timeout=setTimeout(()=>active.abort(),20000);
    try{
      const response=await fetcher(apiUrl('/api/ai/knowledge'),{signal:active.signal});
      if(!response.ok)throw new Error('資料一覧を取得できません。API接続・バージョンを確認してください。');
      const data=await response.json();
      if(token!==generation)return;
      catalog=data;
      el('status').textContent=Object.entries(data.counts||{}).map(([k,v])=>`${CATEGORIES[k]||k} ${v}件`).join(' / ')+(data.load_errors?.length||data.rejected_count?'（読込不能・除外資料あり）':'');
      el('machine').innerHTML='<option value="">機種を選択</option>'+(data.machines||[]).map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join('');
      el('machine').value='';configure();
    }catch(error){if(token===generation)el('status').textContent=error.name==='AbortError'?'資料一覧の通信を中断しました。更新し直してください。':error.message;}
    finally{clearTimeout(timeout);el('refresh').disabled=false;
      if(token!==generation&&!catalog)el('status').textContent='条件が変わりました。資料一覧を更新してください。';}
  }
  el('category').addEventListener('change',()=>{el('question').value=el('category').value==='glossary'?'期待値とは':'登録資料を確認';configure();});
  el('machine').addEventListener('change',configure);
  el('condition').addEventListener('change',invalidate);
  el('question').addEventListener('input',invalidate);
  contextElements.forEach(e=>e?.addEventListener('change',configure));
  el('refresh').addEventListener('click',refresh);
  el('form').addEventListener('submit',async event=>{
    event.preventDefault();
    let payload;
    try{payload=knowledgePayload(catalog,getContext(),values());}catch(error){el('result').textContent=error.message;return;}
    const token=++generation;controller?.abort();const active=new AbortController();controller=active;
    const timeout=setTimeout(()=>active.abort(),20000);
    el('search').disabled=true;el('result').textContent='条件に合う資料を検索中…';
    try{
      const response=await fetcher(apiUrl('/api/ai/knowledge/search'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:active.signal});
      if(token!==generation)return;
      if(response.status===409){catalog=null;throw new Error('資料の版が変わりました。「資料一覧を更新」で条件を選び直してください。');}
      if(!response.ok)throw new Error('検索できません。入力条件とAPI接続を確認してください。');
      const data=await response.json();
      if(token!==generation)return;
      if(JSON.stringify(payload)!==JSON.stringify(knowledgePayload(catalog,getContext(),values()))){el('result').textContent='条件が変わったため古い検索結果を表示しません。';return;}
      el('result').innerHTML=renderKnowledgeAnswer(data);
    }catch(error){if(token===generation)el('result').textContent=error.name==='AbortError'?'通信を中断しました。もう一度検索してください。':error.message;}
    finally{clearTimeout(timeout);if(token===generation)el('search').disabled=!catalog;}
  });
  await refresh();
}
