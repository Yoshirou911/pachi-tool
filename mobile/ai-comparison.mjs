const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

export function comparisonPayload(context, providers, caseIds, mode, confirmed = false, requestId = '') {
  if (!context.hall_name?.trim() || context.hall_name === '全店舗') throw new Error('上の欄で店舗を1店選んでください。');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(context.visit_date || '')) throw new Error('上の欄で行く日を選んでください。');
  if (!providers.length) throw new Error('比較するAIを1社以上選んでください。');
  if (!caseIds.length || caseIds.length > 3) throw new Error('固定問題を1〜3件選んでください。');
  if (mode === 'external' && !confirmed) throw new Error('料金が発生する可能性を確認してください。');
  return {hall_name:context.hall_name,visit_date:context.visit_date,days:Number(context.days || 90),
    providers,case_ids:caseIds,mode,confirm_external:mode === 'external' && confirmed,
    confirm_public_data_only:mode === 'external' && confirmed,
    request_id:requestId || globalThis.crypto?.randomUUID?.()};
}

const value = (number, suffix = '') => number == null ? '未計測' : `${number}${suffix}`;

export function renderComparisonResult(data) {
  const cases = (data.case_definitions || []).map(item =>
    `<li>${esc(item.label)}：根拠${esc(item.evidence_count)}件${item.has_evidence ? '' : '（不足）'}</li>`).join('');
  const providers = (data.provider_results || []).map(item => {
    const s = item.summary || {};
    const state = {not_run:'準備確認のみ',unavailable:'実測不可',measured:'実測済み'}[item.status] || item.status;
    return `<article style="padding:12px 0;border-top:1px solid currentColor">
      <strong>${esc(item.label)} · ${esc(item.model)}</strong><p>${esc(state)}</p>
      <ul><li>契約適合：${value(s.contract_pass_rate_pct,'%')}</li>
      <li>回答点検で不採用：${value(s.guard_rejected_cases,'件')} / 回答を控えた：${value(s.abstained_cases,'件')}</li>
      <li>固定期待根拠との一致：${value(s.evidence_match_pct,'%')}</li>
      <li>平均応答：${value(s.average_latency_ms,'ms')}</li>
      <li>報告トークン：入力 ${value(s.reported_input_tokens)} / 出力 ${value(s.reported_output_tokens)}</li>
      <li>費用見積り：${s.estimated_cost_usd == null ? '未計測（単価または使用量が未記録）' : `$${esc(s.estimated_cost_usd)}`}</li></ul>
      ${(item.cases || []).some(c => c.error_kind) ? `<p>失敗区分：${esc(item.cases.filter(c=>c.error_kind).map(c=>`${c.case_id}:${c.error_kind}`).join(' / '))}</p>` : ''}
      ${(item.cases || []).filter(c=>c.answer_guard?.reasons?.length).map(c=>`<p>${esc(c.case_id)}：${c.answer_guard.reasons.map(esc).join(' / ')}</p>`).join('')}
    </article>`;
  }).join('');
  return `<p><strong>${data.mode === 'external' ? '外部AI実測' : '準備確認'}</strong> · 勝者の自動決定なし · 実戦判定の変更なし</p>
    <p>${esc(data.notice || '')}</p><details><summary>共通の固定問題と根拠</summary><ul>${cases}</ul></details>${providers}`;
}

export function renderComparisonControls(status) {
  const budgets = new Map((status.governance?.providers || []).map(item=>[item.provider,item]));
  const providers = (status.providers || []).map(item => { const budget=budgets.get(item.provider)||{}; return `<label style="display:block"><input type="checkbox" data-provider value="${esc(item.provider)}" checked>
    ${esc(item.label)} · ${esc(item.model)} <small>（${esc(item.reason)}${item.price_configured ? '・単価設定済み' : '・単価未設定'}・月上限 ${budget.monthly_limit_usd == null ? '未設定' : '$'+esc(budget.monthly_limit_usd)}・予約済み ${budget.month_reserved_usd == null ? '不明' : '$'+esc(budget.month_reserved_usd)}・残り ${budget.remaining_usd == null ? '不明' : '$'+esc(budget.remaining_usd)}）</small></label>`; }).join('');
  const cases = (status.cases || []).map((item,index) => `<label style="display:block"><input type="checkbox" data-case value="${esc(item.id)}" ${index < 2 ? 'checked' : ''}>
    ${esc(item.label)} <small>${esc(item.question)}</small></label>`).join('');
  return `<div style="display:grid;gap:12px"><div><b>比較するAI</b>${providers}</div><div><b>固定問題（最大3件）</b>${cases}</div>
    <button type="button" data-preview>外部通信なしで準備を確認</button>
    <p>外部AIへ送るのは選択した店舗・日付の公開根拠と固定質問です。個人の遊技履歴・過去の会話は送りません。初期設定の準備確認はローカル処理です。</p>
    <p data-plan></p>
    <label><input type="checkbox" data-confirm> 公開根拠と固定質問だけを送信し、表示した回数のAPI料金が発生する可能性を確認しました（この1回のみ）</label>
    <button type="button" data-external disabled>外部AIで実測する</button>
    <small>${esc(status.notice || '')}</small></div>`;
}

