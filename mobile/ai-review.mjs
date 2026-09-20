const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const colors = {adopted:'#7ee2b8',assistance_only:'#9fbaff',pending:'#f3ce7a',rejected:'#f5a6a6'};
const money = value => Number.isFinite(value) ? '$'+value : '未設定';

export function renderAiReview(data) {
  const v=data.verification || {};
  return `<p><b>${esc(data.overall_label)}</b> · v${esc(data.app_version)}</p>
    <p>${esc(data.notice)}</p>
    <p>普段の説明：${esc(data.active_explanation)}<br>採用済み外部AI：${esc(data.adopted_external_provider || 'なし')}</p>
    <p role="status">${esc(v.reason)}</p>
    ${v.valid ? `<small>自動点検 ${esc(v.verified_at)} · Python ${esc(v.python_passed)}件 / 画面テスト ${esc(v.ui_passed)}本。実端末試験は別途必要です。</small>` : ''}
    <div style="display:grid;gap:10px;margin-top:12px">${(data.features || []).map(item=>`
      <article style="padding:12px;border:1px solid #64748b;border-radius:10px">
        <div style="display:flex;gap:12px;justify-content:space-between;align-items:center;flex-wrap:wrap"><b>${esc(item.label)}</b>
          <span style="color:${colors[item.status] || colors.pending}">${esc(item.status_label)}</span></div>
        <p>${esc(item.scope)}</p><small>次の確認：${esc(item.next_step)}</small>
      </article>`).join('')}</div>
    <details style="margin-top:12px"><summary style="min-height:44px">外部AIの審査と費用設定</summary>
      <p>費用設定がそろっていても、AIの品質審査を通過した意味ではありません。</p>
      ${(data.providers || []).map(p=>`<p><b>${esc(p.label)}</b>：${esc(p.status_label)}（${esc(p.external_performance)}）<br>
        月枠 ${money(p.monthly_limit_usd)} / 使用・予約 ${money(p.month_reserved_usd)} / 残り ${money(p.remaining_usd)}<br>
        単価：${p.price_configured ? '設定済み' : '未設定'}</p>`).join('') || '<p>設定を確認できません。</p>'}
    </details>
    <details><summary style="min-height:44px">使い方・停止・今後の確認</summary>
      <ul>${(data.switch_policy || []).map(text=>`<li>${esc(text)}</li>`).join('')}</ul>
      <p>未確認の項目</p><ul>${(data.unverified || []).map(text=>`<li>${esc(text)}</li>`).join('')}</ul>
    </details>
    ${v.changed_files?.length ? `<details><summary style="min-height:44px">再点検が必要なファイル</summary><ul>${v.changed_files.map(name=>`<li>${esc(name)}</li>`).join('')}</ul></details>` : ''}`;
}

export async function mountAiReview(root,{apiUrl=path=>path,fetcher=fetch}={}) {
  if(!root || root.dataset.mounted)return;
  root.dataset.mounted='true';
  root.innerHTML='<h3>AI機能の審査結果</h3><p>機能ごとに、使える範囲と残る確認をまとめています。</p><button type="button" data-refresh style="min-height:44px">審査状況を更新</button><div data-result aria-live="polite" style="overflow-wrap:anywhere"></div>';
  const button=root.querySelector('[data-refresh]'),result=root.querySelector('[data-result]');
  let busy=false;
  async function refresh(){
    if(busy)return;
    busy=true;button.disabled=true;result.textContent='審査記録を確認しています…';
    try{
      const response=await fetcher(apiUrl('/api/ai/review'),{cache:'no-store'});
      if(!response.ok)throw new Error('審査記録を取得できません。接続先の更新状況を確認してください。');
      result.innerHTML=renderAiReview(await response.json());
    }catch(error){result.textContent=error.message || '審査記録を確認できません。';}
    finally{busy=false;button.disabled=false;}
  }
  button.addEventListener('click',refresh);
  await refresh();
}
