import {renderAiEvidence} from './ai-evidence.mjs?v=3.49.1';

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const HALL_TOPICS = [['auto','質問から選ぶ'], ['overview','店舗の要点'], ['weekday','曜日'],
  ['digit','日付末尾'], ['machine','機種'], ['event','イベント'], ['seat','台番号の履歴'],
  ['layout','島・配置'], ['comparison','他店との比較']];

export function hallQuestionPayload(context, message, topic, machineName, useExternal=false, confirmed=false, requestId='') {
  if (!context.hall_name?.trim() || context.hall_name === '全店舗') throw new Error('上の欄で店舗を1店選んでください。');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(context.visit_date || '')) throw new Error('上の欄で行く日を選んでください。');
  return {hall_name: context.hall_name, visit_date: context.visit_date, days: Number(context.days || 90),
    message: message.trim() || 'この店の傾向は？', topic, machine_name: machineName.trim(),
    request_id:requestId || globalThis.crypto?.randomUUID?.(), use_external_ai:Boolean(useExternal),
    confirm_public_data_only:Boolean(useExternal && confirmed)};
}

export function renderHallAnswer(data) {
  return `<p>${esc((data.topic_labels || []).join(' / '))}</p>
    <p style="white-space:pre-wrap">${esc(data.summary || '説明を取得できませんでした。')}</p>
    <small>${esc(data.engine || '統計エンジン')} · 店舗 ${esc(data.scope)} · 対象日 ${esc(data.target_date)}</small>
    ${renderAiEvidence(data)}`;
}