export function mountAiComparison(root, {getContext, apiUrl = path => path, fetcher = fetch,
  makeId = () => globalThis.crypto.randomUUID()}) {
  if (!root || root.dataset.mounted) return;
  root.dataset.mounted = 'true';
  root.innerHTML = `<h3>複数AIを同じ条件で比較</h3><p>回答のうまさではなく、固定した根拠を正しく選べるかを比較します。勝手な実通信・採用はしません。</p>
    <div data-controls>比較設定を確認しています…</div><div data-result role="status" aria-live="polite" style="margin-top:12px;overflow-wrap:anywhere"></div>`;
  const controls = root.querySelector('[data-controls]');
  const result = root.querySelector('[data-result]');
  let status, busy=false, pending=null, consentKey=null;
  const selected=selector=>[...controls.querySelectorAll(selector+':checked')].map(item=>item.value);
  const key=()=>JSON.stringify(comparisonPayload(getContext(),selected('[data-provider]'),selected('[data-case]'),'preview',false,'conditions'));
  const paidReady=()=>{
    const providers=selected('[data-provider]'), cases=selected('[data-case]');
    return Boolean(status?.external_comparison_enabled && providers.length && cases.length && cases.length<=3 && providers.every(name=>{
      const connection=status.providers?.find(item=>item.provider===name);
      const budget=status.governance?.providers?.find(item=>item.provider===name);
      return connection?.ready && budget?.ready_for_paid_calls && budget.remaining_usd>0;
    }));
  };
  function updateControls() {
    const confirm=controls.querySelector('[data-confirm]');
    [...controls.querySelectorAll('[data-provider]'),...controls.querySelectorAll('[data-case]'),confirm].forEach(item=>{item.disabled=busy || Boolean(pending);});
    controls.querySelector('[data-preview]').disabled=busy || Boolean(pending && pending.mode!=='preview');
    controls.querySelector('[data-preview]').textContent=pending?.mode==='preview' ? '同じ実行IDで準備状況を確認' : '外部通信なしで準備を確認';
    const external=controls.querySelector('[data-external]');
    external.disabled=busy || (pending ? pending.mode!=='external' : !paidReady() || !confirm.checked);
    external.textContent=pending?.mode==='external' ? '同じ実行IDで受付状況を確認' : '外部AIで実測する';
    controls.querySelector('[data-plan]').textContent=`最大${selected('[data-provider]').length*selected('[data-case]').length}回の外部呼び出し予定。${paidReady() ? '送信時にも予算を確認します。' : '選択したAIの接続・単価・予算、問題数の条件が不足しているため実測できません。'}`;
  }
  return fetcher(apiUrl('/api/ai/comparison/status')).then(response => {
    if (!response.ok) throw new Error('比較設定を取得できませんでした。');
    return response.json();
  }).then(data => {
    status = data;
    controls.innerHTML = renderComparisonControls(data);
    const external = controls.querySelector('[data-external]');
    const confirm = controls.querySelector('[data-confirm]');
    confirm.addEventListener('change', () => {
      try{consentKey=confirm.checked ? key() : null;}
      catch(error){confirm.checked=false;consentKey=null;result.textContent=error.message;}
      updateControls();
    });
    [...controls.querySelectorAll('[data-provider]'),...controls.querySelectorAll('[data-case]')].forEach(item=>item.addEventListener('change',()=>{
      confirm.checked=false;consentKey=null;updateControls();
    }));
    controls.querySelector('[data-preview]').addEventListener('click', () => run('preview'));
    external.addEventListener('click', () => run('external'));
    updateControls();
  }).catch(error => { controls.textContent = error.message; });

  async function run(mode) {
    if(busy || (pending && pending.mode!==mode))return;
    const providers = [...controls.querySelectorAll('[data-provider]:checked')].map(item => item.value);
    const cases = [...controls.querySelectorAll('[data-case]:checked')].map(item => item.value);
    const confirmed = Boolean(controls.querySelector('[data-confirm]')?.checked);
    try {
      if(!pending){
        if(mode==='external' && (!confirmed || consentKey!==key())){
          controls.querySelector('[data-confirm]').checked=false;consentKey=null;updateControls();
          result.textContent='店舗・日付・期間・AI・問題数を確認し、この1回の外部送信にチェックしてください。';return;
        }
        if(mode==='external' && !paidReady()){result.textContent='選択したAIの接続・単価・予算を確認できないため外部送信できません。';return;}
        pending=comparisonPayload(getContext(),providers,cases,mode,confirmed,makeId());
      }
    }
    catch (error) { result.textContent = error.message; return; }
    const payload=pending;
    const callCount = payload.providers.length * payload.case_ids.length;
    busy=true;updateControls();
    result.textContent = mode === 'external' ? `${callCount}回まで外部AIを呼び出して実測しています…` : '外部通信なしで共通条件を準備しています…';
    try {
      const response = await fetcher(apiUrl('/api/ai/comparison/run'), {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok) {
        const error=new Error(typeof data?.detail==='string' ? data.detail : '比較を実行できませんでした。');
        error.confirmedResponse=response.status>=400 && response.status<500 && response.status!==408;
        throw error;
      }
      if(!Array.isArray(data?.provider_results))throw new Error('invalid comparison response');
      pending=null;controls.querySelector('[data-confirm]').checked=false;consentKey=null;
      const current = getContext();
      if (current.hall_name !== payload.hall_name || current.visit_date !== payload.visit_date || Number(current.days || 90) !== payload.days
          || JSON.stringify(selected('[data-provider]'))!==JSON.stringify(payload.providers)
          || JSON.stringify(selected('[data-case]'))!==JSON.stringify(payload.case_ids)) {
        result.textContent = '店舗・日付・期間が変更されたため、古い比較結果は表示しません。';
      } else result.innerHTML = renderComparisonResult(data);
    } catch (error) {
      if(error.confirmedResponse){pending=null;controls.querySelector('[data-confirm]').checked=false;consentKey=null;}
      result.textContent=error.confirmedResponse ? error.message : '受付状況が不明です。条件を変えず「同じ実行ID」で確認してください。新しい実行は開始しません。';
    }
    finally {
      busy=false;updateControls();
    }
  }
}
