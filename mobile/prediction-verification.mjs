// Shared by browser/desktop and iPhone. Never persist API replies in the PWA cache.
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const reasonNames = {missing_diff: '差枚欠損', missing_units: '台数不明', invalid_record: '不正な値・日付', conflicting_rows: '重複の数値矛盾', cutoff_or_future: '入力期限後'};

export function seatHistoryMarkup(history) {
  if (!history) return '';
  const reasons = {first_observation:'初回観測', machine_changed:'機種変更を観測', placement_changed:'配置変更を観測', observation_gap:'30日超の空白', conflict:'同日の情報が矛盾', after_conflict:'矛盾後に再確認', layout_expired:'配置期限後の再確認'};
  const halls = new Map();
  for (const seat of history.seats || []) {
    if (!halls.has(seat.hall_name)) halls.set(seat.hall_name, []);
    halls.get(seat.hall_name).push(seat);
  }
  return `<details class="seat-history"><summary>台番号・配置・入替の観測履歴</summary><p>${esc(history.cutoff_date)}までの情報。旧期間・現状未確認などの${esc(history.excluded_records)}件を台番号予測から分離しました。元の実績は削除していません。機種変更を検知するため、履歴にはスマスロ以外も含みます。</p><p>${esc(history.notice)}</p>
    ${[...halls].map(([hall, seats]) => `<details><summary>${esc(hall)} ／ ${seats.length}席</summary>${seats.map(s => `<details><summary>${esc(s.seat_number)}番台 ${esc(s.machine_name)} ／ ${esc(s.status)}</summary><p>現在期間：${esc(s.training_from)}以降・${esc(s.training_records)}記録（欠損・重複の監査前）<br>最終観測：${esc(s.last_observed)} ／ 期間の切り替わり${esc(s.changes)}回</p>${s.periods.map(p => `<p><b>${esc(p.machine_name)}</b><br>${esc(p.start_observed)}〜${esc(p.last_observed)}の情報 ／ ${esc(reasons[p.reason] || p.reason)}${p.previous_observed ? `<br>前の情報の最終確認：${esc(p.previous_observed)}（この間の正確な変更日は不明）` : ''}${p.position ? `<br>確認配置：${esc(p.position[0])}・${esc(p.position[1])}` : ''}<br><small>${(p.source_urls || []).map(u => /^https?:\/\//i.test(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">出典</a>` : esc(u)).join(' ／ ') || '出典URLなし'}</small></p>`).join('')}</details>`).join('')}</details>`).join('') || '<p>観測履歴はまだありません。</p>'}</details>`;
}

export function qualityMarkup(data) {
  const quality = data.input_quality;
  if (!quality) return '<p>入力品質の集計はまだありません。</p>';
  const groups = [['機種集計', quality.machine], ['台番号', quality.seat]];
  return `<details><summary>入力データの品質を見る</summary><p>利用する実績：${esc(data.input_cutoff_date)}まで。当日途中の実績は除外します。</p>
    ${groups.map(([label, q]) => `<p>${label}：元${q?.raw_rows || 0}件 → 利用${q?.usable_rows || 0}件 ／ 重複${q?.duplicate_rows || 0}件・除外${q?.excluded_rows || 0}件・矛盾${q?.conflict_keys || 0}組<br><small>${Object.entries(q?.exclusion_reasons || {}).map(([reason, count]) => `${esc(reasonNames[reason] || reason)} ${count}件`).join('／') || '除外なし'}</small></p>`).join('')}
    ${(data.halls || []).map(hall => {
      const q = hall.data_quality?.input_audit;
      return q ? `<p><b>${esc(hall.hall_name)}：${esc(q.label)}</b><br>実績${q.sample_days}日・公開日率${q.calendar_coverage_pct}%・G数確認${q.games_known_pct}%<br>${esc(q.blockers.join('／') || '入力品質基準を通過')}</p>` : '';
    }).join('')}<small>欠損を0枚に置き換えず、数値が食い違う重複は保留。公開日率は営業日率ではなく、公開の偏りを調べる指標です。</small></details>`;
}

export function eventStudyMarkup(studies) {
  if (!studies) return '';
  const coins = n => n == null ? '未計測' : `${esc(n)}枚`;
  return `<details class="event-study"><summary>店舗×イベントの比較（3.26・研究用）</summary><p>同じ店でも、別のイベント名は混ぜません。従来のイベント評価とは別の検証です。新しい補正は未採用で、順位・着席判定への加点はありません。</p>${studies.map(s => `<details><summary>${esc(s.hall_name)} ／ ${s.profiles.length}種類</summary><p>予定の確認期限：${esc(s.information_date)}<br>${esc(s.notice)}</p>${s.profiles.map(p => `<details><summary>${esc(p.event_name)} ／ ${esc(p.status)}</summary><p>対象日予定：${p.target_known ? '単独の登録予定あり' : '未確認・重複など'}<br>同曜日で比較できた実績：${esc(p.paired_days)}日 ／ 平均差：${coins(p.matched_lift_coins)}<br>研究用の推定差枚：${coins(p.predicted_coins)} ／ 補正：${coins(p.adjustment_coins)}</p><p>先読みなしの比較：${esc(p.validation.days)}日<br>平均誤差（小さい方が良好）：補正なし ${coins(p.validation.baseline_mae_coins)} → 補正あり ${coins(p.validation.candidate_mae_coins)}</p><p>${esc(p.blockers.join('／') || '比較材料あり。ただし採用審査前です。')}<br>取得時刻不明：${esc(p.unknown_timestamp_records)}件 ／ 複数イベントの重なる日：${esc(p.overlap_days)}日</p><p>${p.source_urls.filter(u => /^https?:\/\//i.test(u)).map(u => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">出典</a>`).join(' ／ ') || '出典URLなし'}</p><details><summary>日別の答え合わせ</summary>${p.validation.trials.map(t => `<p>${esc(t.date)} ／ 入力は${esc(t.input_cutoff_date)}まで<br>補正なし ${coins(t.baseline_coins)}・補正あり ${coins(t.predicted_coins)} → 実際 ${coins(t.actual_coins)}</p>`).join('') || '<p>比較できる日がまだありません。</p>'}</details></details>`).join('') || '<p>登録イベントがありません。強い日なし、という意味ではありません。</p>'}</details>`).join('')}<p>設定的中率・本人の勝率・利益の保証ではありません。</p></details>`;
}

export function machineStudyMarkup(studies) {
  if (!studies) return '';
  const coins = n => n == null ? '未計測' : `${esc(n)}枚`;
  return `<details class="machine-study"><summary>店舗×機種の比較（3.27・研究用）</summary><p>店全体が良かった日と、その機種が相対的に良かった日を分けて比較します。機種によって実績量・精度は異なります。新方式は未採用で、現在の順位や着席判定を変更しません。</p>${studies.map(s => `<details><summary>${esc(s.hall_name)} ／ ${s.profiles.length}機種</summary><p>入力期限：${esc(s.input_cutoff_date)}<br>稼働不明・1000G未満の除外：${esc(s.activity_excluded_rows)}行<br>${esc(s.notice)}</p>${s.profiles.map(p => `<details><summary>${esc(p.machine_name)} ／ ${esc(p.status)}</summary><p>公開実績：${esc(p.observed_days)}日 ／ 稼働基準通過：${esc(p.usable_days)}日<br>他機種と比較できた日：${esc(p.paired_days)}日（直近56日：${esc(p.recent_paired_days)}日）<br>同日・同店の他機種との差：${coins(p.relative_diff_coins)}<br>最終実績：${esc(p.latest_date)} ／ ${esc(p.installation_status)}</p><p>研究用の予測：${coins(p.candidate?.predicted_coins)}<br>比較用の機種平均：${coins(p.candidate?.baseline_coins)}<br>他機種からの予測：${coins(p.candidate?.peer_estimate_coins)} ／ 機種別補正：${coins(p.candidate?.adjustment_coins)}</p><p>同じ日の誤差比較：${esc(p.validation.days)}日 ／ 比較できない日：${esc(p.validation.unevaluated_days)}日<br>平均誤差（小さい方が良好）：機種平均 ${coins(p.validation.baseline_mae_coins)} → 新方式 ${coins(p.validation.candidate_mae_coins)}</p><p>${esc(p.blockers.join('／') || '比較材料あり。ただし採用審査前です。')}</p><details><summary>日別の答え合わせ（直近10件）・出典（最大5件）</summary>${p.validation.trials.slice(-10).map(t => `<p>${esc(t.date)} ／ 入力は${esc(t.input_cutoff_date)}まで<br>機種平均 ${coins(t.baseline_coins)}・新方式 ${coins(t.predicted_coins)} → 実際 ${coins(t.actual_coins)}</p>`).join('') || '<p>比較できる日がまだありません。</p>'}<p>${p.source_urls.filter(u => /^https?:\/\//i.test(u)).slice(0,5).map(u => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">出典</a>`).join(' ／ ') || '出典URLなし'}</p></details></details>`).join('') || '<p>比較に使える機種データがありません。弱い店、という判定ではありません。</p>'}</details>`).join('')}<p>他機種との差がプラスでも、高設定・本人の利益を保証しません。</p></details>`;
}

export function seatRankingMarkup(studies) {
  if (!studies) return '';
  const coins = n => n == null ? '未計測' : `${esc(n)}枚`;
  const profile = p => `<details class="seat-ranking-card"><summary>${p.rank == null ? '順位なし' : `研究順位 ${esc(p.rank)}位`} · <strong>${esc(p.seat_number)}番台</strong> ／ ${esc(p.validation_status)}</summary>
    <p>研究用の予測差枚：<b>${coins(p.candidate?.predicted_coins)}</b><br>単純な台平均：${coins(p.candidate?.baseline_coins)} ／ 台平均での順位：${p.baseline_rank == null ? 'なし' : `${esc(p.baseline_rank)}位`}<br>同機種の他台からの予測：${coins(p.candidate?.peer_estimate_coins)} ／ 台固有の補正：${coins(p.candidate?.adjustment_coins)}</p>
    <p>比較できた日：${esc(p.paired_days)}日 ／ 稼働基準を通過：${esc(p.usable_days)}日<br>現在の観測期間：${esc(p.training_from)}〜 ／ 最終観測：${esc(p.latest_date)}<br>${esc(p.identity_status)}。今もこの番号にあるかは現地で確認してください。</p>
    <p>${esc(p.blockers.join('／') || '研究順位のみ。着席推奨・本番採用の条件を満たした意味ではありません。')}</p>
    <p>現在期間の答え合わせ：${esc(p.validation.days)}日 ／ 未照合：${esc(p.validation.unresolved_days)}日<br>差枚の平均誤差（小さい方が良好）：台平均 ${coins(p.validation.baseline_mae_coins)} → 新方式 ${coins(p.validation.candidate_mae_coins)}</p>
    <details><summary>根拠・答え合わせ（直近5件）</summary>${p.validation.trials.slice(-5).map(t => `<p>${esc(t.date)}（入力は${esc(t.input_cutoff_date)}まで）<br>新方式 ${coins(t.predicted_coins)} → 実際 ${coins(t.actual_coins)}${t.unresolved_reason ? `<br>${esc(t.unresolved_reason)}` : ''}</p>`).join('') || '<p>比較できる日がまだありません。</p>'}<p>${p.source_urls.filter(u=>/^https?:\/\//i.test(u)).slice(0,5).map(u=>`<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">出典</a>`).join(' ／ ') || '出典URLなし'}</p></details></details>`;
  return `<details class="seat-ranking-study" open><summary>台番号ランキング（3.28・研究用）</summary><p>まず店舗、次に機種を開くと番号順ではなく予測順で確認できます。同機種内だけの比較で、<b>研究順位1位も着席許可ではありません。</b>現在の実戦判定は変更していません。</p>
    ${studies.map(s=>`<details><summary>${esc(s.hall_name)} ／ 研究順位${esc(s.ranked_seats)}台・観測${esc(s.observed_seats)}台</summary><p>対象日：${esc(s.target_date)} ／ 入力期限：${esc(s.input_cutoff_date)}<br>${esc(s.installation_status)}</p>
      ${s.machines.map(m=>`<details><summary>${esc(m.machine_name)} ／ 比較${esc(m.ranked_seats)}台・${esc(m.validation.status)}</summary>
        ${m.profiles.filter(p=>p.rank!=null).map(profile).join('') || '<p>現在、順位を付けられる台がありません。下の不足理由を確認してください。</p>'}
        ${m.profiles.some(p=>p.rank==null) ? `<details><summary>順位なし・理由を見る（${m.profiles.filter(p=>p.rank==null).length}台）</summary>${m.profiles.filter(p=>p.rank==null).map(profile).join('')}</details>` : ''}
        <details><summary>この機種の順位は役立った？</summary><p>過去日を順に戻して順位を検証：${esc(m.validation.attempted_days)}日<br>全比較台の結果が揃った日：${esc(m.validation.days)}日 ／ 欠損・入替などで未照合：${esc(m.validation.unresolved_days)}日</p><p>選んだ1位と、その日の比較対象内の最良台との差（小さい方が良好）：<br>台平均順位 ${coins(m.validation.baseline_regret_coins)} ／ 新順位 ${coins(m.validation.candidate_regret_coins)}</p><p>同点1位は全台の結果を平均。全台の結果が揃わない日は集計せず、別の台に差し替えません。過去の観測期間も含む研究成績で、高設定的中率・本人の勝率や利益ではありません。</p>
        ${m.validation.trials.slice(-5).map(t=>`<p>${esc(t.date)} ／ 比較${esc(t.cohort_size)}台<br>新順位1位：${t.selected_seats.map(esc).join('・')}番 → ${coins(t.actual_coins)}<br>台平均1位：${t.baseline_selected_seats.map(esc).join('・')}番 → ${coins(t.baseline_actual_coins)}${!t.resolved ? `<br>未照合：${t.unresolved_seats.map(esc).join('・')}番（予測時点の順位を保持）` : ''}</p>`).join('')}</details></details>`).join('') || '<p>台番号の比較データがまだありません。機種平均から番号を作ることはしません。</p>'}
      <details><summary>比較条件・注意点</summary><p>${esc(s.notice)}</p><p>稼働不明・1000G未満：${esc(s.activity_excluded_rows)}件 ／ 現在期間から分離した記録：${esc(s.identity_excluded_rows)}件。元記録は削除していません。</p></details></details>`).join('')}</details>`;
}

export async function mountVerification(element, request, data) {
  if (!element) return;
  element.innerHTML = `${seatRankingMarkup(data.seat_ranking_studies)}<h3>前日固定の予測を検証</h3><p>過去検証とは別に、保存した予測と後日の実績を照合します。</p>${qualityMarkup(data)}${seatHistoryMarkup(data.seat_identity)}${eventStudyMarkup(data.event_studies)}${machineStudyMarkup(data.machine_studies)}<div data-verification-body role="status">保存済み予測を確認中…</div><section data-comparison-panel aria-label="予測方式の比較"></section>`;
  const body = element.querySelector('[data-verification-body]');
  const {placementStudyMarkup} = await import('./placement-analysis.mjs?v=3.49.1');
  if (!element.isConnected) return;
  element.insertAdjacentHTML('afterbegin', placementStudyMarkup(data.placement_studies));
  const review = document.createElement('section');
  review.setAttribute('aria-label', '予測モデルの採用審査');
  element.prepend(review);
  const {mountModelReview} = await import('./model-review.mjs?v=3.49.1');
  if (!element.isConnected) return;
  // Load independently: a review API error must not hide existing verification.
  void mountModelReview(review, request, data.model_review_research);
  const probability = document.createElement('section');
  probability.setAttribute('aria-label', '予測確率と予測幅の検証');
  element.prepend(probability);
  const {mountProbabilityValidation} = await import('./probability-validation.mjs?v=3.49.1');
  if (!element.isConnected) return;
  void mountProbabilityValidation(probability, request);
  const candidates = document.createElement('section');
  candidates.setAttribute('aria-label', '候補数と的中率の評価');
  element.prepend(candidates);
  const {mountCandidateEvaluation} = await import('./candidate-evaluation.mjs?v=3.49.1');
  if (!element.isConnected) return;
  void mountCandidateEvaluation(candidates, request);
  const monitor = document.createElement('section');
  monitor.setAttribute('aria-label', '店舗傾向と予測の変化');
  element.prepend(monitor);
  const {mountPredictionMonitor} = await import('./prediction-monitor.mjs?v=3.49.1');
  if (!element.isConnected) return;
  void mountPredictionMonitor(monitor, request);
  async function refresh() {
    try {
      const result = await request('/api/predictions/verification');
      if (!element.isConnected) return;
      body.innerHTML = `<p>保存${result.batches}日分 ／ 最終保存 ${esc(result.latest_saved_at || 'まだありません')}</p>
        ${(result.by_scope || []).map(q => `<p><b>${q.scope === 'seat' ? '台番号' : '機種別'}：${q.success_pct == null ? '推奨の成績はまだ未計測' : `推奨のプラス率 ${q.success_pct}%`}</b><br>推奨の答え合わせ ${q.hits}/${q.recommended_resolved}件（${q.distinct_evaluation_days}日）・95%下限 ${q.lower_bound_pct ?? '—'}%<br>全保存${q.predictions}件・照合済み${q.resolved}件・結果待ち${q.pending}件</p>`).join('')}
        <p>${esc(result.definition)}</p><small>${esc(result.scope_notice)}<br>同じ店・同じ日の結果は関連するため、件数だけで精度保証はしません。<br>${esc(result.schedule_notice)}<br>次の自動実行：${esc(result.next_run_at || '自動実行は未設定（手動実行できます）')}</small>
        <p><button type="button" data-freeze ${result.running ? 'disabled' : ''}>${result.running ? '別の処理で保存・照合中' : '明日の予測を固定・結果を照合'}</button> <button type="button" data-refresh>最新状況を見る</button></p>
        <p data-verification-message role="status"></p>
        <details><summary>固定した予測と結果（直近${result.recent.length}件）</summary>${result.recent.map(r => `<p><b>${esc(r.target_date)} ${esc(r.hall_name)}</b><br>${esc(r.machine_name)}${r.seat_number ? `・${r.seat_number}番台` : '・機種平均'} ／ ${esc(r.action)}<br>予測 ${r.projected}枚・プラス確率${r.probability_pct}% → ${esc(r.result)}${r.actual == null ? '' : `（${r.actual}枚）`}<br><small>保存 ${esc(r.saved_at)}・v${esc(r.version)}</small></p>`).join('') || '<p>まだ保存がありません。事前保存を始めてから成績が蓄積されます。</p>'}</details>`;
      body.querySelector('[data-refresh]').onclick = refresh;
      body.querySelector('[data-freeze]').onclick = async event => {
        const button = event.currentTarget;
        button.disabled = true;
        const status = body.querySelector('[data-verification-message]');
        status.textContent = '明日の全候補を計算・保存中…数十秒〜数分かかる場合があります。';
        try {
          const saved = await request('/api/predictions/run', {method: 'POST'});
          await refresh();
          const currentStatus = body.querySelector('[data-verification-message]');
          if (currentStatus) currentStatus.textContent = `${saved.target_date}：${saved.status === 'already_saved' ? '保存済みの予測を保持しました' : saved.status === 'no_candidates' ? '保存できる予測がありません（データ不足）' : `${saved.recorded}件を固定保存しました`}。新しい答え合わせ${saved.resolved}件。`;
        } catch (error) {
          status.textContent = `保存・照合を確認できません：${error.message}。再実行しても保存済み予測は変わりません。`;
          button.disabled = false;
        }
      };
    } catch (error) {
      body.textContent = `事前検証に接続できません：${error.message}。サーバーの起動・更新と通信を確認してください。`;
    }
  }
  await Promise.all([refresh(), mountComparison(element.querySelector('[data-comparison-panel]'), request)]);
}

const value = (n, suffix = '') => n == null ? '未計測' : `${esc(n)}${suffix}`;
const modelLabels = {current: '現在の予測', mean: '単純平均', recent14: '直近14日平均', weekday: '同じ曜日の平均'};

function scoreTable(models) {
  return `<div class="benchmark-table-scroll" tabindex="0" aria-label="予測方式の比較表・横にスクロールできます"><table><caption>同じ対象・同じ結果で比較（誤差は小さい方が良好）</caption><thead><tr><th scope="col">方式</th><th scope="col">確率の誤差<br>Brier</th><th scope="col">日別Brier</th><th scope="col">差枚の誤差<br>平均絶対誤差</th></tr></thead><tbody>${models.map(m => `<tr><th scope="row">${esc(m.label)}</th><td>${value(m.brier)}</td><td>${value(m.daily_brier)}</td><td>${value(m.mae_coins, '枚')}</td></tr>`).join('')}</tbody></table></div>`;
}

export function comparisonMarkup(report) {
  if (!report) return '<p>過去の比較レポートはまだありません。下のボタンから固定7日分を検証できます。</p>';
  return `<p>${esc(report.mode_notice)}</p>${report.created_at ? `<p>検証実行 ${esc(report.created_at)} ／ 対象 ${esc(report.dates?.[0])}〜${esc(report.dates?.at(-1))}</p>` : ''}
    ${report.legacy_batches_excluded ? `<p>比較方式を事前保存していない旧版の${esc(report.legacy_batches_excluded)}日分は比較対象外です。後から本番成績へ追加しません。</p>` : ''}
    ${(report.cohorts || []).map(c => `<article class="benchmark-cohort"><h4>${c.scope === 'seat' ? '台番号' : '機種平均'} ／ v${esc(c.version)}</h4>
      <p>比較可能 ${esc(c.common)}件 ／ 計算対象 ${esc(c.total)}件（履歴不足などで除外 ${esc(c.excluded)}件）<br>結果あり ${esc(c.resolved)}件・${esc(c.days)}日 ／ 結果待ち ${esc(c.pending)}件</p>
      ${scoreTable(c.models)}
      <p>${c.resolved ? '件数が多くても、同じ日の結果は関連します。日別の比較も確認してください。' : '結果がまだないため、どの方式が優れているかは未判定です。'}</p>
      <details><summary>現在の予測は単純な方法より良い？</summary>${c.paired.map(p => `<p>${esc(modelLabels[p.baseline])}との日別誤差の差：${value(p.daily_brier_delta)}（マイナスなら現在の予測が良好）<br>良好 ${esc(p.better_days)}日・悪化 ${esc(p.worse_days)}日・同等 ${esc(p.tied_days)}日 ／ 比較 ${esc(p.days)}日</p>`).join('')}<p>統計的な優位性や将来の再現性を保証する判定ではありません。予測方式は自動変更しません。</p></details>
      <details><summary>70%共通条件の参考シグナル</summary><p>プラス確率70%以上かつ予測差枚0枚超。着席推奨とは別の比較用条件です。</p>${c.models.map(m => `<p><b>${esc(m.label)}</b>：${esc(m.signal_count)}件（候補率 ${value(m.signal_rate_pct, '%')}）<br>照合 ${esc(m.signal_resolved)}件・待ち ${esc(m.signal_pending)}件 ／ プラス率 ${value(m.signal_success_pct, '%')}</p>`).join('')}</details>
      <details><summary>店舗・機種ごとの比較</summary>${[['hall_name', '店舗別'], ['machine_key', '機種別']].map(([key, label]) => `<details><summary>${label}</summary>${(c.breakdown?.[key] || []).map(g => `<details><summary>${esc(g.name)} ／ 結果${esc(g.resolved)}件・${esc(g.days)}日</summary>${scoreTable(g.models)}</details>`).join('')}</details>`).join('')}</details></article>`).join('') || '<p>比較できる予測はまだありません。事前保存を続けて結果を待ちます。</p>'}
    <p>${esc(report.notice)}</p><details><summary>比較条件と注意点</summary><p>${esc(report.method_notice)}<br>${esc(report.selection_notice)}</p><p>同じ対象の実績14日以上が必要。直近平均は入力期限までの14暦日。同じ曜日が3日未満なら単純平均を使います。単純な3方式の確率はプラス日数に少数標本の補正を加えています。公開データの欠測・低稼働・掲載の偏りが残り、全設置台の評価ではありません。</p></details>`;
}

export async function mountComparison(element, request) {
  if (!element) return;
  element.innerHTML = `<h3>予測方式を同じ条件で比較</h3><p>今の予測は、単純平均・直近平均・曜日平均より役立つかを確認します。</p><div data-comparison-body>比較結果を確認中…</div>`;
  const body = element.querySelector('[data-comparison-body]');
  let timer;
  async function refresh() {
    clearTimeout(timer);
    if (!element.isConnected) return;
    const openDetails = [...body.querySelectorAll('details')].map((d, i) => d.open ? i : -1).filter(i => i >= 0);
    try {
      const result = await request('/api/predictions/comparison');
      if (!element.isConnected) return;
      const job = result.job || {};
      const percent = Math.max(0, Math.min(100, Number(job.percent) || 0));
      body.innerHTML = `<details><summary>本番：前日に固定した方式の成績</summary>${comparisonMarkup(result.prospective)}</details>
        <details data-replay-results><summary>研究用：過去7日を同じ条件で検証</summary>${comparisonMarkup(result.retrospective)}</details>
        <p>最新の公開実績日までの7暦日を検証。前日13時の保存を想定し、対象日の2日前までの120日履歴を使います。数分かかる場合があります。</p>
        <div role="status" aria-live="polite">${job.running ? `<p>検証中：${esc(job.completed)}/${esc(job.total)}日（${percent}%）<br>${esc(job.current_date || '準備中')}</p><progress max="100" value="${percent}" aria-label="過去検証の進行度">${percent}%</progress>` : job.status === 'failed' ? `<p>${esc(job.error)}</p>` : job.status === 'completed' ? '<p>過去検証を完了し、レポートを保存しました。</p>' : ''}</div>
        <button type="button" data-run-benchmark ${job.running ? 'disabled' : ''}>${job.running ? '過去検証中…' : '過去7日を比較検証する'}</button> <button type="button" data-comparison-refresh>比較結果を更新</button><p data-comparison-message role="status"></p>`;
      const details = body.querySelectorAll('details');
      openDetails.forEach(i => { if (details[i]) details[i].open = true; });
      body.querySelector('[data-comparison-refresh]').onclick = refresh;
      body.querySelector('[data-run-benchmark]').onclick = async event => {
        event.currentTarget.disabled = true;
        try {
          await request('/api/predictions/benchmark', {method: 'POST'});
          await refresh();
        } catch (error) {
          body.querySelector('[data-comparison-message]').textContent = `開始状況を確認できません：${error.message}。比較結果を更新して確認してください。`;
          body.querySelector('[data-run-benchmark]').disabled = false;
        }
      };
      if (job.running) timer = setTimeout(refresh, 3000);
    } catch (error) {
      body.textContent = `比較結果に接続できません：${error.message}。サーバーの更新と通信を確認してください。`;
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.textContent = '再確認';
      retry.onclick = refresh;
      body.append(retry);
    }
  }
  await refresh();
}
