// Shared fixed-K diagnostics. Rankings here are evaluation subjects, not advice.
const esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = (v,suffix='') => v == null || !Number.isFinite(v) ? '未計測' : `${v.toFixed(1)}${suffix}`;
const poolLabel = p => p === 'eligible' ? '現行判定通過の候補だけ' : '参考・見送りも含む比較用';

function table(metrics, common=false) {
  return `<div class="benchmark-table-scroll" tabindex="0" aria-label="候補数別の成績・横スクロールできます"><table><caption>${common ? '同じ日だけで1・3・5件を比較（対象全件を照合した日）' : '候補数と照合状況'}</caption><thead><tr><th scope="col">上位</th><th scope="col">${common?'比較日':'候補が出た日'}</th><th scope="col">${common?'選択候補のプラス率':'候補全件照合日'}</th><th scope="col">${common?'絞らない場合のプラス率':'未照合件数'}</th><th scope="col">${common?'選択候補の平均差枚':'日数・鮮度の確認'}</th></tr></thead><tbody>
    ${metrics.map(m=>`<tr><th scope="row">${esc(m.k)}件</th><td>${esc(common?m.common_days:m.candidate_days)}日</td><td>${common?num(m.common_positive_pct,'%'):`${esc(m.complete_days)}日`}</td><td>${common?num(m.pool_positive_pct,'%'):`${esc(m.pending)}件`}</td><td>${common?num(m.common_avg_diff_coins,'枚'):(m.status==='hold'?'不足・保留':'参考評価のみ')}</td></tr>`).join('')}</tbody></table></div>`;
}

export function candidateMarkup(report) {
  if (!report) return '<p>結果をまだ取得できていません。</p>';
  return `<p>評価期間 ${esc(report.window_start)}〜${esc(report.window_end)}<br>検証可能な保存 ${esc(report.recorded_days)}日 ／ 未保存・対象外 ${esc(report.unassessed_calendar_days)}日</p>
    <p>${esc(report.notice)}</p><p>${esc(report.missing_notice)}</p>
    ${['prospective','research'].map(mode=>`<details ${mode==='prospective'?'open':''}><summary>${mode==='prospective'?'事前固定した1・3・5件の成績':'参考：旧予測を今回のルールで再集計'}</summary>
      <p>${mode==='prospective'?'選び方も結果が出る前に保存した対象だけです。':'予測値自体は事前保存済みですが、上位の選び方は後付けです。事前ルールの成績や実戦実績へ合算しません。'}</p>
      ${(report.items || []).filter(r=>r.mode===mode).map(r=>`<details class="model-review-item"><summary>${r.scope==='seat'?'台番号':'機種平均'}・${esc(r.label)} ／ ${poolLabel(r.pool)} ／ v${esc(r.version)}</summary>
        ${table(r.metrics)}${table(r.metrics,true)}
        <p>同じ対象の4方式が揃わないため除外 ${esc(r.excluded_missing_models)}件 ／ 現行判定を通過しなかった比較対象 ${esc(r.withheld)}件。比較上位は着席許可ではありません。</p>
        ${r.metrics.map(m=>`<details><summary>上位${esc(m.k)}件の内訳と不足理由</summary><p>保存 ${esc(m.saved_days)}日 ／ 算出0 ${esc(m.no_candidate_days)}日 ／ ${esc(m.k)}件未満 ${esc(m.shortfall_days)}日<br>選択 ${esc(m.selected)}件・照合 ${esc(m.answered)}件・未照合 ${esc(m.pending)}件<br>一部未照合 ${esc(m.incomplete_days)}日 ／ 境界同点 ${esc(m.tie_boundary_days)}日</p>
          <p>この候補数だけで全件照合できた${esc(m.complete_days)}日のプラス率 ${num(m.positive_pct,'%')}、平均差枚 ${num(m.avg_diff_coins,'枚')}。他の候補数と比較する際は上の「同じ日だけ」の表を使ってください。</p>
          <p>${(m.reasons || []).map(esc).join('／') || '集計日数の条件は通過。統計的な優位性や将来利益は未保証です。'}</p><p>${Object.entries(m.unresolved_reasons || {}).map(([k,v])=>`${esc(k)} ${esc(v)}件`).join('／')}</p></details>`).join('')}
        <details><summary>検証用の固定候補を見る（直近3日・最大5件）</summary><p>過去の評価対象です。今日の狙い台や3.28の研究順位ではありません。</p>${(r.recent || []).map(d=>`<p><b>${esc(d.date)}</b><br>${(d.rows || []).map((s,i)=>`${i+1}位 ${esc(s.key[1])}・${esc(s.key[2])}${s.key[0]==='seat'?`・${esc(s.key[3])}番台`:''}<br>予測${num(s.projected,'枚')} → ${s.actual==null?esc(s.unresolved_reason || '未照合'):num(s.actual,'枚')}${s.eligible?'':'（現行判定未通過）'}`).join('<br>') || '算出候補なし'}</p>`).join('')}</details>
        <small>実装識別 ${esc(r.source_hash)}</small></details>`).join('') || '<p>この区分で検証できる記録はまだありません。</p>'}</details>`).join('')}
    <p>${esc(report.scope_notice)}</p><p>${Object.entries(report.exclusions || {}).map(([k,v])=>`${esc(k)} ${esc(v)}日分`).join('／')}</p>`;
}

export async function mountCandidateEvaluation(element, request) {
  element.innerHTML = `<details class="model-review candidate-evaluation" open><summary>候補を何件に絞る？（3.32）</summary><p><b>上位1・3・5件を同じ日で比較します。</b>投資額・遊技時間を再現した利益計算ではありません。実戦の順位や判定は変更しません。</p>
    <div data-candidate-body role="status">事前記録を確認中…</div><button type="button" data-candidate-refresh>候補数の成績を更新</button><p data-candidate-message role="status"></p>
    <details><summary>選び方と集計の条件</summary><p>同じ対象で4方式が揃ったものを、予測差枚→確率の降順に整列。同点は店舗・機種・台番号順です。候補が足りなければある分だけ選び、不足日として残します。</p><p>候補全件を照合した30日・8週、照合率80%以上、直近14日の照合を確認します。通過しても自動採用はしません。機種平均と台番号、方式・版・実装は分けて評価します。</p></details></details>`;
  const button=element.querySelector('[data-candidate-refresh]'), body=element.querySelector('[data-candidate-body]'), message=element.querySelector('[data-candidate-message]');
  let busy=false;
  async function refresh() {
    if (busy) return;
    busy=true; button.disabled=true;
    try {
      const report=await request('/api/predictions/candidate_evaluation');
      if (!element.isConnected) return;
      body.innerHTML=candidateMarkup(report);
      message.textContent='保存済みの予測と結果から集計しました。再検索・着席判定の変更はしていません。';
    } catch (error) {
      body.textContent='成績を取得できませんでした。';
      message.textContent=`確認できません：${error.message}。通信とサーバーの更新を確認し、再確認してください。`;
    } finally {busy=false; button.disabled=false;}
  }
  button.onclick=refresh;
  await refresh();
}
