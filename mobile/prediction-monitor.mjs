// Shared, read-only monitoring. No polling or automatic prediction changes.
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = (v, digits=1) => Number.isFinite(v) ? v.toFixed(digits) : '未計測';
const status = s => ({review:'要確認', insufficient_data:'データ不足・判定保留', no_alert:'固定基準の警告なし'})[s] || '未確認';

function comparison(item) {
  const metrics = [['brier','確率の誤差（小さい方が良い）',3], ['mae_coins','差枚の平均誤差（枚）',1],
    ['avg_diff_coins','共通対象の平均差枚（枚）',1], ['positive_pct','共通対象のプラス率（%）',1]];
  return `<div class="benchmark-table-scroll" tabindex="0" aria-label="以前と直近の成績・横スクロールできます"><table><caption>同じ機種・台番号で比較（日を等重み）</caption><thead><tr><th scope="col">指標</th><th scope="col">以前56日</th><th scope="col">直近28日</th><th scope="col">変化量</th></tr></thead><tbody>${metrics.map(([key,label,digits])=>`<tr><th scope="row">${label}</th><td>${num(item.baseline.metrics[key],digits)}</td><td>${num(item.recent.metrics[key],digits)}</td><td>${num(item.delta[key],digits)}</td></tr>`).join('')}</tbody></table></div>`;
}

function windowMarkup(w, label) {
  return `<p><b>${label}：${esc(w.start)}〜${esc(w.end)}</b><br>全件照合 ${esc(w.complete_days)}日 ／ 保存 ${esc(w.saved_days)}日 ／ 未保存 ${esc(w.unrecorded_days)}日<br>保存された日で全件未照合 ${esc(w.incomplete_days)}日 ／ 共通対象の結果待ち ${esc(w.pending)}件<br>共通対象の照合率 ${num(w.resolution==null?null:w.resolution*100)}% ／ 保存件数に占める共通対象 ${num(w.panel_share==null?null:w.panel_share*100)}%<br>最終全件照合 ${esc(w.latest_complete_date || 'なし')}</p><p>${Object.entries(w.unresolved_reasons || {}).map(([k,v])=>`${esc(k)} ${esc(v)}件`).join('／')}</p>`;
}

export function monitorMarkup(report) {
  if (!report) return '<p>監視結果をまだ取得できていません。</p>';
  const items=report.items || [];
  const halls=[...new Set(items.map(r=>r.hall_name))];
  return `<p>対象期間 ${esc(report.window_start)}〜${esc(report.window_end)}<br>検証可能な保存 ${esc(report.recorded_days)}日 ／ 未保存・対象外 ${esc(report.unassessed_calendar_days)}日</p>
    <p><b>要確認 ${items.filter(r=>r.status==='review').length}区分 ／ データ不足 ${items.filter(r=>r.status==='insufficient_data').length}区分 ／ 固定基準の警告なし ${items.filter(r=>r.status==='no_alert').length}区分</b></p>
    <p>${esc(report.notice)}</p>
    ${halls.map(hall=>`<details class="model-review-item"><summary>${esc(hall)}：${items.filter(r=>r.hall_name===hall && r.status==='review').length}区分が要確認</summary>
      ${items.filter(r=>r.hall_name===hall).map(r=>`<details><summary>${r.scope==='seat'?'台番号':'機種平均'}・${esc(r.label)} ／ ${status(r.status)} ／ v${esc(r.version)}</summary>
        <p>${(r.reasons || []).map(esc).join('／')}</p>
        ${(r.alerts || []).map(a=>`<p><b>${a.kind==='forecast'?'予測誤差':a.metric==='avg_diff_coins'?'平均差枚の変化':'プラス率の変化'}：</b>${esc(a.message)}</p>`).join('')}
        <p>共通対象 ${esc(r.common_subjects)}件。判定保留中の数値も参考表示であり、精度低下を確定したものではありません。</p>
        ${comparison(r)}
        <details><summary>照合状況・警告の根拠</summary>${windowMarkup(r.baseline,'以前56日')}${windowMarkup(r.recent,'直近28日')}
          <p>直近の前半／後半の全件照合：${(r.halves || []).map(h=>`${esc(h.complete_days)}日`).join(' ／ ')}。両方で同じ基準を超えた場合だけ警告します。</p>
          ${(r.halves || []).map((h,i)=>`<p>${i===0?'前半14日':'後半14日'}：確率誤差 ${num(h.metrics.brier,3)} ／ 差枚誤差 ${num(h.metrics.mae_coins)}枚 ／ 平均差枚 ${num(h.metrics.avg_diff_coins)}枚 ／ プラス率 ${num(h.metrics.positive_pct)}%</p>`).join('')}
          <small>実装識別 ${esc(r.source_hash)}</small></details></details>`).join('')}</details>`).join('') || '<p>比較できる事前記録がまだありません。予測悪化や「強い店がない」という意味ではありません。</p>'}
    <p>${esc(report.scope_notice)}</p><p>${esc(report.bias_notice)}</p>
    <p>${Object.entries(report.exclusions || {}).map(([k,v])=>`${esc(k)} ${esc(v)}日分`).join('／')}</p>`;
}

export async function mountPredictionMonitor(element, request) {
  element.innerHTML=`<details class="model-review prediction-monitor" open><summary>店舗傾向・予測の変化を点検（3.33）</summary>
    <p>「予測が外れやすくなった？」と「公開実績が変わった？」を分けて確認します。推奨候補だけでなく、参考・見送りを含む保存対象の点検です。</p>
    <div data-monitor-body role="status">保存記録を確認中…</div><button type="button" data-monitor-refresh>変化を再確認</button><p data-monitor-message role="status"></p>
    <details><summary>比較期間と警告の条件</summary><p>昨日までの直近28日と、その前56日。以前は全件照合28日・6週、直近は14日・3週かつ前半／後半各7日、照合率・共通対象割合とも80%以上、7日以内の全件照合が必要です。</p>
    <p>直近の前半／後半がともに以前より、確率誤差0.05以上、または差枚誤差100枚・20%の大きい方以上悪化したら要確認。公開実績は平均差枚500枚以上、またはプラス率20ポイント以上の同方向変化を別表示します。</p>
    <p>確率誤差は予測確率と実際のプラス／非プラスのずれです。基準は暫定の運用目安で、統計的な確定判定や利益保証ではありません。これは画面を開いた時・再確認時の点検で、常時通知ではありません。</p></details></details>`;
  const body=element.querySelector('[data-monitor-body]'), button=element.querySelector('[data-monitor-refresh]'), message=element.querySelector('[data-monitor-message]');
  let busy=false;
  async function refresh() {
    if (busy) return;
    busy=true; button.disabled=true;
    try {
      const report=await request('/api/predictions/monitor');
      if (!element.isConnected) return;
      body.innerHTML=monitorMarkup(report);
      message.textContent='保存記録を点検しました。予測や着席判定は変更していません。';
    } catch (error) {
      if (!element.isConnected) return;
      body.textContent='監視結果を取得できませんでした。';
      message.textContent=`通信とサーバーの更新を確認して再確認してください：${error.message}`;
    } finally {busy=false; button.disabled=false;}
  }
  button.onclick=refresh;
  await refresh();
}
