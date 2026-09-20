// Shared read-only diagnostics. Never adjusts displayed live predictions.
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = (v, suffix='', digits=1) => v == null || !Number.isFinite(v) ? '未計測' : `${v.toFixed(digits)}${suffix}`;

function intervalText(value) {
  return `照合 ${esc(value?.count ?? 0)}件・${esc(value?.days ?? 0)}日 ／ 幅内 ${num(value?.coverage_pct,'%')}<br>平均幅 ${num(value?.mean_width,'枚')} ／ 幅と外れの総合誤差 ${num(value?.interval_score)}`;
}

export function probabilityMarkup(report) {
  if (!report) return '<p>まだ確認できていません。</p>';
  return `<p>評価期間 ${esc(report.window_start)}〜${esc(report.window_end)}<br>${esc(report.notice)}</p>
    ${(report.items || []).map(r => `<article class="model-review-item"><h4>${r.scope === 'seat' ? '台番号' : '機種平均'}・${esc(r.label)} ／ v${esc(r.version)}</h4>
      <p><b>${r.status === 'reference_improved' ? '参考改善あり・実戦未適用' : '補正の評価は保留'}</b><br>${(r.reasons || []).map(esc).join('／')}</p>
      <p>保存 ${esc(r.saved)}件 ／ 照合 ${esc(r.resolved)}件・${esc(r.days)}日 ／ 未照合 ${esc(r.pending)}件<br>試験補正の事前保存 ${esc(r.shadow_saved)}件 ／ 前後比較 ${esc(r.paired)}件・${esc(r.paired_days)}日・${esc(r.paired_weeks)}週</p>
      <p>同じ対象での確率誤差：${num(r.paired_before_brier,'',4)} → ${num(r.paired_after_brier,'',4)}<br><small>Brier：0に近いほど良好。現行方式の全照合対象：${num(r.raw_brier,'',4)}（比較対象数は異なります）</small></p>
      <details><summary>予測した確率と実際のプラス率</summary><p>棒は現行予測の確率帯ごとの実績です。日・店舗を等重みで集計。少数日は精度の証明ではありません。</p>
        ${(r.bands || []).map(b => `<div class="calibration-band"><b>${esc(b.low_pct)}〜${esc(b.high_pct)}%帯</b> ／ ${esc(b.count)}件・${esc(b.days)}日
          <label>予測 ${num(b.predicted_pct,'%')} ${b.predicted_pct == null ? '' : `<meter min="0" max="100" value="${Math.max(0,Math.min(100,Number(b.predicted_pct)||0))}" aria-label="${esc(b.low_pct)}〜${esc(b.high_pct)}%帯の予測確率"></meter>`}</label>
          <label>実績 ${num(b.actual_pct,'%')} ${b.actual_pct == null ? '' : `<meter min="0" max="100" value="${Math.max(0,Math.min(100,Number(b.actual_pct)||0))}" aria-label="${esc(b.low_pct)}〜${esc(b.high_pct)}%帯の実際のプラス率"></meter>`}</label></div>`).join('')}
      </details>
      <details><summary>80%の予測幅は実際に当たっている？</summary><p><b>試験幅（事前保存した対象のみ）</b><br>${intervalText(r.candidate_interval)}</p><p><b>既存幅（保存済みの対象のみ）</b><br>${intervalText(r.existing_interval)}</p>
        <p>既存幅が保存されていない旧予測・台番号には後付けしません。幅を広げるだけでも収まりやすくなるため、幅の広さと外れの大きさも確認します。</p>
        <details><summary>同じ対象に両方の幅がある場合の比較</summary><p>既存：${intervalText(r.paired_intervals?.before)}</p><p>試験：${intervalText(r.paired_intervals?.after)}</p></details>
      </details><details><summary>対象と未照合理由</summary><p>実装識別 ${esc(r.source_hash)}<br>${Object.entries(r.unresolved_reasons || {}).map(([k,v])=>`${esc(k)} ${esc(v)}件`).join('／') || '未照合理由なし'}</p></details></article>`).join('') || '<p>比較用の事前保存データはまだありません。「明日の予測を固定・結果を照合」から蓄積を始められます。旧結果を後付けで補正済み成績にはしません。</p>'}
    <p>${esc(report.scope_notice)}</p><p>${esc(report.interval_notice)}</p>
    <p>${Object.entries(report.exclusions || {}).map(([k,v])=>`${esc(k)} ${esc(v)}日分`).join('／')}</p>`;
}

export async function mountProbabilityValidation(element, request) {
  element.innerHTML = `<details class="model-review probability-validation" open><summary>予測確率と予測幅の検証（3.31）</summary>
    <p><b>実戦の予測は変更しません。</b>「70%」が本当に7割程度か、予測幅が狭すぎないかを事前成績で確認します。高設定的中率・あなたの勝率ではありません。</p>
    <div data-probability-body role="status">保存した成績を確認中…</div><button type="button" data-probability-refresh>確率・予測幅の結果を更新</button><p data-probability-message role="status"></p>
    <details><summary>補正の条件・時点の制限</summary><p>同じ版・実装・方式・機種平均／台番号に分け、直近120日から予測開始前に答えが判明した結果だけを使います。確率補正には全体30日・確率帯10日以上、予測幅には30日以上が必要です。</p><p>前後比較30日・8週、照合率80%、直近14日以内の結果、プラス・非プラス各5日を確認。それでも前半・後半の改善は参考情報であり、採用や将来の利益を保証しません。</p></details></details>`;
  const button = element.querySelector('[data-probability-refresh]');
  const body = element.querySelector('[data-probability-body]');
  const message = element.querySelector('[data-probability-message]');
  let busy = false;
  async function refresh() {
    if (busy) return;
    busy = true; button.disabled = true;
    try {
      const result = await request('/api/predictions/probability_validation');
      if (!element.isConnected) return;
      body.innerHTML = probabilityMarkup(result);
      message.textContent = '保存済み成績を読み込みました。再検索・再学習・着席判定の変更はしていません。';
    } catch (error) {
      message.textContent = `確認できません：${error.message}。通信とサーバーの更新を確認してください。`;
      body.textContent = '成績を取得できませんでした。下のボタンから再確認できます。';
    } finally {
      busy = false; button.disabled = false;
    }
  }
  button.onclick = refresh;
  await refresh();
}