export function mountHallAi(root, {getContext, apiUrl = path => path, fetcher = fetch,
  makeId = () => globalThis.crypto.randomUUID()}) {
  if (!root || root.dataset.mounted) return;
  root.dataset.mounted = 'true';
  root.innerHTML = `<h3>この店舗について質問</h3><p>上で選んだ店舗・行く日・分析期間を使います。公開実績と、分からないことを根拠付きで確認できます。</p>
    <form data-hall-ai-form style="display:grid;gap:12px">
      <div style="display:flex;flex-wrap:wrap;gap:8px">
        <button type="button" data-topic="machine" data-question="どの機種を強く扱っている？">強い機種</button>
        <button type="button" data-topic="weekday" data-question="何曜日が高め？">強い曜日</button>
        <button type="button" data-topic="event" data-question="登録イベントは熱い？">イベント</button>
        <button type="button" data-topic="seat" data-question="台番号の履歴を教えて">台番号の履歴</button>
        <button type="button" data-topic="layout" data-question="島ごとの傾向は？">島・配置</button>
      </div>
      <label>調べる項目<select data-topic-select style="width:100%;min-height:44px">${HALL_TOPICS.map(([v,l])=>`<option value="${v}">${l}</option>`).join('')}</select></label>
      <label>機種を限定する場合<input data-machine placeholder="正式な機種名（空欄なら全機種）" maxlength="120" style="width:100%;min-height:44px"></label>
      <label>聞きたいこと<textarea data-question-input rows="2" maxlength="2000" placeholder="この店は月曜日の傾向が強い？" style="width:100%"></textarea></label>
      <label><input type="checkbox" data-use-external> 外部AIをこの1回だけ使う（未選択なら無料の統計説明）</label>
      <p>外部AIへ送るのは選択した店舗・日付・機種などの公開根拠と、この質問文です。個人の遊技履歴や過去の会話は送りません。質問に氏名・連絡先・収支・認証情報を入力しないでください。</p>
      <p data-budget>初期設定はローカル処理です。外部AIを選ぶと接続先と料金上限を確認します。</p>
      <label data-external-confirm hidden><input type="checkbox" data-confirm> 上記の公開根拠とこの質問文だけを送信し、料金上限内のAPI料金が発生する可能性を確認しました（この1回のみ）</label>
      <button type="submit" data-send style="min-height:44px">根拠から調べる</button>
    </form>
    <div data-result role="status" aria-live="polite" style="margin-top:12px;overflow-wrap:anywhere"></div>`;
  const form = root.querySelector('form');
  const input = root.querySelector('[data-question-input]');
  const select = root.querySelector('[data-topic-select]');
  const result = root.querySelector('[data-result]');
  const buttons = [...root.querySelectorAll('button')];
  const machine=root.querySelector('[data-machine]');
  const send=root.querySelector('[data-send]');
  let busy=false, pending=null, consentKey=null, status=null, loadingStatus=false;
  const useExternal=root.querySelector('[data-use-external]');
  const confirm=root.querySelector('[data-confirm]');
  const key=()=>JSON.stringify(hallQuestionPayload(getContext(),input.value,select.value,machine.value,
    Boolean(useExternal.checked),false,'conditions'));
  const paidReady=()=>{
    const budget=status?.governance?.providers?.find(item=>item.provider===status.provider);
    return Boolean(status?.available && budget?.ready_for_paid_calls && budget.remaining_usd>0);
  };
  function controls() {
    root.querySelector('[data-external-confirm]').hidden=!useExternal.checked;
    [input,select,machine,useExternal,confirm].forEach(item=>{item.disabled=busy || Boolean(pending);});
    buttons.forEach(button=>{button.disabled=busy || Boolean(pending);});
    send.disabled=busy || (!pending && useExternal.checked && (loadingStatus || !paidReady() || !confirm.checked));
    send.textContent=pending ? '同じ実行IDで受付状況を確認' : '根拠から調べる';
  }
  function invalidateConsent() { confirm.checked=false; consentKey=null; controls(); }
  [input,machine].forEach(item=>item.addEventListener('input',invalidateConsent));
  [select,machine].forEach(item=>item.addEventListener('change',invalidateConsent));
  confirm.addEventListener('change',()=>{
    try {consentKey=confirm.checked ? key() : null;}
    catch(error){invalidateConsent();result.textContent=error.message;}
    controls();
  });
  useExternal.addEventListener('change',async()=>{
    invalidateConsent();
    if(!useExternal.checked)return;
    loadingStatus=true;status=null;controls();
    const budgetLabel=root.querySelector('[data-budget]');
    budgetLabel.textContent='外部AIの接続先・予算を確認しています…';
    try {
      const response=await fetcher(apiUrl('/api/ai/status'));
      if(!response.ok)throw new Error('接続先・予算を確認できないため外部送信できません。');
      status=await response.json();
      const budget=status.governance?.providers?.find(item=>item.provider===status.provider);
      budgetLabel.textContent=`送信先 ${status.provider_label || status.provider || '未設定'}・${status.model || 'モデル未設定'}。月上限 ${budget?.monthly_limit_usd == null ? '未設定' : '$'+budget.monthly_limit_usd} / 予約済み ${budget?.month_reserved_usd == null ? '不明' : '$'+budget.month_reserved_usd} / 残り ${budget?.remaining_usd == null ? '不明' : '$'+budget.remaining_usd}。${paidReady() ? '1回の外部呼び出し予定。送信時にも予算を確認します。' : '接続・単価・予算の条件が不足しているため実行できません。'}`;
    } catch(error){status=null;budgetLabel.textContent=error.message;}
    finally{loadingStatus=false;controls();}
  });
  root.querySelectorAll('[data-topic]').forEach(button => button.addEventListener('click', () => {
    if(busy || pending)return;
    select.value = button.dataset.topic;
    input.value = button.dataset.question;
    invalidateConsent();
    if(useExternal.checked) result.textContent='質問を入力しました。内容を確認して、この1回の外部送信にチェックしてください。';
    else form.requestSubmit();
  }));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if(busy)return;
    try {
      if(!pending){
        if(useExternal.checked && (!confirm.checked || consentKey!==key())){
          invalidateConsent();result.textContent='店舗・日付・質問条件と料金を確認し、この1回の外部送信にチェックしてください。';return;
        }
        if(useExternal.checked && !paidReady()){result.textContent='接続先・単価・予算を確認できないため外部送信できません。';return;}
        pending=hallQuestionPayload(getContext(),input.value,select.value,machine.value,
          useExternal.checked,confirm.checked,makeId());
      }
    }
    catch (error) { result.textContent = error.message; return; }
    const payload=pending;
    busy=true;controls();
    const requestController = new AbortController();
    const timeout = setTimeout(() => requestController.abort(), 135000);
    result.textContent = `${payload.hall_name}・${payload.visit_date}の根拠を確認しています…`;
    try {
      const response = await fetcher(apiUrl('/api/ai/hall_ask'), {method:'POST',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload), signal:requestController.signal});
      const data = await response.json();
      if (!response.ok) {
        const error=new Error(typeof data?.detail==='string' ? data.detail : '説明を取得できませんでした。入力内容を確認してください。');
        error.confirmedResponse=response.status>=400 && response.status<500 && response.status!==408;
        throw error;
      }
      if(typeof data?.summary!=='string')throw new Error('invalid answer response');
      pending=null;confirm.checked=false;consentKey=null;useExternal.checked=false;
      const current = getContext();
      if (current.hall_name !== payload.hall_name || current.visit_date !== payload.visit_date || Number(current.days || 90) !== payload.days
          || select.value !== payload.topic || machine.value.trim() !== payload.machine_name
          || (input.value.trim() || 'この店の傾向は？') !== payload.message) {
        result.textContent = '店舗・日付・質問条件が変更されたため、古い回答は表示しません。もう一度調べてください。';
        return;
      }
      result.innerHTML = renderHallAnswer(data);
    } catch (error) {
      if(error.confirmedResponse){pending=null;confirm.checked=false;consentKey=null;useExternal.checked=false;}
      result.textContent=error.confirmedResponse ? error.message : '受付状況が不明です。条件を変えず「同じ実行ID」で確認してください。新しい実行は開始しません。';
    } finally {
      clearTimeout(timeout);
      busy=false;controls();
    }
  });
  controls();
}
