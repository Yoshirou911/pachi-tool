import {renderAiEvidence} from './ai-evidence.mjs?v=3.49.0';

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (value, suffix = '') => Number.isFinite(value) ? `${value}${suffix}` : '未計測';
const statusLabel = status => ({running:'評価中',completed:'完了',failed:'処理失敗',interrupted:'中断・自動再開なし'}[status] || '状態不明');

export function evaluationPayload(catalog, provider, repeats, confirmed, requestId) {
  if (!catalog?.suite_hash) throw new Error('評価問題を読み込み直してください。');
  const count = Number(repeats);
  if (![1,2,3].includes(count)) throw new Error('反復回数は1〜3回です。');
  if (provider && !confirmed) throw new Error('外部AIの実行確認が必要です。');
  return {request_id:requestId,suite_hash:catalog.suite_hash,mode:provider ? 'external' : 'offline',
    provider:provider || null,repeats:count,confirm_external:Boolean(provider && confirmed),
    confirm_public_data_only:Boolean(provider && confirmed)};
}

export function renderEvaluationReport(report) {
  const s = report.summary || {};
  const progress = report.status === 'running'
    ? `<p>進捗 ${number(s.completed)} / ${number(s.total)} 回 · ${number(report.progress_pct,'%')}</p><progress max="100" value="${Number(report.progress_pct) || 0}" style="width:100%"></progress>` : '';
  const complete = report.status === 'completed';
  return `<h4>${esc(report.label)} · ${esc(statusLabel(report.status))}</h4>
    <p>${esc(report.notice)}</p>${report.mode === 'offline' ? '<p><b>ローカル参考方式の点検です。外部AIの実力は未計測です。</b></p>' : ''}${progress}
    <p>${esc(report.model)} / 問題集 ${esc(report.suite_version)} / 各問${number(report.repeats)}回 / ${esc(report.created_at)}</p>
    <p>${complete ? '保存済みの最終結果' : '途中結果（完了成績には含めません）'}</p>
    <ul><li>正解の根拠を選べた割合：${number(s.exact_match_pct,'%')}（${number(s.exact_count)} / ${number(s.completed)}回）</li>
      <li>回答形式が正しい割合：${number(s.schema_pass_pct,'%')}</li>
      <li>回答前の点検：${number(s.guard_evaluated_count)}回中 ${number(s.guard_rejected_count)}回を不採用（旧記録は未計測）</li>
      <li>選んだ根拠の適切さ：${number(s.precision_pct,'%')} / 必要な根拠を拾えた割合：${number(s.recall_pct,'%')}</li>
      <li>不要な根拠：${number(s.unnecessary_count)}件 / 禁止根拠の混入：${number(s.forbidden_count)}件</li>
      <li>根拠不足で回答を控えられた割合：${number(s.abstention_pass_pct,'%')}</li>
      <li>反復時の回答一致：${number(s.consistency_pct,'%')}（比較可能 ${number(s.consistency_pairs)} / ${number(s.planned_pairs)}組）</li>
      <li>外部呼出試行：${number(s.external_calls)}回 / 費用概算：${s.estimated_cost_usd == null ? '未計測' : '$' + number(s.estimated_cost_usd)}</li></ul>
    <p><small>同じ誤答が続いても「回答一致」は高くなります。正解率と併せて確認してください。</small></p>
    ${(report.cases || []).map(c => {
      const rows = (report.samples || []).filter(s=>s.case_id===c.id);
      return `<details><summary>${esc(c.category)} · ${rows.filter(r=>r.exact_match).length} / ${rows.length}回 正解</summary>
        <p>${esc(c.question)}</p><p>正解根拠：${esc(c.expected_ids?.join(', ') || '回答を控える（空配列）')}<br>${esc(c.reason)}</p>
        ${rows.map(r=>`<p>${number(r.attempt)}回目：${r.schema_valid ? esc(r.selected_ids.join(', ') || '回答を控えた') : '形式不正・通信失敗'}<br>
          ${r.exact_match ? '正解' : '要確認'}${r.error_kind ? ' · ' + esc(r.error_kind) : ''}
          ${r.guard_passed === false ? ' · 回答には不採用：' + esc((r.answer_guard?.reasons || []).join(' / ')) : ''}
          ${r.unnecessary_ids?.length ? ' · 不要 ' + esc(r.unnecessary_ids.join(', ')) : ''}
          ${r.missing_ids?.length ? ' · 不足 ' + esc(r.missing_ids.join(', ')) : ''}</p>`).join('')}
        ${c.excluded_inputs?.length ? `<p>入力期限後のため除外：${esc(c.excluded_inputs.join(', '))}</p>` : ''}
        ${c.outcome ? `<p>後日照合用の架空結果：${esc(c.outcome.report_date)} · ${number(c.outcome.diff_coins,'枚')}。AIへの入力には含めていません。</p>` : ''}
        ${renderAiEvidence({...c.snapshot,claims:[],answer_status:'ai_disabled'})}</details>`;
    }).join('')}
    <details><summary>この評価の保存条件</summary><p>問題識別 ${esc(report.suite_hash)}<br>入力識別 ${esc(report.prompt_hash)}<br>
      採点方式 ${esc(report.protocol)} / アプリ ${esc(report.app_version)}<br>過去の記録を上書きせず、実行ごとに保存します。</p></details>`;
}

