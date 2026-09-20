// PC / mobile share the same evidence viewer. Never render model HTML.
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));

function sourceLink(source) {
  try {
    const url = new URL(source.url);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error();
    return `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">${esc(source.label || '出典を開く')}</a>`;
  } catch { return '<span>出典URL未記録</span>'; }
}

export function renderAiEvidence(data) {
  if (!data?.contract_version) return '<small>根拠の詳細は未取得です。サーバーの対応状況を確認してください。</small>';
  const facts = Array.isArray(data.evidence) ? data.evidence : [];
  const ids = new Set((data.claims || []).map(item => item.evidence_id));
  const status = {validated: '根拠照合済み', fallback: '固定説明へ復帰', ai_disabled: data.knowledge_version ? '登録資料の引用' : '統計による説明',
    no_data: '根拠不足', insufficient_evidence: '質問の根拠不足', abstained: 'AIが判断を控えました'}[data.answer_status] || '根拠を確認';
  return `<details class="ai-evidence-panel" style="margin-top:12px;overflow-wrap:anywhere;line-height:1.7">
    <summary style="cursor:pointer;padding:10px 0;min-height:44px">根拠・出典を見る（${facts.length}件） · ${esc(status)}</summary>
    ${data.answer_guard ? `<p>回答前の点検：説明から除外 ${esc(data.answer_guard.excluded_count)}件<br>${(data.answer_guard.reasons || []).map(esc).join(' / ')}<br>${(data.answer_guard.warnings || []).map(esc).join(' / ')}</p>` : ''}
    <p>対象日 ${esc(data.target_date)} / ${esc(data.scope)}<br>説明作成 ${esc(data.generated_at)}<br><small>説明の作成時刻と、元データの取得時刻は別です。</small></p>
    ${facts.map(fact => `<article style="padding:12px 0;border-top:1px solid currentColor">
      <strong>[${esc(fact.id)}] ${esc(fact.hall_name || fact.machine_name || '対象データ')}${ids.has(fact.id) ? ' · 回答で参照' : ''}</strong>
      ${fact.subject_label ? `<p>${esc(fact.subject_label)}</p>` : ''}
      ${fact.comparison_scope ? `<p>比較範囲：${esc(fact.comparison_scope)}</p>` : ''}
      ${fact.interpretation ? `<p>${esc(fact.interpretation)}</p>` : ''}
      ${fact.knowledge ? `<p>資料：${esc(fact.knowledge.title)}<br>資料ID ${esc(fact.knowledge.document_id)}<br>版 ${esc(fact.knowledge.revision)}<br>
        引用元 ${esc(fact.knowledge.source_locator)}<br>確認 ${esc(fact.knowledge.reviewed_on || '未記録')} / 登録基準日 ${esc(fact.knowledge.registered_on)}<br>
        適用開始 ${esc(fact.knowledge.valid_from || '未記録')} / 適用終了 ${esc(fact.knowledge.valid_to || '未記録')}<br>${esc(fact.knowledge.verification)}</p>` : ''}
      <p>${esc([fact.machine_name, fact.seat_number != null ? `${fact.seat_number}番台` : '', fact.event_name].filter(Boolean).join(' / '))}<br>
      対象日 ${esc(fact.target_date || '未記録')}${fact.knowledge ? '' : `<br>集計 ${esc(fact.period?.start || '未記録')}〜${esc(fact.period?.end || '未記録')}`}</p>
      <ul>${(fact.metrics || []).map(item => `<li>${esc(item.label)}：${esc(item.value)}${esc(item.unit)}</li>`).join('')}</ul>
      ${fact.decision ? `<p>既存判定：${esc(fact.decision)}</p>` : ''}
      <p>${esc(fact.source_label)}${fact.kind === 'user_input' ? ' · 入力値、独立検証なし' : ''}</p>
      ${(fact.sources || []).map(source => `<p>${sourceLink(source)}<br>取得 ${esc(source.retrieved_at || '未記録')}</p>`).join('') || (fact.knowledge ? '<p>外部出典なし・上記のプロジェクト内資料を引用</p>' : '<p>出典・取得時刻は未記録です。</p>')}
      ${(fact.missing_information || []).length ? `<p>不足：${esc(fact.missing_information.join(' / '))}</p>` : ''}
    </article>`).join('') || '<p>対象に合う根拠がありません。</p>'}
    <p>${esc((data.missing_information || []).join(' / '))}</p>
  </details>`;
}
