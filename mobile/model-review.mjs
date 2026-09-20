// Small shared panel; no search/replay and no automatic adoption from the UI.
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = (v, suffix='') => v == null || !Number.isFinite(Number(v)) ? '未計測' : `${Number(v).toFixed(1)}${suffix}`;
const labels = {hold:'保留', reject:'却下', adoptable:'採用可・未適用'};
const baseline = {baseline:'単純な基準方式',current:'現行方式'};

export function reviewMarkup(report) {
  if (!report) return '<p>まだ審査していません。下のボタンで、保存済みの事前成績だけを審査します。</p>';
  return `<p>固定評価期間 ${esc(report.window_start)}〜${esc(report.window_end)} ／ 審査#${esc(report.id)}<br>保存 ${esc(report.created_at)}</p>
    ${report.current_implementation === false ? '<p role="alert">これは以前の実装の審査結果です。現在のコードには適用できません。</p>' : ''}
    ${(report.items || []).map(r=>`<article class="model-review-item"><h4>${esc(r.label)}：${esc(labels[r.decision] || '保留')}</h4>
      <p>${(r.reasons || []).map(esc).join('／')}</p>
      <p>事前予測 ${esc(r.saved)}件 ／ 同条件比較 ${esc(r.comparable)}件 ／ 照合 ${esc(r.resolved)}件・待ち ${esc(r.pending)}件<br>${esc(r.metrics?.days || 0)}日・${esc(r.metrics?.weeks || 0)}週 ／ 照合率 ${num((r.coverage || 0)*100,'%')}</p>
      <details><summary>誤差・改善幅と未照合理由</summary><p>新方式の誤差 ${num(r.metrics?.candidate_mae,'枚')}</p>
        ${(r.metrics?.comparisons || []).map(c=>`<p>${esc(baseline[c.against])}の誤差 ${num(c.mae,'枚')}<br>改善幅 ${num(c.improvement,'枚')} ／ 週再標本化の下限 ${num(c.lower_bound,'枚')}<br>前半 ${num(c.first_half,'枚')} ／ 後半 ${num(c.second_half,'枚')}</p>`).join('')}
        <p>${Object.entries(r.unresolved_reasons || {}).map(([k,v])=>`${esc(k)} ${esc(v)}件`).join('／') || '未照合理由なし'}</p></details></article>`).join('')}
    <p>${esc(report.notice)}</p><p>${Object.entries(report.exclusions || {}).map(([k,v])=>`${esc(k)} ${esc(v)}件`).join('／')}</p>`;
}

export function researchMarkup(research) {
  return `<details><summary>過去データでの参考比較（採用成績と分離）</summary><p>${esc(research?.notice)}</p>
    ${(research?.items || []).map(r=>`<p><b>${esc(r.label)}</b>：${esc(r.resolved)}件・${esc(r.days)}日<br>基準誤差 ${num(r.baseline_mae,'枚')} → 新方式 ${num(r.candidate_mae,'枚')}<br>未照合 ${esc(r.unresolved)}件 ／ 不正・矛盾 ${esc(r.invalid)}件</p>`).join('') || '<p>研究比較はまだありません。</p>'}</details>`;
}

export async function mountModelReview(element, request, research) {
  element.innerHTML = `<details class="model-review" open><summary>予測モデルの採用審査（3.30）</summary>
    <p><b>現行方式を維持しています。</b>過去に好成績だっただけでは切り替えません。「採用可」も実戦への適用とは別です。</p>
    <div data-review-body role="status">保存済み審査を確認中…</div>
    <button type="button" data-review-run>事前成績を審査して保存</button> <button type="button" data-review-refresh>保存結果を更新</button>
    <p data-review-message role="status"></p>
    <details><summary>審査基準・対象・限界</summary><p>直前の完了した日曜日まで84日。同じ実装・四條畷周辺の全保存対象を比較。30日・8週、照合率80%以上、期間末尾14日以内の照合実績が必要です。</p>
    <p>単純な基準方式と現行方式の両方に対し、平均誤差を50枚以上かつ5%以上改善。前半・後半の両方で改善し、週単位で再標本化した改善幅の下限も0超が必要です。週の相関や掲載の偏りが消えるわけではありません。</p>
    <p>店舗イベントは同じ対象の現行予測が未整備のため保留。台番号は差枚誤差の審査で、順位1位の的中率・勝率を証明するものではありません。確率校正と実戦切替は別途確認します。</p></details>
    ${researchMarkup(research)}</details>`;
  const body = element.querySelector('[data-review-body]');
  const message = element.querySelector('[data-review-message]');
  const run = element.querySelector('[data-review-run]');
  const refresh = element.querySelector('[data-review-refresh]');
  let busy = false;
  async function load(save=false) {
    if (busy) return;
    busy = true; run.disabled = refresh.disabled = true;
    message.textContent = save ? '保存済み予測と結果を審査中…（過去検索は再実行しません）' : '';
    try {
      const result = await request('/api/predictions/model_review', save ? {method:'POST'} : undefined);
      if (!element.isConnected) return;
      body.innerHTML = reviewMarkup(result.report);
      message.textContent = save ? '審査を保存しました。同じ根拠の再実行では重複保存しません。実戦判定は変更していません。' : '';
    } catch (error) {
      message.textContent = `審査を確認できません：${error.message}。通信とサーバーの更新を確認してください。`;
      if (!save) body.textContent = '保存済み結果を取得できませんでした。';
    } finally {
      busy = false; run.disabled = refresh.disabled = false;
    }
  }
  run.onclick = () => load(true);
  refresh.onclick = () => load();
  await load();
}