export async function mountAiEvaluation(root, {apiUrl=path=>path,fetcher=fetch,
  makeId=()=>globalThis.crypto.randomUUID(),schedule=setTimeout,cancel=clearTimeout} = {}) {
  if (!root || root.dataset.mounted) return;
  root.dataset.mounted = 'true';
  root.innerHTML = `<h3>AI回答の自動評価</h3><p>固定問題で、根拠の選び方・回答を控える判断・反復時の安定性を点検します。</p>
    <button type="button" data-refresh>評価状況を更新</button><p data-info></p>
    <form data-eval-form style="display:grid;gap:12px">
      <label>点検する方式<select data-provider style="width:100%;min-height:44px"><option value="">ローカル参考方式（AI未使用・無料）</option></select></label>
      <label>各問の反復<select data-repeats style="width:100%;min-height:44px"><option value="1">1回</option><option value="2" selected>2回</option><option value="3">3回</option></select></label>
      <p>外部AIへ送るのは固定された架空問題とその根拠です。実際の個人の遊技履歴・過去の会話は送りません。初期設定はローカル処理です。</p>
      <label data-consent hidden><input type="checkbox" data-confirm> 固定された架空問題だけを送信し、表示した回数の料金が発生する可能性を確認しました（この1回のみ）</label>
      <p data-plan></p><button type="submit" data-run disabled>評価を開始して保存</button>
    </form><p data-message role="status" aria-live="polite"></p>
    <details><summary>保存した評価（直近10件）</summary><div data-history></div></details>
    <div data-report style="overflow-wrap:anywhere"></div>`;
  const q = selector=>root.querySelector(selector);
  const provider=q('[data-provider]'), repeats=q('[data-repeats]'), confirm=q('[data-confirm]');
  const message=q('[data-message]'), report=q('[data-report]'), run=q('[data-run]');
  let catalog, pending=null, busy=false, active=null, selectedRun=null, timer=null, generation=0, consentKey=null;
  const key=()=>JSON.stringify([catalog?.suite_hash,provider.value,Number(repeats.value)]);
  const paidReady=()=>{
    const connection=catalog?.connection?.providers?.find(item=>item.provider===provider.value);
    const budget=catalog?.connection?.governance?.providers?.find(item=>item.provider===provider.value);
    return Boolean(connection?.ready && budget?.ready_for_paid_calls && budget.remaining_usd>0);
  };
  function controls() {
    const external=Boolean(provider.value);
    q('[data-consent]').hidden=!external;
    const count=(catalog?.cases?.length || 0)*Number(repeats.value);
    const budget=(catalog?.connection?.governance?.providers || []).find(item=>item.provider===provider.value);
    q('[data-plan]').textContent=external ? `${count}回の外部呼び出し予定。月上限 ${budget?.monthly_limit_usd == null ? '未設定' : '$'+budget.monthly_limit_usd} / 予約済み ${budget?.month_reserved_usd == null ? '不明' : '$'+budget.month_reserved_usd} / 残り ${budget?.remaining_usd == null ? '不明' : '$'+budget.remaining_usd}。${paidReady() ? '送信時にも予算を確認します。' : '接続・単価・予算の条件が不足しているため実行できません。'}本文は保存せず採点結果だけを保存します。`
      : `${catalog?.cases?.length || 0}問 × ${repeats.value}回をローカルで点検します。外部通信は0回です。`;
    provider.disabled=repeats.disabled=confirm.disabled=busy || Boolean(active) || Boolean(pending);
    run.disabled=busy || Boolean(active) || !catalog || (!pending && external && (!confirm.checked || !paidReady()));
    run.textContent=pending ? '同じ実行IDで開始状況を確認' : '評価を開始して保存';
  }
  async function request(path, options) {
    const r=await fetcher(apiUrl(path),options);
    const data=await r.json();
    if (!r.ok) {
      const error=new Error(typeof data?.detail==='string' ? data.detail : '評価を取得できませんでした。');
      error.confirmedResponse=r.status>=400 && r.status<500 && r.status!==408;
      throw error;
    }
    return data;
  }
  async function showRun(id, token=++generation) {
    const data=await request('/api/ai/evaluation/runs/'+encodeURIComponent(id));
    if (token!==generation) return;
    selectedRun=id;
    report.innerHTML=renderEvaluationReport(data);
    if (data.status==='running') {
      active=id;
      message.textContent=`評価中 ${data.summary.completed}/${data.summary.total}回（${data.progress_pct}%）`;
      timer=schedule(()=>showRun(id,token).catch(error=>{message.textContent='進捗を確認できません。評価状況を更新してください。';}),1500);
    } else {
      if (active===id) active=null;
      message.textContent=statusLabel(data.status)+'。結果を保存しています。';
    }
    controls();
  }
  async function refresh() {
    cancel(timer);
    const token=++generation;
    try {
      const data=await request('/api/ai/evaluation');
      if(token!==generation)return;
      catalog=data; active=data.active_run_id;
      if(!pending){confirm.checked=false;consentKey=null;}
      const previous=provider.value;
      provider.innerHTML='<option value="">ローカル参考方式（AI未使用・無料）</option>'+(data.connection?.providers || []).map(p=>
        `<option value="${esc(p.provider)}" ${p.ready ? '' : 'disabled'}>${esc(p.label)} · ${esc(p.model)}${p.ready ? '' : '（未接続）'}</option>`).join('');
      if(previous) provider.value=previous;
      q('[data-info]').textContent=`問題集 ${data.suite_version} · ${data.cases.length}問。${data.notice}`;
      q('[data-history]').innerHTML=(data.runs || []).map(r=>`<button type="button" data-history-id="${esc(r.id)}" style="display:block;min-height:44px">${esc(r.label)} · ${esc(statusLabel(r.status))} · ${esc(r.created_at)}</button>`).join('') || '<p>まだ記録はありません。</p>';
      q('[data-history]').querySelectorAll('[data-history-id]').forEach(button=>button.addEventListener('click',()=>{
        if(active && active!==button.dataset.historyId) {message.textContent='実行中の評価を確認してから過去の記録を開いてください。';return;}
        cancel(timer); showRun(button.dataset.historyId).catch(()=>{message.textContent='保存した評価を開けませんでした。';});
      }));
      const id=active || selectedRun || data.runs?.[0]?.id;
      if(id) await showRun(id,token);
      controls();
    } catch(error) {message.textContent=error.message;}
  }
  provider.addEventListener('change',()=>{confirm.checked=false;consentKey=null;controls();});
  repeats.addEventListener('change',()=>{confirm.checked=false;consentKey=null;controls();});
  confirm.addEventListener('change',()=>{consentKey=confirm.checked ? key() : null;controls();});
  q('[data-refresh]').addEventListener('click',refresh);
  q('[data-eval-form]').addEventListener('submit',async event=>{
    event.preventDefault();
    if(busy || active)return;
    try {
      if(!pending){
        if(provider.value && (!confirm.checked || consentKey!==key())){
          confirm.checked=false;consentKey=null;controls();throw new Error('評価条件を確認し、この1回の外部送信にチェックしてください。');
        }
        if(provider.value && !paidReady())throw new Error('接続先・単価・予算を確認できないため外部送信できません。');
        pending=evaluationPayload(catalog,provider.value,repeats.value,confirm.checked,makeId());
      }
    }
    catch(error){message.textContent=error.message;return;}
    busy=true;controls();message.textContent='評価を開始しています…';
    try {
      const data=await request('/api/ai/evaluation/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pending)});
      if(!data?.id || !['running','completed','failed','interrupted'].includes(data.status))throw new Error('invalid start response');
      pending=null; confirm.checked=false; consentKey=null; selectedRun=data.id;
      active=data.status==='running' ? data.id : null;
      await refresh();
    } catch(error) {
      if(error.confirmedResponse){pending=null;confirm.checked=false;consentKey=null;}
      message.textContent=error.confirmedResponse ? error.message : '開始状況を確認できません。同じ実行IDで確認すれば重複実行されません。';
    } finally {busy=false;controls();}
  });
  await refresh();
}
