// Shared by PC and PWA. Row membership is explicitly recorded, never inferred.
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const coins = value => value == null ? '未計測' : `${esc(value)}枚`;
const links = urls => [...new Set(urls)].filter(u => /^https?:\/\//i.test(u || '')).slice(0, 5)
  .map(u => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">出典</a>`).join(' ／ ') || '出典URLなし';

export function serializePlacement(seats) {
  return seats.filter(s => s.row_name).map(s => [s.seat_number, s.island_name, s.row_name, s.row_order].join(',')).join('\n');
}

export function applyPlacement(seats, text) {
  const result = seats.map(s => ({...s, row_name: '', row_order: null}));
  const byNumber = new Map(result.map(s => [Number(s.seat_number), s]));
  const used = new Set();
  for (const [i, line] of String(text || '').split(/\r?\n/).entries()) {
    if (!line.trim()) continue;
    const fields = line.split(',').map(s => s.trim());
    const [number, island, row, order] = fields;
    const n = Number(number), position = Number(order);
    if (fields.length !== 4 || !Number.isInteger(n) || !byNumber.has(n) || used.has(n)
        || !island || island.length > 80 || !row || row.length > 80 || !Number.isInteger(position) || position < 1 || position > 1000) {
      throw new Error(`${i+1}行目：登録済み台番号,島名,列名,列内順（1〜1000）を重複なしで入力してください。`);
    }
    used.add(n);
    Object.assign(byNumber.get(n), {island_name: island, row_name: row, row_order: position});
  }
  const positions = result.filter(s => s.row_name).map(s => JSON.stringify([s.island_name, s.row_name, s.row_order]));
  if (new Set(positions).size !== positions.length) throw new Error('同じ島・列の順番が重複しています。');
  return result;
}

function groupMarkup(p) {
  const validation = p.validation || {};
  return `<details class="placement-group"><summary>${p.kind === 'row3' ? '並び3台' : '登録島'} · ${esc(p.floor_name)} ${esc(p.name)} ／ ${esc(p.paired_days ?? '—')}比較日</summary>
    <p class="placement-seats">${(p.seat_numbers || []).map(n => `<span>${esc(n)}番</span>`).join('')}</p>
    <p>同日の他の同機種との差（1台平均）：<b>${coins(p.relative_diff_coins)}</b><br>登録した組の全台が差枚プラス：${esc(p.all_positive_days ?? '—')}/${esc(p.paired_days ?? '—')}日（${p.all_positive_rate_pct == null ? '未計測' : `${esc(p.all_positive_rate_pct)}%`}）</p>
    <p>研究用の予測（1台平均）：${coins(p.candidate?.predicted_coins)}<br>比較用の組平均：${coins(p.candidate?.baseline_coins)} ／ 補正：${coins(p.candidate?.adjustment_coins)}</p>
    <p>${esc((p.blockers || []).join('／') || '研究材料あり・3.30の採用審査待ち')}</p>
    <p>${esc(validation.status)} ／ 答え合わせ${esc(validation.days ?? 0)}日・未照合${esc(validation.unresolved_days ?? 0)}日<br>平均誤差（小さい方が良好）：組平均 ${coins(validation.baseline_mae_coins)} → 新方式 ${coins(validation.candidate_mae_coins)}</p>
    <details><summary>配置と答え合わせの根拠</summary><p>配置の有効日 ${esc(p.layout_valid_from)} ／ 記録時刻 ${esc(p.layout_known_at)}</p>
      ${(validation.trials || []).slice(-5).map(t => `<p>${esc(t.date)}（入力${esc(t.input_cutoff_date)}まで）<br>予測${coins(t.predicted_coins)} → 実際${coins(t.actual_coins)}<br>${esc(t.unresolved_reason)}</p>`).join('') || '<p>答え合わせはまだありません。</p>'}
      <p>${links([p.source_url, ...(p.daily_points || []).flatMap(t => t.source_urls || [])])}</p></details></details>`;
}

export function placementStudyMarkup(studies) {
  if (!studies) return '';
  const labels = {negative: '前日マイナス', positive: '前日プラス', zero: '前日±0'};
  return `<details class="placement-study" open><summary>並び・島・翌日の傾向（3.29・研究用）</summary>
    <p>同じ島・確認した3台組の傾向と、前日から翌日の差枚を見られます。<b>差枚の改善は「設定上げ確定」ではありません。</b></p>
    ${studies.map(s => `<details class="placement-hall"><summary>${esc(s.hall_name)} ／ ${esc(s.candidate_groups)}組を試算・翌日傾向${esc(s.transitions?.length || 0)}区分</summary>
      <p>対象日 ${esc(s.target_date)} ／ 入力期限 ${esc(s.input_cutoff_date)}</p>
      ${Object.keys(s.layout_issues || {}).map(reason => `<p class="placement-empty">${esc(reason)}。「店内」→「店内マップを登録・修正」で島名・列名・列内順を登録できます。</p>`).join('')}
      ${(s.profiles || []).map(groupMarkup).join('') || '<p>並び・島の比較はまだありません。実際の列を確認して登録すると集計が始まります。</p>'}
      <details open><summary>前日から翌日の差枚傾向</summary><p>同じ機種・観測期間の連続する2日、両日1000G以上だけを集計。店舗の設定変更率や本人の勝率ではありません。</p>
        ${(s.transitions || []).map(t => `<article class="placement-transition"><b>${esc(t.machine_name)} ／ ${esc(labels[t.previous_state] || t.previous_state)}</b>
          <div class="placement-rate"><meter min="0" max="100" value="${Math.max(0,Math.min(100,Number(t.positive_rate_pct)||0))}" aria-label="翌日の差枚プラス率"></meter><span>翌日プラス ${esc(t.positive_rate_pct)}%</span></div>
          <p>${esc(t.pairs)}組の連日記録・${esc(t.days)}営業日 ／ ${esc(t.status)}<br>翌日の平均差枚：${coins(t.average_next_coins)}</p>
          <details><summary>連日記録と出典（直近5件）</summary>${(t.observations || []).slice(-5).map(p => `<p>${esc(p.date)} ${esc(p.seat_number)}番 ／ 前日 ${coins(p.previous_coins)} → 翌日 ${coins(p.actual_coins)}</p>`).join('')}<p>${links((t.observations || []).flatMap(p=>p.source_urls||[]))}</p></details></article>`).join('') || '<p>同じ台の連日実績が足りません。欠けた日は負けや0枚に数えません。</p>'}
        <small>${Object.entries(s.transition_exclusions || {}).map(([reason,n])=>`${esc(reason)} ${esc(n)}件`).join(' ／ ')}</small></details>
      <details><summary>設定値の確定記録からの比較</summary><p>比較できる連日記録 ${esc(s.setting_evidence?.pairs || 0)}組 ／ ${esc(s.setting_evidence?.days || 0)}営業日<br>設定値が上昇 ${esc(s.setting_evidence?.higher || 0)}組 ／ 同じ値 ${esc(s.setting_evidence?.same_value || 0)}組 ／ 低下 ${esc(s.setting_evidence?.lower || 0)}組<br>矛盾して集計できない記録 ${esc(s.setting_evidence?.conflicts || 0)}組</p><p>${esc(s.setting_evidence?.notice)}</p></details>
      <details><summary>旧配置の研究成績（${esc(s.retired_group_validations?.length || 0)}組）</summary>${(s.retired_group_validations || []).map(groupMarkup).join('') || '<p>旧配置の比較結果はまだありません。</p>'}</details>
      <p class="placement-notice">${esc(s.notice)}</p></details>`).join('')}</details>`;
}
