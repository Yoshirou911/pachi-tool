/* pachi-tool フロントエンド SPA */
'use strict';

const API = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
  ? ''   // same origin (FastAPI が静的配信)
  : '';

// ---------------------------------------------------------------------------
// API クライアント
// ---------------------------------------------------------------------------
async function apiFetch(path, opts = {}) {
  const url = API + path;
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

const api = {
  getMachines: () => apiFetch('/api/machines'),
  getMachine: (name) => apiFetch(`/api/machines/${encodeURIComponent(name)}`),
  estimate: (body) => apiFetch('/api/estimate', { method: 'POST', body: JSON.stringify(body) }),
  createSession: (body) => apiFetch('/api/sessions', { method: 'POST', body: JSON.stringify(body) }),
  getSessions: (params = {}) => apiFetch('/api/sessions?' + new URLSearchParams(params)),
  getSession: (id) => apiFetch(`/api/sessions/${id}`),
  deleteSession: (id) => apiFetch(`/api/sessions/${id}`, { method: 'DELETE' }),
  getHalls: () => apiFetch('/api/halls'),
  getDaitoAnalysis: () => apiFetch('/api/hall/daito'),
  getMachineRanking: (hall) => apiFetch(`/api/hall/machine_ranking?hall_name=${encodeURIComponent(hall)}`),
};

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------
let _toastTimer;
function showToast(msg, type = '') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + type;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.className = 'toast'; }, 2800);
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  machines: [],
  currentProfile: null,
  lastEstimate: null,
  currentMachine: '',
  currentHall: '',
  sessions: [],
  estimateHistory: [],  // [{games, expected, confidence}]
  minSetting: null,     // 確定演出による下限設定
};

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------
function switchTab(tabId) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabId));
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === `page-${tabId}`));
  if (tabId === 'session') loadSessions();
  if (tabId === 'hall') loadHallPage();
  if (tabId === 'map') loadMapPage();
  if (tabId === 'ai') loadAiPage();
  if (tabId === 'machines') loadMachinesPage();
}

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

// ---------------------------------------------------------------------------
// Connection check
// ---------------------------------------------------------------------------
async function checkConnection() {
  const badge = document.getElementById('header-status');
  try {
    await apiFetch('/api/machines');
    badge.textContent = '接続済';
    badge.className = 'status-badge status-ok';
    return true;
  } catch {
    badge.textContent = 'オフライン';
    badge.className = 'status-badge status-err';
    return false;
  }
}

// ---------------------------------------------------------------------------
// Estimate page
// ---------------------------------------------------------------------------
const estMachine = document.getElementById('est-machine');
const estHall = document.getElementById('est-hall');
const estWeekday = document.getElementById('est-weekday');
const estDom = document.getElementById('est-dom');
const estEvent = document.getElementById('est-event');
const estGames = document.getElementById('est-games');
const estCountsList = document.getElementById('est-counts-list');
const estRunBtn = document.getElementById('est-run-btn');
const estResult = document.getElementById('est-result');
const estSaveBtn = document.getElementById('est-save-btn');
const estSaveForm = document.getElementById('est-save-form');
const estResetBtn = document.getElementById('est-reset-btn');

async function loadMachineSelect() {
  try {
    state.machines = await api.getMachines();
    estMachine.innerHTML = '<option value="">-- 機種を選択 --</option>' +
      state.machines.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
  } catch (e) {
    showToast('機種一覧の取得に失敗: ' + e.message, 'error');
  }
}

estMachine.addEventListener('change', async () => {
  const name = estMachine.value;
  state.currentMachine = name;
  state.estimateHistory = [];
  state.minSetting = null;  // 確定演出リセット
  if (!name) {
    estCountsList.innerHTML = '<p class="hint">機種を選択するとカウント入力欄が表示されます</p>';
    estResetBtn.style.display = 'none';
    estRunBtn.disabled = true;
    state.currentProfile = null;
    document.getElementById('est-confirm-section').style.display = 'none';
    return;
  }
  try {
    state.currentProfile = await api.getMachine(name);
    renderCountInputs(state.currentProfile);
    estRunBtn.disabled = false;
    estResetBtn.style.display = 'block';
    document.getElementById('est-confirm-section').style.display = 'block';
    updateConfirmBadge();
    // 変更検知フォームも更新
    if (cdSection) cdSection.style.display = 'block';
    renderCdCounts(state.currentProfile);
    // 保存したドラフトを復元
    restoreDraft(name);
    // 直近セッション表示
    loadRecentSessions(name);
  } catch (e) {
    showToast('機種データ取得失敗: ' + e.message, 'error');
  }
});

async function loadRecentSessions(machineName) {
  const el = document.getElementById('est-recent-sessions');
  if (!el) return;
  try {
    const [sessions, stats] = await Promise.all([
      api.getSessions({ machine_name: machineName, limit: 5 }),
      apiFetch(`/api/machine/stats?machine_name=${encodeURIComponent(machineName)}`).catch(() => null),
    ]);
    if (!sessions || sessions.length === 0) { el.style.display = 'none'; return; }
    el.style.display = 'block';
    // 集計バッジ
    let summaryHtml = '';
    if (stats && stats.total_sessions > 0) {
      const wr = Math.round((stats.win_rate || 0) * 100);
      const d = stats.diff_yen || 0;
      const sg = d >= 0 ? '+' : '';
      const avgG = stats.total_games && stats.total_sessions ? Math.round(stats.total_games / stats.total_sessions) : 0;
      const wrColor = wr >= 50 ? 'var(--success)' : 'var(--danger)';
      const dColor = d >= 0 ? 'var(--success)' : 'var(--danger)';
      summaryHtml = `
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--border)">
          <span style="font-size:.75rem;background:rgba(124,127,245,0.12);border:1px solid rgba(124,127,245,0.25);border-radius:6px;padding:3px 8px">
            <span style="color:var(--text3)">計</span> <strong>${stats.total_sessions}回</strong>
          </span>
          <span style="font-size:.75rem;background:rgba(124,127,245,0.12);border:1px solid rgba(124,127,245,0.25);border-radius:6px;padding:3px 8px">
            <span style="color:var(--text3)">勝率</span> <strong style="color:${wrColor}">${wr}%</strong>
          </span>
          <span style="font-size:.75rem;background:rgba(124,127,245,0.12);border:1px solid rgba(124,127,245,0.25);border-radius:6px;padding:3px 8px">
            <span style="color:var(--text3)">収支</span> <strong style="color:${dColor}">${sg}${d.toLocaleString()}円</strong>
          </span>
          ${avgG ? `<span style="font-size:.75rem;background:rgba(124,127,245,0.12);border:1px solid rgba(124,127,245,0.25);border-radius:6px;padding:3px 8px">
            <span style="color:var(--text3)">平均G</span> <strong>${avgG.toLocaleString()}</strong>
          </span>` : ''}
          ${stats.avg_estimated_setting ? `<span style="font-size:.75rem;background:rgba(124,127,245,0.12);border:1px solid rgba(124,127,245,0.25);border-radius:6px;padding:3px 8px">
            <span style="color:var(--text3)">平均設定</span> <strong style="color:var(--primary-h)">${stats.avg_estimated_setting}</strong>
          </span>` : ''}
        </div>`;
    }
    el.innerHTML = `
      <div class="card" style="padding:10px 14px">
        <div style="font-size:.72rem;color:var(--text3);text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px">
          この機種の記録
        </div>
        ${summaryHtml}
        ${sessions.map(s => {
          const diffYen = s.diff_yen || 0;
          const diffColor = diffYen > 0 ? 'var(--success)' : diffYen < 0 ? 'var(--danger)' : 'var(--text3)';
          const poster = s.posterior;
          const expSetting = poster ? calcExpectedSetting(poster).toFixed(1) : '--';
          return `
            <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)">
              <div>
                <div style="font-size:.8rem;color:var(--text2)">${esc(s.date)}${s.hall_name ? ' · ' + esc(s.hall_name) : ''}</div>
                <div style="font-size:.72rem;color:var(--text3)">${(s.games_total||0).toLocaleString()}G · 推測設定${expSetting}${s.is_event_day ? ' イベ' : ''}${s.is_corner ? ' 角' : ''}</div>
              </div>
              <div style="text-align:right">
                <div style="font-weight:700;font-size:.88rem;color:${diffColor}">${fmt(diffYen)}</div>
                <div style="font-size:.7rem;color:var(--text3)">${s.diff_coins >= 0 ? '+' : ''}${(s.diff_coins||0).toLocaleString()}枚</div>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `;
  } catch { el.style.display = 'none'; }
}

function renderCountInputs(profile) {
  estCountsList.innerHTML = profile.elements.map(el => `
    <div class="count-row" data-element="${esc(el.name)}">
      <span class="count-name">${esc(el.name)}</span>
      <div class="count-stepper">
        <button class="count-btn" data-dir="-1">−</button>
        <input class="count-input" type="number" min="0" value="0"
               inputmode="numeric" data-el="${esc(el.name)}">
        <button class="count-btn" data-dir="+1">＋</button>
      </div>
    </div>
  `).join('');

  // ステッパーボタン
  estCountsList.querySelectorAll('.count-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const row = btn.closest('.count-row');
      const input = row.querySelector('.count-input');
      const cur = parseInt(input.value) || 0;
      const next = Math.max(0, cur + parseInt(btn.dataset.dir));
      input.value = next;
      saveDraft();
      autoEstimate();
    });
  });

  // 直接入力
  estCountsList.querySelectorAll('.count-input').forEach(input => {
    input.addEventListener('input', () => { saveDraft(); autoEstimate(); });
  });
}

// ゲーム数変更でも自動推測
estGames.addEventListener('input', () => { saveDraft(); autoEstimate(); });
estHall.addEventListener('change', () => { state.currentHall = estHall.value; autoEstimate(); });
estWeekday.addEventListener('change', autoEstimate);
estDom.addEventListener('input', autoEstimate);
estEvent.addEventListener('change', autoEstimate);

let _autoTimer;
function autoEstimate() {
  clearTimeout(_autoTimer);
  _autoTimer = setTimeout(runEstimate, 600);
}

estRunBtn.addEventListener('click', runEstimate);

// ---------------------------------------------------------------------------
// セッションタイマー
// ---------------------------------------------------------------------------
let _timerStart = null;
let _timerInterval = null;
const timerToggle = document.getElementById('timer-toggle');
const timerDisplay = document.getElementById('timer-display');
const timerReset = document.getElementById('timer-reset');

function formatElapsed(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
}

timerToggle?.addEventListener('click', () => {
  if (_timerInterval) {
    // 停止
    clearInterval(_timerInterval);
    _timerInterval = null;
    timerToggle.textContent = '⏱ 再開';
    timerToggle.classList.remove('btn-primary');
  } else {
    // 開始 or 再開
    if (!_timerStart) _timerStart = Date.now();
    else _timerStart = Date.now() - (parseInt(timerDisplay.dataset.elapsed || '0'));
    _timerInterval = setInterval(() => {
      const el = Date.now() - _timerStart;
      timerDisplay.dataset.elapsed = el;
      timerDisplay.textContent = formatElapsed(el);
    }, 1000);
    timerToggle.textContent = '⏸ 一時停止';
    timerToggle.classList.add('btn-primary');
    timerDisplay.style.display = 'inline';
    timerReset.style.display = 'inline-block';
  }
});

timerReset?.addEventListener('click', () => {
  clearInterval(_timerInterval);
  _timerInterval = null;
  _timerStart = null;
  timerDisplay.textContent = '00:00:00';
  timerDisplay.dataset.elapsed = '0';
  timerDisplay.style.display = 'none';
  timerToggle.textContent = '⏱ タイマー開始';
  timerToggle.classList.remove('btn-primary');
  timerReset.style.display = 'none';
});

// ---------------------------------------------------------------------------
// ノートテンプレートボタン
// ---------------------------------------------------------------------------
document.querySelectorAll('.note-tag').forEach(btn => {
  btn.addEventListener('click', () => {
    const notesEl = document.getElementById('save-notes');
    if (!notesEl) return;
    const tag = btn.dataset.note;
    const cur = notesEl.value.trim();
    notesEl.value = cur ? (cur.includes(tag) ? cur : cur + ' / ' + tag) : tag;
    btn.classList.toggle('btn-primary', notesEl.value.includes(tag));
  });
});

async function runEstimate() {
  if (!state.currentMachine) return;
  const counts = getCountValues();
  const games = parseInt(estGames.value) || 0;
  const startedFrom = parseInt(document.getElementById('est-started-from')?.value) || 0;
  const weekday = estWeekday.value !== '' ? parseInt(estWeekday.value) : null;
  const dom = estDom.value ? parseInt(estDom.value) : null;

  try {
    const result = await api.estimate({
      machine_name: state.currentMachine,
      games_total: games,
      started_from: startedFrom,
      element_counts: counts,
      hall_name: estHall.value || '',
      weekday,
      is_event_day: estEvent.checked,
      day_of_month: dom,
      ...(state.minSetting ? { min_setting: state.minSetting } : {}),
    });
    state.lastEstimate = { result, machine: state.currentMachine, games, startedFrom, counts };
    // 推測履歴を積む（最大20件）
    if (result.expected_setting) {
      state.estimateHistory.push({ games, expected: result.expected_setting, confidence: result.confidence || 0 });
      if (state.estimateHistory.length > 20) state.estimateHistory.shift();
    }
    renderEstimateResult(result);
  } catch (e) {
    showToast('推測エラー: ' + e.message, 'error');
  }
}

function getCountValues() {
  const counts = {};
  estCountsList.querySelectorAll('.count-input').forEach(input => {
    const v = parseInt(input.value);
    if (v > 0) counts[input.dataset.el] = v;
  });
  return counts;
}

function renderEstimateResult(r) {
  estResult.style.display = 'block';

  const expectedEl = document.getElementById('res-expected');
  const highEl = document.getElementById('res-high-prob');
  const evEl = document.getElementById('res-ev');
  const retreatEl = document.getElementById('res-retreat');
  const retreatMsg = document.getElementById('res-retreat-msg');
  const barsEl = document.getElementById('res-bars');
  const confEl = document.getElementById('res-confidence');
  const confBar = document.getElementById('res-confidence-bar');
  const confLabel = document.getElementById('res-confidence-label');

  // サンプル不足警告
  let warnEl = document.getElementById('res-sample-warning');
  if (!warnEl) {
    warnEl = document.createElement('div');
    warnEl.id = 'res-sample-warning';
    warnEl.style.cssText = 'display:none;margin-bottom:10px;padding:8px 12px;border-radius:8px;background:rgba(251,191,36,0.12);border:1px solid rgba(251,191,36,0.35);color:#fbbf24;font-size:0.82rem;line-height:1.4';
    confEl.parentNode.insertBefore(warnEl, confEl);
  }
  if (r.sample_warning) {
    warnEl.style.display = 'block';
    warnEl.innerHTML = `<span style="font-size:1em;margin-right:6px">&#9888;</span>${r.sample_warning}`;
  } else {
    warnEl.style.display = 'none';
  }

  // 信頼度表示
  if (confEl && r.confidence !== undefined) {
    confEl.style.display = 'block';
    const pct = Math.round(r.confidence * 100);
    const confColor = r.confidence >= 0.75 ? 'var(--success)' :
                      r.confidence >= 0.50 ? 'var(--warning)' :
                      r.confidence >= 0.25 ? '#f97316' : 'var(--danger)';
    confBar.style.width = pct + '%';
    confBar.style.background = confColor;
    confLabel.textContent = `${r.confidence_label}（${pct}%）`;
    confLabel.style.color = confColor;
  }

  expectedEl.textContent = r.expected_setting.toFixed(2);

  // 90%信用区間をサブテキストで表示
  let ciEl = document.getElementById('res-credible-interval');
  if (!ciEl) {
    ciEl = document.createElement('div');
    ciEl.id = 'res-credible-interval';
    ciEl.style.cssText = 'font-size:.7rem;color:var(--text3);margin-top:2px';
    expectedEl.parentNode.appendChild(ciEl);
  }
  if (r.credible_interval) {
    ciEl.textContent = `90%信用区間: 設定${r.credible_interval[0].toFixed(0)}〜${r.credible_interval[1].toFixed(0)}`;
  }

  const highPct = (r.high_setting_prob * 100).toFixed(0);
  highEl.textContent = highPct + '%';
  highEl.style.color = r.high_setting_prob > 0.5 ? 'var(--success)' : r.high_setting_prob > 0.3 ? 'var(--warning)' : 'var(--danger)';
  evEl.textContent = r.ev_pct.toFixed(1) + '%';
  evEl.style.color = r.ev >= 1.0 ? 'var(--success)' : r.ev >= 0.98 ? 'var(--warning)' : 'var(--danger)';

  // 要素識別力ランキング（折りたたみ）
  let powerEl = document.getElementById('res-element-powers');
  if (!powerEl) {
    powerEl = document.createElement('div');
    powerEl.id = 'res-element-powers';
    powerEl.style.cssText = 'margin-top:10px;padding:8px 10px;background:rgba(124,127,245,0.06);border-radius:8px;border:1px solid rgba(124,127,245,0.15)';
    barsEl.parentNode.insertBefore(powerEl, barsEl);
  }
  if (r.element_powers && Object.keys(r.element_powers).length > 0) {
    const sorted = Object.entries(r.element_powers).sort((a,b) => b[1]-a[1]);
    const maxPow = sorted[0]?.[1] || 1;
    const corrWarning = r.correlated_elements?.length
      ? `<div style="color:#f97316;font-size:.7rem;margin-top:6px">相関要素あり（二重計上の可能性）: ${r.correlated_elements.map(([a,b]) => `${a}↔${b}`).join(', ')}</div>`
      : '';
    powerEl.style.display = 'block';
    powerEl.innerHTML = `<div style="font-size:.68rem;color:var(--text3);margin-bottom:5px;font-weight:600;text-transform:uppercase;letter-spacing:.06em">要素別識別力</div>
      ${sorted.map(([name, pow]) => {
        const pct = Math.round(pow / maxPow * 100);
        const col = pct >= 70 ? 'var(--success)' : pct >= 40 ? 'var(--warning)' : 'var(--text3)';
        return `<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">
          <div style="font-size:.72rem;color:var(--text2);width:120px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(name)}</div>
          <div style="flex:1;height:4px;background:var(--bg3);border-radius:2px">
            <div style="width:${pct}%;height:100%;background:${col};border-radius:2px"></div>
          </div>
          <div style="font-size:.68rem;color:${col};width:32px;text-align:right;flex-shrink:0">${pow.toFixed(1)}</div>
        </div>`;
      }).join('')}${corrWarning}`;
  } else {
    powerEl.style.display = 'none';
  }

  // コイン単価別期待収益
  const profitRow = document.getElementById('res-profit-row');
  const profitEl = document.getElementById('res-profit-1k');
  const denomSel = document.getElementById('denom-select');
  if (profitRow && profitEl && denomSel) {
    profitRow.style.display = 'flex';
    const updateProfit = () => {
      const yen = parseFloat(denomSel.value);
      // 1G あたりのコスト: BET3コイン × 円単価
      // 1円機: 1G = 3コイン × 1円 = 3円 → 1000G = 3000円消費
      // ただし 50コイン = 1000円 なので実際は 1G = 3/50 * 1000 = 60円? 違う
      // 正確: 1円機は 1コイン = 1円, BET3 = 3円/G → 1000G = 3000円の掛け
      // EV = 1.047なら → 期待回収 3000 * 1.047 = 3141 → 期待利益 +141円/1000G
      // しかし実際のパチスロは 50コイン=1000円 (20円/コイン) × denominator...
      // denominator は 1円, 2.5円, 5円 の単価
      // BET3コイン/G, 1コイン=denom円, よって 1G=3*denom円
      const costPer1000G = 3 * yen * 1000;
      const ev = r.ev || 1.0;
      const profit = Math.round(costPer1000G * (ev - 1));
      profitEl.textContent = (profit >= 0 ? '+' : '') + profit.toLocaleString() + '円';
      profitEl.style.color = profit > 0 ? 'var(--success)' : profit < 0 ? 'var(--danger)' : 'var(--text2)';
    };
    updateProfit();
    denomSel.onchange = updateProfit;
  }

  if (r.should_retreat) {
    retreatEl.style.display = 'flex';
    retreatMsg.textContent = r.retreat_reason || '撤退を推奨します';
  } else {
    retreatEl.style.display = 'none';
  }

  const settings = r.settings || Object.keys(r.posterior).sort((a, b) => +a - +b);
  barsEl.innerHTML = settings.map(s => {
    const p = r.posterior[s] || 0;
    const pct = (p * 100).toFixed(1);
    const w = Math.max(4, Math.round(p * 100));
    return `
      <div class="bar-row setting-${s}">
        <span class="bar-label" style="color:var(--s${s})">設定${s}</span>
        <div class="bar-track">
          <div class="bar-fill" style="width:${w}%">
            ${p > 0.08 ? `<span class="bar-pct">${pct}%</span>` : ''}
          </div>
        </div>
        ${p <= 0.08 ? `<span style="font-size:.75rem;color:var(--text3);width:36px">${pct}%</span>` : ''}
      </div>
    `;
  }).join('');

  // 要素別分析（理論値比較付き）
  const analysisEl = document.getElementById('res-element-analysis');
  if (analysisEl && r.element_analysis && r.element_analysis.length > 0) {
    const hasData = r.element_analysis.some(e => e.observed > 0);
    if (hasData) {
      analysisEl.style.display = 'block';
      const rows = r.element_analysis.filter(e => e.observed > 0).map(e => {
        const obs = e.observed;
        const perN = e.observed_per_n ? `1/${e.observed_per_n.toFixed(0)}` : '-';
        const cs = e.closest_setting;
        const csColors = {'1':'var(--s1)','2':'var(--s2)','3':'var(--s3)','4':'var(--s4)','5':'var(--s5)','6':'var(--s6)'};
        const csColor = csColors[cs] || 'var(--text)';
        const theory = e.theoretical || {};
        const settingKeys = Object.keys(theory).sort((a,b) => parseInt(a)-parseInt(b));

        // 理論値の最小〜最大でバーの位置を計算
        const vals = settingKeys.map(s => theory[s]).filter(Boolean);
        const tMin = vals.length ? Math.min(...vals) : obs;
        const tMax = vals.length ? Math.max(...vals) : obs;
        const range = tMax - tMin || 1;
        // 実測値のバー上の位置 (0〜100%)
        const obsPos = Math.max(0, Math.min(100, ((obs - tMin) / range) * 100));
        // 矢印・色
        const up = e.direction === 'up';
        const dirColor = up ? 'var(--success)' : 'var(--danger)';
        const dirText  = up ? '↑ 高め' : '↓ 低め';

        // 設定ごとの理論値ドット文字列
        const theoryDots = settingKeys.map(s => {
          const tp = theory[s];
          const tPerN = tp && tp < 0.05 ? `1/${(1/tp).toFixed(0)}` : tp ? `${(tp*100).toFixed(1)}%` : '-';
          return `<span style="color:${csColors[s]||'var(--text3)'};font-size:.63rem">設${s}:${tPerN}</span>`;
        }).join(' ');

        return `<div style="padding:9px 0;border-bottom:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px">
            <span style="font-size:.78rem;color:var(--text2);flex:1">${esc(e.name)}</span>
            <span style="font-size:.85rem;font-weight:800">${perN}</span>
            <span style="font-size:.75rem;font-weight:800;color:${csColor};background:${csColor}22;padding:1px 7px;border-radius:4px">設${cs}</span>
            <span style="font-size:.72rem;font-weight:700;color:${dirColor}">${dirText}</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">
            <span style="font-size:.6rem;color:var(--text3);flex-shrink:0">設1</span>
            <div style="flex:1;height:6px;background:var(--bg3);border-radius:3px;position:relative">
              <div style="position:absolute;top:50%;left:${obsPos}%;transform:translate(-50%,-50%);width:8px;height:8px;border-radius:50%;background:var(--primary-h);border:2px solid var(--bg2);z-index:1"></div>
            </div>
            <span style="font-size:.6rem;color:var(--text3);flex-shrink:0">設${settingKeys[settingKeys.length-1]}</span>
          </div>
          <div style="display:flex;gap:4px;flex-wrap:wrap">${theoryDots}</div>
        </div>`;
      }).join('');

      analysisEl.innerHTML = `
        <div style="padding:12px 16px">
          <div style="font-size:.68rem;color:var(--primary-h);font-weight:800;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">要素別理論値比較</div>
          ${rows}
        </div>`;
    } else {
      analysisEl.style.display = 'none';
    }
  }

  // 推測履歴スパークライン
  renderSparkline();

  // 撤退推奨判定
  renderAdvice(r);

  // C. AIコメント（非同期で後から表示）
  fetchAiEstimateComment(r);

  // スクロール
  setTimeout(() => estResult.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 100);
}

async function fetchAiEstimateComment(r) {
  let el = document.getElementById('res-ai-comment');
  if (!el) {
    el = document.createElement('div');
    el.id = 'res-ai-comment';
    el.style.cssText = 'margin:8px 0;padding:10px 14px;background:var(--bg3);border-left:3px solid var(--accent);border-radius:0 8px 8px 0;font-size:.78rem;line-height:1.7;color:var(--text2);display:none';
    estResult.appendChild(el);
  }
  try {
    const games = parseInt(document.getElementById('est-games')?.value) || 0;
    if (games < 500) return; // データが少ない場合はスキップ
    el.style.display = 'block';
    el.textContent = 'AIコメント生成中...';
    const data = await fetch('/api/ai/estimate_comment', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_name: state.currentMachine || '',
        games,
        element_counts: state.lastEstimate?.counts || {},
        posterior: r.posterior || {},
        ev: r.ev || 1.0,
        recommendation: r.should_retreat ? '撤退推奨' : '続行',
        element_analysis: r.element_analysis || [],
      }),
    }).then(res => res.json());
    if (data.comment) {
      el.innerHTML = `<span style="font-size:.7rem;color:var(--accent);font-weight:600;display:block;margin-bottom:4px">AIコメント</span>${esc(data.comment)}`;
    } else {
      el.style.display = 'none';
    }
  } catch {
    el.style.display = 'none';
  }
}

function renderAdvice(r) {
  const el = document.getElementById('res-advice');
  const icon = document.getElementById('res-advice-icon');
  const text = document.getElementById('res-advice-text');
  const detail = document.getElementById('res-advice-detail');
  if (!el) return;

  const ev = r.ev || 1.0;
  const expected = r.expected_setting || 1;
  const highProb = r.high_setting_prob || 0;
  const games = parseInt(estGames?.value) || 0;
  const conf = r.confidence || 0;

  // データ不足（信頼度低すぎ）
  if (games < 1000 || conf < 0.15) {
    el.style.display = 'block';
    el.style.background = 'rgba(100,116,139,.1)';
    el.style.border = '1px solid rgba(100,116,139,.2)';
    icon.textContent = '⏳';
    text.textContent = 'データ収集中';
    text.style.color = 'var(--text2)';
    detail.textContent = 'G数を増やすと推測精度が向上します';
    return;
  }

  if (ev >= 1.05 || (highProb >= 0.6 && expected >= 5.0)) {
    el.style.display = 'block';
    el.style.background = 'rgba(16,185,129,.1)';
    el.style.border = '1px solid rgba(16,185,129,.25)';
    icon.textContent = '✅';
    text.textContent = 'ヤメ時ではありません — 継続推奨';
    text.style.color = 'var(--success)';
    detail.textContent = `期待値 ${r.ev_pct?.toFixed(1)}% / 高設定率 ${(highProb*100).toFixed(0)}%`;
  } else if (ev >= 1.00 || highProb >= 0.35) {
    el.style.display = 'block';
    el.style.background = 'rgba(245,158,11,.08)';
    el.style.border = '1px solid rgba(245,158,11,.25)';
    icon.textContent = '⚠️';
    text.textContent = '要判断 — 状況次第で続行';
    text.style.color = 'var(--warning)';
    detail.textContent = `期待値 ${r.ev_pct?.toFixed(1)}% / 高設定率 ${(highProb*100).toFixed(0)}%`;
  } else if (r.should_retreat || ev < 0.98) {
    el.style.display = 'block';
    el.style.background = 'rgba(244,63,94,.1)';
    el.style.border = '1px solid rgba(244,63,94,.25)';
    icon.textContent = '🚨';
    text.textContent = '撤退推奨';
    text.style.color = 'var(--danger)';
    detail.textContent = r.retreat_reason || `期待値 ${r.ev_pct?.toFixed(1)}% — EV割れ`;
  } else {
    el.style.display = 'none';
  }
}

function renderSparkline() {
  const el = document.getElementById('res-sparkline');
  const svg = document.getElementById('res-sparkline-svg');
  if (!el || !svg || state.estimateHistory.length < 2) {
    if (el) el.style.display = 'none';
    return;
  }
  el.style.display = 'block';

  const h = state.estimateHistory;
  const W = svg.parentElement?.clientWidth || 280;
  const H = 40;
  const pad = 6;

  const minV = 1, maxV = 6;
  const xs = h.map((_, i) => pad + (i / (h.length - 1)) * (W - pad * 2));
  const ys = h.map(p => H - pad - ((p.expected - minV) / (maxV - minV)) * (H - pad * 2));

  const polyline = xs.map((x, i) => `${x},${ys[i]}`).join(' ');
  // 最後の点
  const lastX = xs[xs.length - 1];
  const lastY = ys[ys.length - 1];
  const lastE = h[h.length - 1].expected;
  const dotColor = lastE >= 4 ? 'var(--success)' : lastE >= 3 ? 'var(--warning)' : 'var(--danger)';

  // y軸ガイドライン（設定4）
  const y4 = H - pad - ((4 - minV) / (maxV - minV)) * (H - pad * 2);

  // 塗りつぶし用のパスポイント（底辺を閉じる）
  const fillPath = `M${xs[0]},${ys[0]} ` + xs.slice(1).map((x, i) => `L${x},${ys[i+1]}`).join(' ') +
                   ` L${xs[xs.length-1]},${H} L${xs[0]},${H} Z`;

  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = `
    <defs>
      <linearGradient id="spk-grad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.3"/>
        <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <line x1="${pad}" y1="${y4}" x2="${W - pad}" y2="${y4}"
          stroke="var(--text3)" stroke-width="0.5" stroke-dasharray="2 2" opacity="0.5"/>
    <text x="${pad}" y="${y4 - 2}" font-size="8" fill="var(--text3)" opacity="0.5">設4</text>
    <path d="${fillPath}" fill="url(#spk-grad)"/>
    <polyline points="${polyline}"
      fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${lastX}" cy="${lastY}" r="3.5" fill="${dotColor}" stroke="var(--bg2)" stroke-width="1"/>
    <text x="${Math.min(lastX + 5, W - 24)}" y="${Math.max(lastY + 4, 10)}" font-size="9.5" font-weight="bold" fill="${dotColor}">${lastE.toFixed(1)}</text>
  `;
}

// 確定演出制約
function updateConfirmBadge() {
  const badge = document.getElementById('est-confirm-badge');
  const clearBtn = document.getElementById('est-confirm-clear');
  const min = state.minSetting;
  document.querySelectorAll('.confirm-btn').forEach(btn => {
    btn.classList.toggle('btn-warning', Number(btn.dataset.min) === min);
  });
  if (min) {
    badge.style.display = 'block';
    badge.textContent = `⚡ 設${min}以上確定が適用中`;
    clearBtn.style.display = 'inline-block';
  } else {
    badge.style.display = 'none';
    clearBtn.style.display = 'none';
  }
}

document.querySelectorAll('.confirm-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const min = Number(btn.dataset.min);
    state.minSetting = state.minSetting === min ? null : min;
    updateConfirmBadge();
    // 推測結果があれば再計算
    if (state.lastEstimate) runEstimate();
  });
});

document.getElementById('est-confirm-clear')?.addEventListener('click', () => {
  state.minSetting = null;
  updateConfirmBadge();
  if (state.lastEstimate) runEstimate();
});

// リセット
estResetBtn.addEventListener('click', () => {
  estCountsList.querySelectorAll('.count-input').forEach(i => i.value = '0');
  estGames.value = '';
  estResult.style.display = 'none';
  estSaveForm.style.display = 'none';
  clearDraft();
});

// セッション保存フォーム表示
estSaveBtn.addEventListener('click', () => {
  estSaveForm.style.display = estSaveForm.style.display === 'none' ? 'block' : 'none';
  if (estSaveForm.style.display === 'block') {
    estSaveForm.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
});
document.getElementById('save-cancel-btn').addEventListener('click', () => {
  estSaveForm.style.display = 'none';
});

// 投資・回収から差額を自動計算表示、差枚→円換算ヒント
function updateSaveDiff() {
  const inv = parseInt(document.getElementById('save-inv').value) || 0;
  const ret = parseInt(document.getElementById('save-ret').value) || 0;
  const coins = parseInt(document.getElementById('save-coins').value) || null;
  const diffEl = document.getElementById('save-diff-display');
  const hintEl = document.getElementById('save-coins-hint');
  if (inv > 0 || ret > 0) {
    const d = ret - inv;
    diffEl.style.display = 'block';
    diffEl.textContent = (d >= 0 ? '+' : '') + d.toLocaleString() + '円';
    diffEl.style.background = d > 0 ? 'rgba(34,197,94,.15)' : d < 0 ? 'rgba(239,68,68,.15)' : 'var(--bg3)';
    diffEl.style.color = d > 0 ? 'var(--success)' : d < 0 ? 'var(--danger)' : 'var(--text1)';
  } else {
    diffEl.style.display = 'none';
  }
  if (coins && hintEl) {
    const coinYen = coins * 20;
    hintEl.textContent = `≈ ${coinYen >= 0 ? '+' : ''}${coinYen.toLocaleString()}円`;
    hintEl.style.color = coinYen >= 0 ? 'var(--success)' : 'var(--danger)';
  } else if (hintEl) {
    hintEl.textContent = '';
  }
}
document.getElementById('save-inv').addEventListener('input', updateSaveDiff);
document.getElementById('save-ret').addEventListener('input', updateSaveDiff);
document.getElementById('save-coins').addEventListener('input', updateSaveDiff);

document.getElementById('save-confirm-btn').addEventListener('click', async () => {
  if (!state.lastEstimate) return;
  const inv = parseInt(document.getElementById('save-inv').value) || 0;
  const ret = parseInt(document.getElementById('save-ret').value) || 0;
  const seat = parseInt(document.getElementById('save-seat').value) || null;
  const coins = parseInt(document.getElementById('save-coins').value) || 0;
  const notes = document.getElementById('save-notes').value;
  const corner = document.getElementById('save-corner').checked;

  try {
    const dom = estDom.value ? parseInt(estDom.value) : null;
    const eventDay = estEvent.checked;
    const weekday = estWeekday.value !== '' ? parseInt(estWeekday.value) : null;

    await api.createSession({
      machine_name: state.lastEstimate.machine,
      hall_name: estHall.value || '',
      games_total: state.lastEstimate.games,
      started_from: state.lastEstimate.startedFrom || 0,
      element_counts: state.lastEstimate.counts,
      posterior: state.lastEstimate.result.posterior,
      investment: inv,
      returns: ret,
      diff_coins: coins,
      seat_number: seat,
      is_corner: corner,
      is_event_day: eventDay,
      notes,
    });
    showToast('セッションを保存しました ✓', 'success');
    estSaveForm.style.display = 'none';
    // フォームリセット
    ['save-inv','save-ret','save-seat','save-coins','save-notes'].forEach(id => {
      document.getElementById(id).value = '';
    });
    document.getElementById('save-corner').checked = false;
  } catch (e) {
    showToast('保存失敗: ' + e.message, 'error');
  }
});

// ---------------------------------------------------------------------------
// Draft (localStorage)
// ---------------------------------------------------------------------------
function saveDraft() {
  if (!state.currentMachine) return;
  const counts = getCountValues();
  const draft = { machine: state.currentMachine, games: estGames.value, counts };
  localStorage.setItem('pachi_draft', JSON.stringify(draft));
}

function restoreDraft(machine) {
  try {
    const raw = localStorage.getItem('pachi_draft');
    if (!raw) return;
    const draft = JSON.parse(raw);
    if (draft.machine !== machine) return;
    estGames.value = draft.games || '';
    Object.entries(draft.counts || {}).forEach(([el, cnt]) => {
      const input = estCountsList.querySelector(`[data-el="${el}"]`);
      if (input) input.value = cnt;
    });
  } catch { /* ignore */ }
}

function clearDraft() {
  localStorage.removeItem('pachi_draft');
}

// ---------------------------------------------------------------------------
// Sessions page
// ---------------------------------------------------------------------------
async function loadSessions() {
  const hallFilter = document.getElementById('ses-hall-filter').value;
  const machineFilter = document.getElementById('ses-machine-filter').value;
  const monthFilter = document.getElementById('ses-month-filter')?.value;

  try {
    const params = {};
    if (hallFilter) params.hall_name = hallFilter;
    if (machineFilter) params.machine_name = machineFilter;
    if (monthFilter) {
      params.date_from = monthFilter + '-01';
      const [y, m] = monthFilter.split('-').map(Number);
      const lastDay = new Date(y, m, 0).getDate();
      params.date_to = `${monthFilter}-${lastDay}`;
    }

    const sessions = await api.getSessions(params);
    state.sessions = sessions;
    renderSessions(sessions);
    renderSessionSummary(sessions);
    renderPnLChart(sessions);
    renderMonthlyStats(sessions);
    renderMonthlyBarChart(sessions);
    renderMachineBreakdownChart(sessions);
    renderSeatAnalysis(sessions);
    renderDailyProfitChart(sessions);
  } catch (e) {
    showToast('セッション取得失敗: ' + e.message, 'error');
  }
}
document.getElementById('ses-month-filter')?.addEventListener('change', loadSessions);

async function populateSessionFilters() {
  try {
    const halls = await api.getHalls();
    // 収支フィルター
    const hallSel = document.getElementById('ses-hall-filter');
    // ホール傾向セレクタ
    const hallTrendSel = document.getElementById('hall-select');
    // 推測ページのホールdatalist
    const hallDatalist = document.getElementById('hall-datalist');
    halls.forEach(h => {
      const o1 = document.createElement('option');
      o1.value = h; o1.textContent = h;
      hallSel.appendChild(o1);
      if (hallDatalist) {
        const o2 = document.createElement('option');
        o2.value = h;
        hallDatalist.appendChild(o2);
      }
      if (hallTrendSel) {
        // 既存の選択肢と重複しない場合のみ追加
        if (![...hallTrendSel.options].some(o => o.value === h)) {
          const o3 = document.createElement('option');
          o3.value = h; o3.textContent = h;
          hallTrendSel.appendChild(o3);
        }
      }
    });
    const machineSel = document.getElementById('ses-machine-filter');
    state.machines.forEach(m => {
      const o = document.createElement('option');
      o.value = m; o.textContent = m;
      machineSel.appendChild(o);
    });
  } catch { /* ignore */ }
}

function renderSessionSummary(sessions) {
  const total = sessions.length;
  const diffYen = sessions.reduce((s, r) => s + (r.diff_yen || 0), 0);
  const wins = sessions.filter(s => (s.diff_yen || 0) > 0).length;
  document.getElementById('sum-count').textContent = total;
  const diffEl = document.getElementById('sum-diff');
  diffEl.textContent = fmt(diffYen);
  diffEl.className = 'stat-value ' + (diffYen >= 0 ? 'diff-pos glow' : 'diff-neg glow');
  document.getElementById('sum-wr').textContent = total ? Math.round(wins / total * 100) + '%' : '--%';

  // 連続記録（直近のW/L streak）
  const streakEl = document.getElementById('sum-streak');
  if (streakEl && total > 0) {
    const sorted = [...sessions].sort((a, b) => a.date < b.date ? 1 : -1);
    let streak = 0;
    const first = (sorted[0].diff_yen || 0) >= 0;
    for (const s of sorted) {
      if (((s.diff_yen || 0) >= 0) === first) streak++;
      else break;
    }
    if (streak > 1) {
      const label = first ? `🔥${streak}連勝中` : `❄${streak}連敗中`;
      const color = first ? 'var(--success)' : 'var(--danger)';
      streakEl.innerHTML = `<span style="color:${color}">${label}</span>`;
    } else {
      streakEl.innerHTML = '';
    }
  }
}

function renderSessions(sessions) {
  const container = document.getElementById('session-list');
  if (!sessions.length) {
    container.innerHTML = '<p class="hint center">まだ記録がありません</p>';
    return;
  }
  container.innerHTML = sessions.map(s => {
    const diffYen = s.diff_yen || 0;
    const tags = [
      s.is_event_day ? '<span class="tag tag-event">イベント</span>' : '',
      s.is_corner ? '<span class="tag tag-corner">角台</span>' : '',
    ].filter(Boolean).join('');
    const expectedSetting = s.posterior ? calcExpectedSetting(s.posterior) : null;

    return `
      <div class="session-item ${diffYen >= 0 ? 'pos' : 'neg'}" data-id="${s.id}">
        <div class="session-item-header">
          <span class="session-date">${s.date}</span>
          <span class="session-machine">${esc(s.machine_name)}</span>
          ${tags}
        </div>
        <div class="session-hall">${esc(s.hall_name || '')} ${s.seat_number ? '台' + s.seat_number : ''}</div>
        <div class="session-stats" style="margin-top:6px">
          <span class="session-stat">G数: <span class="val">${(s.games_total || 0).toLocaleString()}</span></span>
          <span class="session-stat">収支: <span class="val ${diffYen >= 0 ? 'diff-pos glow' : 'diff-neg glow'}" style="font-weight:800">${fmt(diffYen)}</span></span>
          ${expectedSetting !== null ? `<span class="session-stat">推測設定: <span class="val">${expectedSetting.toFixed(1)}</span></span>` : ''}
        </div>
      </div>
    `;
  }).join('');

  container.querySelectorAll('.session-item').forEach(item => {
    item.addEventListener('click', () => openSessionModal(parseInt(item.dataset.id)));
  });
}

function calcExpectedSetting(posterior) {
  return Object.entries(posterior).reduce((sum, [s, p]) => sum + parseInt(s) * p, 0);
}

async function openSessionModal(id) {
  const s = await api.getSession(id).catch(() => null);
  if (!s) return;
  const diffYen = s.diff_yen || 0;
  document.getElementById('modal-title').textContent = `${s.machine_name} (${s.date})`;
  const body = document.getElementById('modal-body');
  const posterior = s.posterior;
  const expectedSetting = posterior ? calcExpectedSetting(posterior).toFixed(2) : '--';

  body.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px">
      <div><span style="font-size:.75rem;color:var(--text3)">ホール</span><br><strong>${esc(s.hall_name || '--')}</strong></div>
      <div><span style="font-size:.75rem;color:var(--text3)">台番号</span><br><strong>${s.seat_number ? s.seat_number + '番台' : '--'}</strong></div>
      <div><span style="font-size:.75rem;color:var(--text3)">投資</span><br><strong>${(s.investment||0).toLocaleString()}円</strong></div>
      <div><span style="font-size:.75rem;color:var(--text3)">回収</span><br><strong>${(s.returns||0).toLocaleString()}円</strong></div>
      <div><span style="font-size:.75rem;color:var(--text3)">収支</span><br><strong class="${diffYen >= 0 ? 'diff-pos' : 'diff-neg'}">${fmt(diffYen)}</strong></div>
      <div><span style="font-size:.75rem;color:var(--text3)">総G数</span><br><strong>${(s.games_total || 0).toLocaleString()}</strong></div>
      <div><span style="font-size:.75rem;color:var(--text3)">差枚</span><br><strong>${(s.diff_coins || 0).toLocaleString()}</strong></div>
      <div><span style="font-size:.75rem;color:var(--text3)">推測設定</span><br><strong>${expectedSetting}</strong></div>
    </div>
    ${s.notes ? `<p style="font-size:.85rem;color:var(--text2);margin-bottom:12px">📝 ${esc(s.notes)}</p>` : ''}
    ${posterior ? renderMiniPosterior(posterior) : ''}
    ${Object.keys(s.element_counts || {}).length ? `
      <div style="margin-top:12px">
        <div style="font-size:.75rem;color:var(--text3);margin-bottom:6px">カウント</div>
        ${Object.entries(s.element_counts).map(([k, v]) => `
          <div style="display:flex;justify-content:space-between;padding:4px 0;font-size:.85rem;border-bottom:1px solid var(--border)">
            <span style="color:var(--text2)">${esc(k)}</span><span><strong>${v}</strong></span>
          </div>
        `).join('')}
      </div>
    ` : ''}
  `;

  document.getElementById('modal-delete').onclick = async () => {
    if (!confirm('このセッションを削除しますか？')) return;
    await api.deleteSession(id).catch(() => null);
    closeModal();
    loadSessions();
    showToast('削除しました', 'success');
  };

  document.getElementById('modal-edit').onclick = () => openSessionEdit(s);

  document.getElementById('modal-overlay').style.display = 'flex';
}

function openSessionEdit(s) {
  const body = document.getElementById('modal-body');
  body.innerHTML = `
    <div style="display:grid;gap:10px">
      <div class="form-row-2col">
        <div>
          <label class="form-label">投資 (円)</label>
          <input type="number" id="edit-inv" class="form-input" value="${s.investment || 0}" inputmode="numeric">
        </div>
        <div>
          <label class="form-label">回収 (円)</label>
          <input type="number" id="edit-ret" class="form-input" value="${s.returns || 0}" inputmode="numeric">
        </div>
      </div>
      <div class="form-row-2col">
        <div>
          <label class="form-label">総G数</label>
          <input type="number" id="edit-games" class="form-input" value="${s.games_total || 0}" inputmode="numeric">
        </div>
        <div>
          <label class="form-label">差枚</label>
          <input type="number" id="edit-coins" class="form-input" value="${s.diff_coins || 0}" inputmode="numeric">
        </div>
      </div>
      <div class="form-row-2col">
        <div>
          <label class="form-label">台番号</label>
          <input type="number" id="edit-seat" class="form-input" value="${s.seat_number || ''}" inputmode="numeric">
        </div>
        <div>
          <label class="form-label" style="padding-top:8px">
            <input type="checkbox" id="edit-corner" ${s.is_corner ? 'checked' : ''}> 角台
          </label>
        </div>
      </div>
      <div>
        <label class="form-label">メモ</label>
        <input type="text" id="edit-notes" class="form-input" value="${esc(s.notes || '')}">
      </div>
      <button id="edit-save-btn" class="btn btn-primary btn-full">保存する</button>
    </div>
  `;
  document.getElementById('edit-save-btn').onclick = async () => {
    const updates = {
      investment: parseInt(document.getElementById('edit-inv').value) || 0,
      returns: parseInt(document.getElementById('edit-ret').value) || 0,
      games_total: parseInt(document.getElementById('edit-games').value) || 0,
      diff_coins: parseInt(document.getElementById('edit-coins').value) || 0,
      seat_number: parseInt(document.getElementById('edit-seat').value) || null,
      is_corner: document.getElementById('edit-corner').checked,
      notes: document.getElementById('edit-notes').value,
    };
    try {
      await apiFetch(`/api/sessions/${s.id}`, { method: 'PUT', body: JSON.stringify(updates) });
      closeModal();
      loadSessions();
      showToast('更新しました', 'success');
    } catch (e) {
      showToast('更新失敗: ' + e.message, 'error');
    }
  };
}

function renderMiniPosterior(posterior) {
  const settings = Object.keys(posterior).sort((a, b) => +a - +b);
  return `
    <div style="margin-top:4px">
      <div style="font-size:.75rem;color:var(--text3);margin-bottom:6px">推測分布</div>
      ${settings.map(s => {
        const p = posterior[s] || 0;
        const w = Math.max(3, Math.round(p * 100));
        return `
          <div class="bar-row setting-${s}" style="margin-bottom:5px">
            <span class="bar-label" style="color:var(--s${s})">設定${s}</span>
            <div class="bar-track" style="height:18px">
              <div class="bar-fill" style="width:${w}%">
                ${p > 0.1 ? `<span class="bar-pct">${(p*100).toFixed(0)}%</span>` : ''}
              </div>
            </div>
            ${p <= 0.1 ? `<span style="font-size:.72rem;color:var(--text3);width:32px">${(p*100).toFixed(0)}%</span>` : ''}
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function closeModal() {
  document.getElementById('modal-overlay').style.display = 'none';
}
document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('modal-close2').addEventListener('click', closeModal);
document.getElementById('modal-overlay').addEventListener('click', e => {
  if (e.target === document.getElementById('modal-overlay')) closeModal();
});

// クイック記録フォーム
const quickEntryBtn = document.getElementById('quick-entry-btn');
const quickEntryForm = document.getElementById('quick-entry-form');
quickEntryBtn?.addEventListener('click', () => {
  const visible = quickEntryForm.style.display !== 'none';
  quickEntryForm.style.display = visible ? 'none' : 'block';
  quickEntryBtn.textContent = visible ? '＋ セッションを素早く記録' : '▲ 閉じる';
  if (!visible) {
    // デフォルト値セット
    const today = new Date().toLocaleDateString('sv');  // YYYY-MM-DD in local TZ
    document.getElementById('qe-date').value = today;
    // ホール候補をdatalistに
    const hallDL = document.getElementById('qe-hall-list');
    const machineDL = document.getElementById('qe-machine-list');
    if (hallDL && !hallDL.children.length) {
      [...document.getElementById('hall-datalist').children].forEach(o => {
        const c = document.createElement('option'); c.value = o.value; hallDL.appendChild(c);
      });
    }
    if (machineDL && !machineDL.children.length) {
      state.machines.forEach(m => {
        const o = document.createElement('option'); o.value = m; machineDL.appendChild(o);
      });
    }
    quickEntryForm.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
});

function updateQeDiff() {
  const inv = parseInt(document.getElementById('qe-inv').value) || 0;
  const ret = parseInt(document.getElementById('qe-ret').value) || 0;
  const el = document.getElementById('qe-diff-display');
  if (inv > 0 || ret > 0) {
    const d = ret - inv;
    el.style.display = 'block';
    el.textContent = (d >= 0 ? '+' : '') + d.toLocaleString() + '円';
    el.style.background = d > 0 ? 'rgba(34,197,94,.15)' : d < 0 ? 'rgba(239,68,68,.15)' : 'var(--bg3)';
    el.style.color = d > 0 ? 'var(--success)' : d < 0 ? 'var(--danger)' : 'var(--text1)';
  } else { el.style.display = 'none'; }
}
document.getElementById('qe-inv')?.addEventListener('input', updateQeDiff);
document.getElementById('qe-ret')?.addEventListener('input', updateQeDiff);

document.getElementById('qe-cancel-btn')?.addEventListener('click', () => {
  quickEntryForm.style.display = 'none';
  quickEntryBtn.textContent = '＋ セッションを素早く記録';
});

document.getElementById('qe-save-btn')?.addEventListener('click', async () => {
  const machine = document.getElementById('qe-machine').value.trim();
  if (!machine) { showToast('機種名を入力してください', 'error'); return; }
  const inv = parseInt(document.getElementById('qe-inv').value) || 0;
  const ret = parseInt(document.getElementById('qe-ret').value) || 0;
  const payload = {
    machine_name: machine,
    hall_name: document.getElementById('qe-hall').value.trim(),
    date: document.getElementById('qe-date').value || new Date().toLocaleDateString('sv'),
    games_total: parseInt(document.getElementById('qe-games').value) || 0,
    investment: inv,
    returns: ret,
    seat_number: parseInt(document.getElementById('qe-seat').value) || null,
    is_corner: document.getElementById('qe-corner').checked,
  };
  try {
    await api.createSession(payload);
    showToast('記録しました', 'success');
    quickEntryForm.style.display = 'none';
    quickEntryBtn.textContent = '＋ セッションを素早く記録';
    // フォームリセット
    ['qe-machine','qe-hall','qe-inv','qe-ret','qe-games','qe-seat'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = '';
    });
    document.getElementById('qe-corner').checked = false;
    document.getElementById('qe-diff-display').style.display = 'none';
    loadSessions();
    // ホールdatalist更新
    await populateSessionFilters();
  } catch (e) {
    showToast('記録失敗: ' + e.message, 'error');
  }
});

document.getElementById('ses-refresh-btn').addEventListener('click', loadSessions);

document.getElementById('ses-export-btn').addEventListener('click', () => {
  const hallFilter = document.getElementById('ses-hall-filter').value;
  const machineFilter = document.getElementById('ses-machine-filter').value;
  const params = new URLSearchParams();
  if (hallFilter) params.set('hall_name', hallFilter);
  if (machineFilter) params.set('machine_name', machineFilter);
  const url = '/api/sessions/export?' + params.toString();
  const a = document.createElement('a');
  a.href = url;
  a.download = 'sessions.csv';
  a.click();
  showToast('CSVをダウンロードしています...', 'success');
});

// CSVインポート（ブラウザ側でパースして個別POST）
const sesImportInput = document.getElementById('ses-import-input');
if (sesImportInput) {
  sesImportInput.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const text = await file.text();
    const lines = text.replace(/^﻿/, '').trim().split('\n');
    if (lines.length < 2) { showToast('CSVにデータがありません', 'error'); return; }
    const headers = lines[0].split(',').map(h => h.trim());
    const rows = lines.slice(1).map(line => {
      const vals = line.split(',');
      const obj = {};
      headers.forEach((h, i) => obj[h] = (vals[i] || '').trim());
      return obj;
    }).filter(r => r.machine_name);
    if (!rows.length) { showToast('インポート対象がありません', 'error'); return; }
    showToast(`${rows.length}件をインポート中...`, 'success');
    let imported = 0, skipped = 0;
    for (const row of rows) {
      try {
        await api.createSession({
          date: row.date || new Date().toISOString().slice(0, 10),
          machine_name: row.machine_name,
          hall_name: row.hall_name || '',
          seat_number: row.seat_number ? parseInt(row.seat_number) : null,
          is_corner: row.is_corner === '1',
          games_total: parseInt(row.games_total) || 0,
          investment: parseInt(row.investment) || 0,
          returns: parseInt(row.returns) || 0,
          diff_coins: parseInt(row.diff_coins) || 0,
          is_event_day: row.is_event_day === '1',
          started_from: parseInt(row.started_from) || 0,
          notes: row.notes || '',
        });
        imported++;
      } catch { skipped++; }
    }
    showToast(`インポート完了: ${imported}件 (${skipped}件スキップ)`, 'success');
    loadSessions();
    e.target.value = '';
  });
}

// ---------------------------------------------------------------------------
// Hall page
// ---------------------------------------------------------------------------
async function loadHallPage() {
  const hall = getSelectedHall();
  if (!hall) return;
  try {
    // セッション統計を取得
    const [daitoData, hallStats] = await Promise.allSettled([
      api.getDaitoAnalysis(),
      apiFetch(`/api/hall/stats?hall_name=${encodeURIComponent(hall)}`),
    ]);

    // 自分のセッションによる機種ランキング（セッション側を優先）
    const stats = hallStats.status === 'fulfilled' ? hallStats.value : null;
    if (stats && stats.total_sessions > 0) {
      renderMySessionStats(stats);
    }

    if (daitoData.status === 'fulfilled' && hall === 'ベガスベガス大東店') {
      const data = daitoData.value;
      renderWeekdayChart(data.weekday_scores);
      renderMachineRanking(data.machine_scores);
      renderSpecialDays(data.special_days);
      renderTodayRecommend(data);
    } else {
      // 他ホール or DAITO失敗 → セッション由来データで代替
      renderWeekdayChartFromSessions(hall);
      document.getElementById('hall-machine-ranking').innerHTML =
        stats && stats.machine_stats
          ? renderMachineRankingFromSessions(stats.machine_stats)
          : '<p class="hint">まだ記録がありません</p>';
      document.getElementById('hall-special-days').innerHTML =
        '<p class="hint">特定日分析には50件以上のデータが必要です</p>';
      document.getElementById('hall-today-recommend').innerHTML =
        '<p class="hint">データ蓄積後に推奨が表示されます</p>';
    }
  } catch (e) {
    showToast('店データ取得失敗: ' + e.message, 'error');
  }
  // スクレイプステータスを非同期で読み込み
  loadScrapeStatus();
  loadAnasloStatus();
  loadTodayTargets(hall);
  loadTodayDowMachines(hall);
  loadMachineSettingTendency(hall);
}

function renderMySessionStats(stats) {
  const el = document.getElementById('hall-machine-ranking');
  if (!stats || !stats.machine_stats) return;
  el.innerHTML = renderMachineRankingFromSessions(stats.machine_stats);
}

function renderMachineRankingFromSessions(machineStats) {
  const sorted = Object.entries(machineStats)
    .sort(([,a],[,b]) => (b.total_diff_yen / b.count) - (a.total_diff_yen / a.count));
  if (!sorted.length) return '<p class="hint">まだ記録がありません</p>';
  return `
    <div style="font-size:.72rem;color:var(--text3);margin-bottom:8px">自分の収支実績より</div>
    ${sorted.slice(0, 8).map(([machine, d], i) => {
      const avg = Math.round(d.total_diff_yen / d.count);
      const avgColor = avg >= 0 ? 'var(--success)' : 'var(--danger)';
      const wr = Math.round(d.wins / d.count * 100);
      const medal = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : '';
      return `
        <div class="machine-rank-row">
          <span class="rank-num">${medal || (i + 1)}</span>
          <span class="rank-machine">${esc(machine)}</span>
          <div style="text-align:right">
            <div style="font-weight:700;font-size:.85rem;color:${avgColor}">${avg >= 0 ? '+' : ''}${avg.toLocaleString()}円/回</div>
            <div style="font-size:.7rem;color:var(--text3)">${d.count}回 勝率${wr}%</div>
          </div>
        </div>
      `;
    }).join('')}
  `;
}

async function renderWeekdayChartFromSessions(hall) {
  const el = document.getElementById('hall-weekday-chart');
  try {
    const sessions = await api.getSessions({ hall_name: hall, limit: 500 });
    if (!sessions.length) { el.innerHTML = '<p class="hint">まだ記録がありません</p>'; return; }
    const weekdayNames = ['月','火','水','木','金','土','日'];
    const byDay = Array.from({length: 7}, () => ({count: 0, totalDiff: 0}));
    sessions.forEach(s => {
      const d = new Date(s.date).getDay();
      const idx = d === 0 ? 6 : d - 1;
      byDay[idx].count++;
      byDay[idx].totalDiff += s.diff_yen || 0;
    });
    const withData = byDay.map((d, i) => ({
      day: weekdayNames[i], day_index: i,
      avg_diff: d.count ? Math.round(d.totalDiff / d.count) : null,
      count: d.count,
    })).filter(d => d.count > 0);
    if (!withData.length) { el.innerHTML = '<p class="hint">まだ記録がありません</p>'; return; }
    const maxAbs = Math.max(...withData.map(d => Math.abs(d.avg_diff)));
    el.innerHTML = `
      <div style="font-size:.72rem;color:var(--text3);margin-bottom:8px">自分の収支実績より（平均差額）</div>
      ${withData.sort((a,b) => b.avg_diff - a.avg_diff).map(d => {
        const pct = maxAbs > 0 ? Math.round(Math.abs(d.avg_diff) / maxAbs * 100) : 0;
        const color = d.avg_diff >= 0 ? 'var(--success)' : 'var(--danger)';
        const sign = d.avg_diff >= 0 ? '+' : '';
        return `
          <div class="weekday-bar-row">
            <span class="weekday-label">${esc(d.day)}</span>
            <div class="weekday-track">
              <div class="weekday-fill" style="width:${pct}%;background:${color};opacity:.8">
                ${pct > 25 ? `<span class="weekday-fill-text">${sign}${d.avg_diff.toLocaleString()}</span>` : ''}
              </div>
            </div>
            ${pct <= 25 ? `<span class="weekday-score" style="color:${color}">${sign}${d.avg_diff.toLocaleString()}</span>` : ''}
          </div>
        `;
      }).join('')}
    `;
  } catch { el.innerHTML = '<p class="hint">データ取得失敗</p>'; }
}

function renderWeekdayChart(weekdays) {
  const el = document.getElementById('hall-weekday-chart');
  const max = Math.max(...weekdays.map(d => d.avg_score));
  const sorted = [...weekdays].sort((a, b) => b.avg_score - a.avg_score);
  el.innerHTML = sorted.map(d => {
    const pct = Math.round(d.avg_score / max * 100);
    const color = d.avg_score >= 1.95 ? 'linear-gradient(90deg,#a855f7,#6366f1)' :
                  d.avg_score >= 1.85 ? 'linear-gradient(90deg,#6366f1,#0ea5e9)' :
                  d.avg_score >= 1.75 ? 'linear-gradient(90deg,#0ea5e9,#22c55e)' :
                  'linear-gradient(90deg,#64748b,#475569)';
    return `
      <div class="weekday-bar-row">
        <span class="weekday-label">${esc(d.day)}</span>
        <div class="weekday-track">
          <div class="weekday-fill" style="width:${pct}%;background:${color}">
            ${pct > 30 ? `<span class="weekday-fill-text">${d.avg_score.toFixed(2)}</span>` : ''}
          </div>
        </div>
        ${pct <= 30 ? `<span class="weekday-score">${d.avg_score.toFixed(2)}</span>` : ''}
      </div>
    `;
  }).join('');
}

function renderMachineRanking(machines) {
  const el = document.getElementById('hall-machine-ranking');
  el.innerHTML = machines.slice(0, 10).map((m, i) => {
    const medal = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : '';
    return `
      <div class="machine-rank-row">
        <span class="rank-num">${medal || (i + 1)}</span>
        <span class="rank-machine">${esc(m.machine)}</span>
        <div style="text-align:right">
          <span class="rank-score">+${m.score}pt</span><br>
          <span class="rank-meta">${m.appearances}回 avg${m.avg}</span>
        </div>
      </div>
    `;
  }).join('');
}

function renderSpecialDays(days) {
  const el = document.getElementById('hall-special-days');
  el.innerHTML = Object.entries(days).map(([label, d]) => {
    const vs = d.vs_normal > 0 ? `<span style="color:var(--success)">+${d.vs_normal.toFixed(2)}</span>` :
               d.vs_normal < 0 ? `<span style="color:var(--danger)">${d.vs_normal.toFixed(2)}</span>` :
               '<span style="color:var(--text3)">±0</span>';
    return `
      <div class="special-day-row">
        <div>
          <div class="special-day-label">${esc(label)}</div>
          <div style="font-size:.75rem;color:var(--text3)">${d.sample_days || d.days || 0}日間サンプル</div>
        </div>
        <div style="text-align:right">
          <div class="special-day-score">${d.avg_score.toFixed(2)}</div>
          <div class="special-day-vs">通常比 ${vs}</div>
        </div>
      </div>
    `;
  }).join('');
}

function renderTodayRecommend(data) {
  const el = document.getElementById('hall-today-recommend');
  const today = new Date();
  const weekdayIdx = (today.getDay() + 6) % 7; // 0=月〜6=日
  const weekdayNames = ['月','火','水','木','金','土','日'];
  const dayOfMonth = today.getDate();
  const digit = dayOfMonth % 10;

  const dayScore = data.weekday_scores.find(d => d.day_index === weekdayIdx);
  const topMachines = data.machine_scores.slice(0, 3).map(m => m.machine);

  const isSpecial5 = digit === 5 || dayOfMonth === 5 || dayOfMonth === 15 || dayOfMonth === 25;
  const isSpecial8 = digit === 8;

  const items = [];

  if (dayScore) {
    const rating = dayScore.avg_score >= 1.95 ? '🔥 今日は熱い！' :
                   dayScore.avg_score >= 1.85 ? '✅ 良好な曜日' :
                   dayScore.avg_score >= 1.75 ? '📊 平均的' : '🥶 期待薄';
    items.push({
      icon: '📅',
      text: `今日(${weekdayNames[weekdayIdx]}): ${rating}`,
      sub: `曜日平均スコア ${dayScore.avg_score.toFixed(2)} (7曜日中 ${data.weekday_scores.sort((a,b)=>b.avg_score-a.avg_score).findIndex(d=>d.day_index===weekdayIdx)+1}位)`,
    });
  }

  if (isSpecial5) {
    items.push({ icon: '5️⃣', text: '5のつく日: 通常とほぼ同じ', sub: '過度な期待は禁物。通常日と同水準。' });
  }
  if (isSpecial8) {
    items.push({ icon: '8️⃣', text: '8のつく日: 通常より低い傾向', sub: '過去データでは通常日より -0.22 低い。要注意。' });
  }

  items.push({
    icon: '🏆',
    text: `推奨機種: ${topMachines.join('、')}`,
    sub: '過去スコア上位3機種。設定が入りやすい傾向あり。',
  });

  items.push({
    icon: '⚠',
    text: '注意: データは推測の参考程度に',
    sub: '「高設定がある日」の判断はツールを補助として活用。規律ある資金管理が最重要。',
  });

  el.innerHTML = items.map(item => `
    <div class="recommend-item">
      <span class="recommend-icon">${item.icon}</span>
      <div>
        <div class="recommend-text">${item.text}</div>
        <div class="recommend-sub">${item.sub}</div>
      </div>
    </div>
  `).join('');
}

document.getElementById('hall-select').addEventListener('change', () => {
  const v = document.getElementById('hall-select').value;
  const customInput = document.getElementById('hall-custom-input');
  if (v === '__custom__') {
    customInput.style.display = 'block';
    customInput.focus();
  } else {
    customInput.style.display = 'none';
    loadHallPage();
  }
});

document.getElementById('hall-custom-input').addEventListener('change', () => {
  const name = document.getElementById('hall-custom-input').value.trim();
  if (name) loadHallPage();
});

function getSelectedHall() {
  const v = document.getElementById('hall-select').value;
  if (v === '__custom__') {
    return document.getElementById('hall-custom-input').value.trim() || '';
  }
  return v;
}

// ---------------------------------------------------------------------------
// Scrape UI (みんレポ)
// ---------------------------------------------------------------------------

let _scrapePoller = null;

async function loadScrapeStatus() {
  const hall = getSelectedHall();
  if (!hall) return;
  const bar = document.getElementById('scrape-status-bar');
  try {
    const s = await apiFetch(`/api/hall/scrape_status?hall_name=${encodeURIComponent(hall)}`);
    const statusMap = { running: '取得中...', done: '取得済み', error: 'エラー' };
    const label = statusMap[s.status] || (s.scraped_days > 0 ? '取得済み' : '未取得');
    const dateStr = s.latest_date ? ` (最新: ${s.latest_date}, ${s.scraped_days}日分)` : '';
    bar.textContent = label + dateStr;

    if (s.status === 'running') {
      document.getElementById('scrape-btn').disabled = true;
      if (!_scrapePoller) {
        _scrapePoller = setInterval(() => loadScrapeStatus(), 3000);
      }
    } else {
      document.getElementById('scrape-btn').disabled = false;
      if (_scrapePoller) { clearInterval(_scrapePoller); _scrapePoller = null; }
    }

    if (s.scraped_days > 0) {
      await loadScrapeDates(hall);
    }
  } catch(e) {
    bar.textContent = 'ステータス取得失敗';
  }
}

async function loadScrapeDates(hall) {
  const dates = await apiFetch(`/api/hall/report_dates?hall_name=${encodeURIComponent(hall)}`);
  const sel = document.getElementById('scrape-date-select');
  const row = document.getElementById('scrape-date-row');
  if (!dates || dates.length === 0) { row.style.display = 'none'; return; }
  row.style.display = 'block';
  sel.innerHTML = dates.map(d => `<option value="${d}">${d}</option>`).join('');
  // 最初の日付のレポートを即表示
  loadScrapeReport(hall, dates[0]);
  // 過去30日ランキング
  loadTopMachines(hall);
}

async function loadScrapeReport(hall, date) {
  const el = document.getElementById('scrape-report-table');
  el.innerHTML = '<p class="hint">読み込み中...</p>';
  try {
    const rows = await apiFetch(`/api/hall/report?hall_name=${encodeURIComponent(hall)}&report_date=${date}&limit=30`);
    if (!rows || rows.length === 0) { el.innerHTML = '<p class="hint">データなし</p>'; return; }
    el.innerHTML = `
      <table style="width:100%;font-size:.78rem;border-collapse:collapse">
        <thead>
          <tr style="color:var(--text3);border-bottom:1px solid var(--border)">
            <th style="text-align:left;padding:4px 2px">機種</th>
            <th style="text-align:right;padding:4px 2px">差枚</th>
            <th style="text-align:right;padding:4px 2px">G数</th>
            <th style="text-align:right;padding:4px 2px">出率</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((r, i) => {
            const diff = r.avg_diff_coins;
            const diffColor = diff > 0 ? 'var(--up)' : diff < 0 ? 'var(--down)' : 'var(--text2)';
            const diffStr = diff != null ? (diff > 0 ? '+' : '') + diff.toLocaleString() : '-';
            const encHall = encodeURIComponent(hall).replace(/'/g, '%27');
            const encMachine = encodeURIComponent(r.machine_name).replace(/'/g, '%27');
            return `<tr style="border-bottom:1px solid var(--border-subtle);cursor:pointer"
                        onclick="renderMachineTrendChart(decodeURIComponent('${encHall}'),decodeURIComponent('${encMachine}'))">
              <td style="padding:5px 2px;max-width:130px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">
                <span style="color:var(--text3);margin-right:4px">${i+1}</span>
                <span style="color:var(--primary);text-decoration:underline dotted">${r.machine_name}</span>
              </td>
              <td style="text-align:right;padding:5px 2px;color:${diffColor};font-weight:600">${diffStr}</td>
              <td style="text-align:right;padding:5px 2px;color:var(--text3)">${r.avg_games != null ? r.avg_games.toLocaleString() : '-'}</td>
              <td style="text-align:right;padding:5px 2px;color:var(--text2)">${r.ev_pct != null ? r.ev_pct + '%' : '-'}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>`;
  } catch(e) {
    el.innerHTML = '<p class="hint">取得失敗: ' + e.message + '</p>';
  }
}

async function loadTopMachines(hall) {
  const card = document.getElementById('scrape-trend-card');
  const el = document.getElementById('scrape-top-machines');
  card.style.display = 'block';
  try {
    const rows = await apiFetch(`/api/hall/top_machines?hall_name=${encodeURIComponent(hall)}&days=30&limit=15`);
    if (!rows || rows.length === 0) { card.style.display = 'none'; return; }
    const maxDiff = Math.max(...rows.map(r => Math.abs(r.avg_diff || 0)), 1);
    el.innerHTML = rows.map((r, i) => {
      const diff = r.avg_diff || 0;
      const pct = Math.round(Math.abs(diff) / maxDiff * 100);
      const barColor = diff >= 0 ? 'var(--up)' : 'var(--down)';
      const diffStr = (diff > 0 ? '+' : '') + Math.round(diff).toLocaleString();
      return `<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
        <span style="width:20px;text-align:right;font-size:.72rem;color:var(--text3)">${i+1}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${r.machine_name}</div>
          <div style="height:4px;background:var(--border);border-radius:2px;margin-top:2px">
            <div style="width:${pct}%;height:4px;background:${barColor};border-radius:2px"></div>
          </div>
        </div>
        <span style="font-size:.78rem;font-weight:600;color:${barColor};white-space:nowrap">${diffStr}</span>
        <span style="font-size:.68rem;color:var(--text3);white-space:nowrap">${r.report_count}日</span>
      </div>`;
    }).join('');
  } catch(e) {
    card.style.display = 'none';
  }
}

document.getElementById('scrape-btn').addEventListener('click', async () => {
  const hall = getSelectedHall();
  const btn = document.getElementById('scrape-btn');
  btn.disabled = true;
  try {
    await apiFetch(`/api/hall/scrape?hall_name=${encodeURIComponent(hall)}&days=30`, { method: 'POST' });
    showToast('スクレイプ開始しました。しばらくお待ちください。');
    // ポーリング開始
    _scrapePoller = setInterval(() => loadScrapeStatus(), 3000);
  } catch(e) {
    showToast('スクレイプ開始失敗: ' + e.message, 'error');
    btn.disabled = false;
  }
});

document.getElementById('scrape-date-select').addEventListener('change', (e) => {
  const hall = getSelectedHall();
  loadScrapeReport(hall, e.target.value);
});

// ---------------------------------------------------------------------------
// アナスロ 台番別データ
// ---------------------------------------------------------------------------
let _anasloPoller = null;
let _anasloTab = 'seat';

async function loadAnasloStatus() {
  const hall = getSelectedHall();
  if (!hall) return;
  const bar = document.getElementById('anaslo-status-bar');
  try {
    const s = await fetch(`/api/hall/anaslo_status?hall_name=${encodeURIComponent(hall)}`).then(r => r.json());
    if (s.status === 'running') {
      bar.textContent = '⏳ データ取得中...';
      document.getElementById('anaslo-scrape-btn').disabled = true;
      if (!_anasloPoller) {
        _anasloPoller = setInterval(() => loadAnasloStatus(), 3000);
      }
    } else {
      document.getElementById('anaslo-scrape-btn').disabled = false;
      if (_anasloPoller) { clearInterval(_anasloPoller); _anasloPoller = null; }
      if (s.scraped_days > 0) {
        bar.textContent = `${s.scraped_days}日分のデータあり（最新: ${s.latest_date}）`;
        await loadAnasloSeatDates(hall);
        if (_anasloTab === 'tail') loadAnasloTailAnalysis(hall);
      } else {
        bar.textContent = 'データなし。「取得」ボタンでアナスロからデータを取得します。';
      }
    }
  } catch(e) {
    bar.textContent = 'ステータス取得失敗';
  }
}

async function loadAnasloSeatDates(hall) {
  const dates = await fetch(`/api/hall/seat_dates?hall_name=${encodeURIComponent(hall)}`).then(r => r.json());
  const sel = document.getElementById('anaslo-date-select');
  const row = document.getElementById('anaslo-date-row');
  if (!dates.length) { row.style.display = 'none'; return; }
  sel.innerHTML = dates.map(d => `<option value="${d}">${d}</option>`).join('');
  row.style.display = '';
  loadAnasloSeatReport(hall, dates[0]);
}

async function loadAnasloSeatReport(hall, date) {
  const container = document.getElementById('anaslo-seat-table');
  container.innerHTML = '<p class="hint center">読み込み中...</p>';
  try {
    const rows = await fetch(`/api/hall/seat_report?hall_name=${encodeURIComponent(hall)}&date=${date}&limit=50`).then(r => r.json());
    if (!rows.length) { container.innerHTML = '<p class="hint center">データなし</p>'; return; }
    const html = `<div style="overflow-x:auto">
      <table style="width:100%;font-size:.78rem;border-collapse:collapse">
        <thead><tr style="background:var(--bg2);color:var(--text2)">
          <th style="padding:4px 6px;text-align:center">台番</th>
          <th style="padding:4px 6px;text-align:left">機種</th>
          <th style="padding:4px 6px;text-align:right">差枚</th>
          <th style="padding:4px 6px;text-align:right">G数</th>
          <th style="padding:4px 6px;text-align:right">BB</th>
          <th style="padding:4px 6px;text-align:right">RB</th>
        </tr></thead>
        <tbody>${rows.map((r, i) => {
          const color = r.diff_coins > 3000 ? 'color:#e85' : r.diff_coins > 0 ? '' : 'color:var(--text3)';
          return `<tr style="border-bottom:1px solid var(--bg2);${color}">
            <td style="padding:4px 6px;text-align:center;font-weight:${i < 5 ? 'bold' : 'normal'}">${r.seat_number}</td>
            <td style="padding:4px 6px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.machine_name)}</td>
            <td style="padding:4px 6px;text-align:right;font-weight:bold">${r.diff_coins > 0 ? '+' : ''}${r.diff_coins?.toLocaleString() ?? '-'}</td>
            <td style="padding:4px 6px;text-align:right">${r.games?.toLocaleString() ?? '-'}</td>
            <td style="padding:4px 6px;text-align:right">${r.bb_count ?? '-'}</td>
            <td style="padding:4px 6px;text-align:right">${r.rb_count ?? '-'}</td>
          </tr>`;
        }).join('')}</tbody>
      </table></div>`;
    container.innerHTML = html;
  } catch(e) {
    container.innerHTML = `<p class="hint center">取得失敗</p>`;
  }
}

async function loadAnasloTailAnalysis(hall) {
  const container = document.getElementById('anaslo-tail-table');
  container.innerHTML = '<p class="hint center">読み込み中...</p>';
  try {
    const rows = await fetch(`/api/hall/tail_analysis?hall_name=${encodeURIComponent(hall)}&days=30`).then(r => r.json());
    if (!rows.length) { container.innerHTML = '<p class="hint center">データなし（末尾データなし）</p>'; return; }
    const html = `<p style="font-size:.75rem;color:var(--text3);margin-bottom:6px">過去30日・末尾別平均差枚</p>
      <div style="overflow-x:auto">
      <table style="width:100%;font-size:.8rem;border-collapse:collapse">
        <thead><tr style="background:var(--bg2);color:var(--text2)">
          <th style="padding:4px 8px;text-align:center">末尾</th>
          <th style="padding:4px 8px;text-align:right">平均差枚</th>
          <th style="padding:4px 8px;text-align:right">勝率</th>
          <th style="padding:4px 8px;text-align:right">サンプル</th>
        </tr></thead>
        <tbody>${rows.map(r => {
          const tail = r.tail.replace('末尾', '');
          const color = r.avg_diff > 500 ? 'color:#e85;font-weight:bold' : r.avg_diff < -500 ? 'color:var(--text3)' : '';
          return `<tr style="border-bottom:1px solid var(--bg2);${color}">
            <td style="padding:4px 8px;text-align:center;font-size:1rem;font-weight:bold">${esc(tail)}</td>
            <td style="padding:4px 8px;text-align:right">${r.avg_diff > 0 ? '+' : ''}${r.avg_diff.toLocaleString()}</td>
            <td style="padding:4px 8px;text-align:right">${r.win_rate}%</td>
            <td style="padding:4px 8px;text-align:right;color:var(--text3)">${r.count}</td>
          </tr>`;
        }).join('')}</tbody>
      </table></div>`;
    container.innerHTML = html;
  } catch(e) {
    container.innerHTML = `<p class="hint center">取得失敗</p>`;
  }
}

document.getElementById('anaslo-scrape-btn').addEventListener('click', async () => {
  const hall = getSelectedHall();
  if (!hall) { showToast('店舗を選択してください', 'error'); return; }
  const btn = document.getElementById('anaslo-scrape-btn');
  btn.disabled = true;
  document.getElementById('anaslo-status-bar').textContent = '⏳ 開始中...';
  try {
    await fetch(`/api/hall/anaslo_scrape?hall_name=${encodeURIComponent(hall)}&days=30`, { method: 'POST' });
    showToast('アナスロ取得開始。約3〜5分かかります。');
    _anasloPoller = setInterval(() => loadAnasloStatus(), 3000);
  } catch(e) {
    showToast('取得開始失敗: ' + e.message, 'error');
    btn.disabled = false;
  }
});

document.getElementById('anaslo-date-select').addEventListener('change', (e) => {
  const hall = getSelectedHall();
  loadAnasloSeatReport(hall, e.target.value);
});

document.getElementById('anaslo-tab-seat').addEventListener('click', () => {
  _anasloTab = 'seat';
  document.getElementById('anaslo-tab-seat').className = 'btn btn-primary';
  document.getElementById('anaslo-tab-tail').className = 'btn btn-ghost';
  document.getElementById('anaslo-seat-section').style.display = '';
  document.getElementById('anaslo-tail-section').style.display = 'none';
});

document.getElementById('anaslo-tab-tail').addEventListener('click', () => {
  _anasloTab = 'tail';
  document.getElementById('anaslo-tab-tail').className = 'btn btn-primary';
  document.getElementById('anaslo-tab-seat').className = 'btn btn-ghost';
  document.getElementById('anaslo-seat-section').style.display = 'none';
  document.getElementById('anaslo-tail-section').style.display = '';
  loadAnasloTailAnalysis(getSelectedHall());
});

// ---------------------------------------------------------------------------
// Machines page
// ---------------------------------------------------------------------------
async function loadMachinesPage() {
  const container = document.getElementById('machine-list');
  container.innerHTML = '<p class="hint center">読み込み中...</p>';
  try {
    const machines = await api.getMachines();
    const profiles = await Promise.all(
      machines.map(m => api.getMachine(m).catch(() => ({ machine_name: m, elements: [], settings: [] })))
    );
    renderMachineList(profiles);
  } catch (e) {
    container.innerHTML = `<p class="hint center">取得失敗: ${esc(e.message)}</p>`;
  }
}

function renderMachineList(profiles) {
  const container = document.getElementById('machine-list');
  const search = document.getElementById('machine-search');

  function categorize(name) {
    if (/ジャグラー/.test(name)) return 'ジャグラー系';
    if (/ハナハナ/.test(name)) return 'ハナハナ系';
    if (/スマスロ|^S[^マ]|^L[^S]/.test(name)) return 'スマスロ系';
    if (/バジリスク|絆/.test(name)) return 'バジリスク系';
    if (/カバネリ|甲鉄城/.test(name)) return 'カバネリ系';
    if (/北斗/.test(name)) return '北斗系';
    return 'その他';
  }

  function render(filter = '') {
    const filtered = filter
      ? profiles.filter(p => p.machine_name.includes(filter))
      : profiles;

    if (!filtered.length) {
      container.innerHTML = '<p class="hint center">機種が見つかりません</p>';
      return;
    }

    // カテゴリ別グループ化
    const groups = {};
    filtered.forEach(p => {
      const cat = categorize(p.machine_name);
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(p);
    });
    const catOrder = ['ジャグラー系', 'ハナハナ系', 'スマスロ系', 'バジリスク系', 'カバネリ系', '北斗系', 'その他'];
    const sortedGroups = catOrder.filter(c => groups[c]);
    // フィルター時はグループ表示なし
    const useGroups = !filter && sortedGroups.length > 1;

    container.innerHTML = (useGroups ? sortedGroups : ['__all__']).map(cat => {
      const items = useGroups ? groups[cat] : filtered;
      const header = useGroups ? `<div style="font-size:.72rem;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;padding:8px 4px 4px">${esc(cat)}</div>` : '';
      return header + items.map(p => {
      const settings = p.settings || [];
      const kwTags = p.machine_kw
        ? Object.entries(p.machine_kw).map(([s, kw]) => {
            const color = kw >= 1.05 ? 'var(--success)' : kw >= 1.0 ? 'var(--warning)' : 'var(--danger)';
            return `<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:.72rem;font-weight:700;color:${color};background:${color}22">設${s}: ${(kw*100).toFixed(1)}%</span>`;
          }).join(' ')
        : '<span style="font-size:.75rem;color:var(--text3)">機械割未登録</span>';

      // 確率テーブル
      const els = p.elements || [];
      const probTable = els.length && settings.length ? `
        <div class="prob-table-wrap" style="display:none;margin-top:10px;overflow-x:auto">
          <table style="width:100%;border-collapse:collapse;font-size:.72rem">
            <thead>
              <tr>
                <th style="padding:4px 6px;text-align:left;color:var(--text3);border-bottom:1px solid var(--border)">要素</th>
                ${settings.map(s => `<th style="padding:4px 6px;text-align:center;color:var(--s${s});border-bottom:1px solid var(--border)">設${s}</th>`).join('')}
              </tr>
            </thead>
            <tbody>
              ${els.map(el => {
                const ps = el.probabilities || el.p || {};
                return `<tr>
                  <td style="padding:4px 6px;color:var(--text2)">${esc(el.name)}</td>
                  ${settings.map(s => {
                    const v = ps[s] || 0;
                    const display = v >= 0.01 ? `1/${(1/v).toFixed(0)}` : v > 0 ? `1/${(1/v).toFixed(0)}` : '-';
                    return `<td style="padding:4px 6px;text-align:center;font-variant-numeric:tabular-nums">${display}</td>`;
                  }).join('')}
                </tr>`;
              }).join('')}
              ${p.machine_kw ? `<tr style="border-top:1px solid var(--border)">
                <td style="padding:4px 6px;color:var(--text3);font-weight:600">機械割</td>
                ${settings.map(s => {
                  const kw = p.machine_kw[s];
                  const color = kw >= 1.05 ? 'var(--success)' : kw >= 1.0 ? 'var(--warning)' : 'var(--danger)';
                  return kw ? `<td style="padding:4px 6px;text-align:center;color:${color};font-weight:700">${(kw*100).toFixed(1)}%</td>` : '<td>-</td>';
                }).join('')}
              </tr>` : ''}
            </tbody>
          </table>
          <button class="btn btn-primary btn-sm go-estimate-btn" style="width:100%;margin-top:10px;padding:10px">
            この機種で推測する →
          </button>
        </div>
      ` : `<button class="btn btn-primary btn-sm go-estimate-btn" style="width:100%;margin-top:10px;display:none">
            この機種で推測する →
          </button>`;

      return `
        <div class="machine-card" data-name="${esc(p.machine_name)}">
          <div style="display:flex;align-items:center;justify-content:space-between">
            <div class="machine-card-name">${esc(p.machine_name)}</div>
            <span class="expand-arrow" style="color:var(--text3);font-size:.8rem;transition:transform .2s">▶</span>
          </div>
          <div class="machine-card-meta">
            <span class="machine-tag">設定: ${settings.join('・')}</span>
            <span class="machine-tag">${els.length}要素</span>
          </div>
          <div class="machine-card-meta" style="margin-top:6px">${kwTags}</div>
          <div class="machine-stats-wrap" style="display:none"></div>
          ${probTable}
        </div>
      `;
      }).join('');
    }).join('');

    // カードタップで確率テーブル展開、ボタンで推測ページへ
    container.querySelectorAll('.machine-card').forEach(card => {
      card.addEventListener('click', async (e) => {
        if (e.target.closest('.go-estimate-btn')) {
          const name = card.dataset.name;
          estMachine.value = name;
          estMachine.dispatchEvent(new Event('change'));
          switchTab('estimate');
          return;
        }
        const wrap = card.querySelector('.prob-table-wrap') || card.querySelector('.go-estimate-btn');
        const arrow = card.querySelector('.expand-arrow');
        const statsWrap = card.querySelector('.machine-stats-wrap');
        if (wrap) {
          const open = wrap.style.display !== 'none';
          wrap.style.display = open ? 'none' : 'block';
          if (statsWrap) statsWrap.style.display = open ? 'none' : 'block';
          if (arrow) arrow.style.transform = open ? '' : 'rotate(90deg)';
          // 初回展開時に個人統計を取得
          if (!open && statsWrap && !statsWrap.dataset.loaded) {
            statsWrap.dataset.loaded = '1';
            statsWrap.innerHTML = '<p style="font-size:.72rem;color:var(--text3);padding:4px 0">統計読み込み中...</p>';
            try {
              const stats = await apiFetch(`/api/machine/stats?machine_name=${encodeURIComponent(card.dataset.name)}`);
              if (stats.total_sessions === 0) {
                statsWrap.innerHTML = '<p style="font-size:.72rem;color:var(--text3)">まだ記録がありません</p>';
              } else {
                const wr = Math.round((stats.win_rate || 0) * 100);
                const diff = stats.diff_yen || 0;
                const sign = diff >= 0 ? '+' : '';
                const avgG = stats.total_sessions > 0 ? Math.round((stats.total_games||0) / stats.total_sessions) : 0;
                const recentRows = (stats.recent_sessions || []).map(s => {
                  const d = s.diff_yen || 0;
                  const sg = d >= 0 ? '+' : '';
                  const est = s.posterior ? (() => {
                    const p = typeof s.posterior === 'string' ? JSON.parse(s.posterior) : s.posterior;
                    const e = Object.entries(p).reduce((a,[k,v]) => a + parseInt(k)*v, 0);
                    return `<span style="color:var(--text3);font-size:.7rem">推測設定${e.toFixed(1)}</span>`;
                  })() : '';
                  return `<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-size:.75rem">
                    <span style="color:var(--text3)">${s.date||''} ${s.hall_name||''} #${s.seat_number||'-'}</span>
                    <span style="display:flex;align-items:center;gap:8px">${est}<strong style="color:${d>=0?'var(--success)':'var(--danger)'}">${sg}${(d||0).toLocaleString()}円</strong></span>
                  </div>`;
                }).join('');
                statsWrap.innerHTML = `
                  <div style="border-top:1px solid var(--border);margin-top:8px;padding-top:8px">
                    <div style="display:flex;gap:12px;flex-wrap:wrap;font-size:.78rem;margin-bottom:8px">
                      <span><span style="color:var(--text3)">回数</span> <strong>${stats.total_sessions}</strong></span>
                      <span><span style="color:var(--text3)">勝率</span> <strong style="color:${wr>=50?'var(--success)':'var(--danger)'}">${wr}%</strong></span>
                      <span><span style="color:var(--text3)">累計収支</span> <strong style="color:${diff>=0?'var(--success)':'var(--danger)'}">${sign}${diff.toLocaleString()}円</strong></span>
                      <span><span style="color:var(--text3)">平均G数</span> <strong>${avgG.toLocaleString()}</strong></span>
                      ${stats.avg_estimated_setting ? `<span><span style="color:var(--text3)">平均推測設定</span> <strong style="color:var(--primary-h)">${stats.avg_estimated_setting}</strong></span>` : ''}
                    </div>
                    ${recentRows ? `<div style="font-size:.7rem;color:var(--text3);margin-bottom:4px;font-weight:600">直近${(stats.recent_sessions||[]).length}回</div>${recentRows}` : ''}
                  </div>
                `;
              }
            } catch { statsWrap.innerHTML = ''; }
          }
        }
      });
    });
  }

  render();
  search.addEventListener('input', () => render(search.value));
}

// ---------------------------------------------------------------------------
// Setting change detection
// ---------------------------------------------------------------------------
const cdSection = document.getElementById('change-detect-section');
const cdEarlyGames = document.getElementById('cd-early-games');
const cdLateGames = document.getElementById('cd-late-games');
const cdCountsArea = document.getElementById('cd-counts-area');
const cdRunBtn = document.getElementById('cd-run-btn');
const cdResult = document.getElementById('cd-result');

// 機種変更時に変更検知フォームも更新する（estMachine changeイベントから呼ばれる）

function renderCdCounts(profile) {
  if (!profile || !cdCountsArea) return;
  const elements = profile.elements || [];
  cdCountsArea.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px">
      <div>
        <div style="font-size:.75rem;color:var(--text3);margin-bottom:6px;font-weight:600">前半カウント</div>
        ${elements.map(el => `
          <div style="margin-bottom:6px">
            <label class="form-label" style="font-size:.75rem">${esc(el.name)}</label>
            <input type="number" class="form-input" style="padding:7px 10px"
                   min="0" value="0" inputmode="numeric" data-el="${esc(el.name)}" data-phase="early">
          </div>
        `).join('')}
      </div>
      <div>
        <div style="font-size:.75rem;color:var(--text3);margin-bottom:6px;font-weight:600">後半カウント</div>
        ${elements.map(el => `
          <div style="margin-bottom:6px">
            <label class="form-label" style="font-size:.75rem">${esc(el.name)}</label>
            <input type="number" class="form-input" style="padding:7px 10px"
                   min="0" value="0" inputmode="numeric" data-el="${esc(el.name)}" data-phase="late">
          </div>
        `).join('')}
      </div>
    </div>
  `;
}

if (cdRunBtn) {
  cdRunBtn.addEventListener('click', async () => {
    if (!state.currentMachine || !state.currentProfile) return;
    const earlyGames = parseInt(cdEarlyGames.value) || 0;
    const lateGames = parseInt(cdLateGames.value) || 0;
    if (!earlyGames || !lateGames) { showToast('前半・後半G数を入力してください', 'error'); return; }

    const earlyCounts = {}, lateCounts = {};
    cdCountsArea.querySelectorAll('input[data-el]').forEach(inp => {
      const v = parseInt(inp.value) || 0;
      if (v > 0) {
        if (inp.dataset.phase === 'early') earlyCounts[inp.dataset.el] = v;
        else lateCounts[inp.dataset.el] = v;
      }
    });

    try {
      const result = await apiFetch('/api/setting_change', {
        method: 'POST',
        body: JSON.stringify({
          machine_name: state.currentMachine,
          early_games: earlyGames,
          late_games: lateGames,
          early_counts: earlyCounts,
          late_counts: lateCounts,
        }),
      });
      renderCdResult(result);
    } catch (e) {
      showToast('分析エラー: ' + e.message, 'error');
    }
  });
}

function renderCdResult(r) {
  if (!cdResult) return;
  cdResult.style.display = 'block';
  const prob = (r.change_prob * 100).toFixed(1);
  const color = r.change_prob >= 0.6 ? 'var(--danger)' :
                r.change_prob >= 0.35 ? 'var(--warning)' : 'var(--success)';
  cdResult.innerHTML = `
    <div style="border:1px solid var(--border);border-radius:8px;overflow:hidden">
      <div style="padding:12px 14px;background:${r.change_prob>=0.5?'rgba(239,68,68,.1)':'rgba(34,197,94,.07)'}">
        <div style="display:flex;align-items:center;justify-content:space-between">
          <span style="font-weight:700;color:${color}">変更確率: ${prob}%</span>
          <span style="font-size:.8rem;color:var(--text3)">${r.verdict}</span>
        </div>
        <div style="margin-top:6px">
          <div style="height:6px;background:var(--bg3);border-radius:3px;overflow:hidden">
            <div style="height:100%;width:${Math.round(r.change_prob*100)}%;background:${color};border-radius:3px;transition:width .5s"></div>
          </div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;padding:12px 14px;gap:10px">
        <div style="text-align:center">
          <div style="font-size:.7rem;color:var(--text3)">前半 推測設定</div>
          <div style="font-size:1.1rem;font-weight:700">${r.early_setting}</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:.7rem;color:var(--text3)">後半 推測設定</div>
          <div style="font-size:1.1rem;font-weight:700">${r.late_setting}</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:.7rem;color:var(--text3)">全体 推測設定</div>
          <div style="font-size:1.1rem;font-weight:700">${r.combined_setting}</div>
        </div>
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// PnL Chart (SVG累計収支)
// ---------------------------------------------------------------------------
function renderPnLChart(sessions) {
  const card = document.getElementById('pnl-chart-card');
  const svg = document.getElementById('pnl-chart');
  if (!sessions.length) { card.style.display = 'none'; return; }
  card.style.display = 'block';

  const sorted = [...sessions].sort((a, b) => a.date < b.date ? -1 : 1);
  let cum = 0;
  const points = [0, ...sorted.map(s => { cum += (s.diff_yen || 0); return cum; })];

  const W = svg.parentElement.offsetWidth - 32 || 300;
  const H = 120;
  const padL = 48, padR = 10, padT = 10, padB = 24;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;

  const minV = Math.min(...points);
  const maxV = Math.max(...points);
  const range = maxV - minV || 1;
  const toX = i => padL + (i / (points.length - 1)) * innerW;
  const toY = v => padT + innerH - ((v - minV) / range) * innerH;

  const zeroY = toY(0);
  const linePts = points.map((v, i) => `${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join(' ');
  const areaTop = `${toX(0).toFixed(1)},${Math.min(zeroY, toY(points[0])).toFixed(1)} ` + linePts;
  const areaBottom = ` ${toX(points.length - 1).toFixed(1)},${zeroY.toFixed(1)} ${toX(0).toFixed(1)},${zeroY.toFixed(1)}`;

  const lastVal = points[points.length - 1];
  const isPos = lastVal >= 0;
  const lastColor = isPos ? '#10b981' : '#f43f5e';
  const glowColor = isPos ? 'rgba(16,185,129,0.5)' : 'rgba(244,63,94,0.5)';
  const gradId = `pnl-grad-${Date.now()}`;
  const fillId = `pnl-fill-${Date.now()}`;

  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = `
    <defs>
      <linearGradient id="${gradId}" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%" stop-color="${lastColor}" stop-opacity="0.8"/>
        <stop offset="100%" stop-color="${isPos ? '#34d399' : '#fb7185'}" stop-opacity="1"/>
      </linearGradient>
      <linearGradient id="${fillId}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${lastColor}" stop-opacity="0.25"/>
        <stop offset="100%" stop-color="${lastColor}" stop-opacity="0.02"/>
      </linearGradient>
      <filter id="pnl-glow">
        <feGaussianBlur stdDeviation="2.5" result="blur"/>
        <feComposite in="SourceGraphic" in2="blur" operator="over"/>
      </filter>
    </defs>
    <!-- Zero line -->
    ${zeroY > padT && zeroY < padT+innerH
      ? `<line x1="${padL}" y1="${zeroY.toFixed(1)}" x2="${padL+innerW}" y2="${zeroY.toFixed(1)}" stroke="rgba(255,255,255,0.1)" stroke-width="1" stroke-dasharray="4,4"/>`
      : ''}
    <!-- Area fill -->
    <polygon points="${areaTop}${areaBottom}" fill="url(#${fillId})"/>
    <!-- Glow line (wide, blurred) -->
    <polyline points="${linePts}" fill="none" stroke="${glowColor}" stroke-width="5" stroke-linejoin="round" filter="url(#pnl-glow)" opacity="0.6"/>
    <!-- Main line -->
    <polyline points="${linePts}" fill="none" stroke="url(#${gradId})" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    <!-- Last point dot -->
    <circle cx="${toX(points.length-1).toFixed(1)}" cy="${toY(lastVal).toFixed(1)}" r="4" fill="${lastColor}" stroke="rgba(7,9,15,0.8)" stroke-width="2"/>
    <!-- Labels -->
    <text x="${padL - 5}" y="${(padT + 5).toFixed(0)}" text-anchor="end" font-size="9" fill="rgba(180,200,230,0.45)" font-family="Inter,sans-serif">${(maxV/1000).toFixed(0)}k</text>
    <text x="${padL - 5}" y="${(padT + innerH + 1).toFixed(0)}" text-anchor="end" font-size="9" fill="rgba(180,200,230,0.45)" font-family="Inter,sans-serif">${(minV/1000).toFixed(0)}k</text>
    <!-- Current value label -->
    <text x="${(padL+innerW).toFixed(0)}" y="${Math.max(padT+13, Math.min(padT+innerH-4, toY(lastVal)-6)).toFixed(0)}"
          text-anchor="end" font-size="11" font-weight="700" fill="${lastColor}" font-family="Inter,sans-serif">${fmt(lastVal)}</text>
    <!-- Footer labels -->
    <text x="${padL}" y="${(H-5).toFixed(0)}" font-size="9" fill="rgba(180,200,230,0.35)" font-family="Inter,sans-serif">${sorted.length}回</text>
    <text x="${padL+innerW}" y="${(H-5).toFixed(0)}" text-anchor="end" font-size="9" fill="rgba(180,200,230,0.35)" font-family="Inter,sans-serif">${sorted[sorted.length-1]?.date || ''}</text>
  `;
}

// ---------------------------------------------------------------------------
// Monthly Stats
// ---------------------------------------------------------------------------
function renderMonthlyStats(sessions) {
  const card = document.getElementById('monthly-card');
  const el = document.getElementById('monthly-stats');
  if (!sessions.length) { card.style.display = 'none'; return; }
  card.style.display = 'block';

  const byMonth = {};
  for (const s of sessions) {
    const month = s.date.slice(0, 7); // YYYY-MM
    if (!byMonth[month]) byMonth[month] = { count: 0, diff: 0, wins: 0, games: 0 };
    byMonth[month].count++;
    byMonth[month].diff += s.diff_yen || 0;
    byMonth[month].games += s.games_total || 0;
    if ((s.diff_yen || 0) > 0) byMonth[month].wins++;
  }

  const months = Object.keys(byMonth).sort().reverse();
  el.innerHTML = months.map(m => {
    const d = byMonth[m];
    const wr = Math.round(d.wins / d.count * 100);
    return `
      <div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border)">
        <div style="width:60px;font-size:.85rem;font-weight:700;color:var(--text2)">${m}</div>
        <div style="flex:1">
          <div style="display:flex;gap:12px;flex-wrap:wrap">
            <span style="font-size:.82rem;color:var(--text2)">${d.count}回</span>
            <span style="font-size:.82rem;font-weight:700" class="${d.diff>=0?'diff-pos':'diff-neg'}">${fmt(d.diff)}</span>
            <span style="font-size:.82rem;color:var(--text3)">勝率${wr}%</span>
            <span style="font-size:.82rem;color:var(--text3)">${(d.games/1000).toFixed(1)}kG</span>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

// ---------------------------------------------------------------------------
// Seat analysis
// ---------------------------------------------------------------------------
function renderSeatAnalysis(sessions) {
  const card = document.getElementById('seat-card');
  const el = document.getElementById('seat-analysis');
  const withSeat = sessions.filter(s => s.seat_number != null);
  if (withSeat.length < 3) { card.style.display = 'none'; return; }
  card.style.display = 'block';

  const corners = withSeat.filter(s => s.is_corner);
  const nonCorners = withSeat.filter(s => !s.is_corner);

  function stats(arr) {
    if (!arr.length) return null;
    const diff = arr.reduce((s, r) => s + (r.diff_yen || 0), 0);
    const wins = arr.filter(s => (s.diff_yen || 0) > 0).length;
    const avgExp = arr.filter(s => s.posterior)
      .map(s => calcExpectedSetting(s.posterior));
    const avgSetting = avgExp.length ? avgExp.reduce((a, b) => a + b, 0) / avgExp.length : null;
    return { count: arr.length, diff, wr: Math.round(wins / arr.length * 100), avgSetting };
  }

  const cStats = stats(corners);
  const ncStats = stats(nonCorners);

  // 台番号末尾別
  const byDigit = {};
  for (const s of withSeat) {
    const d = s.seat_number % 10;
    if (!byDigit[d]) byDigit[d] = [];
    byDigit[d].push(s.diff_yen || 0);
  }

  el.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px">
      ${cStats ? `
        <div style="background:rgba(99,102,241,.1);border:1px solid rgba(99,102,241,.3);border-radius:8px;padding:12px">
          <div style="font-size:.75rem;color:var(--text3);margin-bottom:4px">角台 (${cStats.count}台)</div>
          <div style="font-weight:700;font-size:.9rem" class="${cStats.diff>=0?'diff-pos':'diff-neg'}">${fmt(cStats.diff)}</div>
          <div style="font-size:.78rem;color:var(--text2)">勝率${cStats.wr}%${cStats.avgSetting ? ' / 推測設定avg'+cStats.avgSetting.toFixed(1) : ''}</div>
        </div>
      ` : ''}
      ${ncStats ? `
        <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:12px">
          <div style="font-size:.75rem;color:var(--text3);margin-bottom:4px">非角台 (${ncStats.count}台)</div>
          <div style="font-weight:700;font-size:.9rem" class="${ncStats.diff>=0?'diff-pos':'diff-neg'}">${fmt(ncStats.diff)}</div>
          <div style="font-size:.78rem;color:var(--text2)">勝率${ncStats.wr}%${ncStats.avgSetting ? ' / 推測設定avg'+ncStats.avgSetting.toFixed(1) : ''}</div>
        </div>
      ` : ''}
    </div>
    <div style="font-size:.78rem;color:var(--text3);margin-bottom:6px">台番号末尾別 平均収支</div>
    <div style="display:flex;flex-wrap:wrap;gap:6px">
      ${Object.entries(byDigit).sort((a, b) => +a[0] - +b[0]).map(([d, diffs]) => {
        const avg = diffs.reduce((a, b) => a + b, 0) / diffs.length;
        return `<div style="padding:6px 10px;border-radius:6px;background:${avg>=0?'rgba(34,197,94,.15)':'rgba(239,68,68,.15)'};border:1px solid ${avg>=0?'rgba(34,197,94,.3)':'rgba(239,68,68,.3)'};font-size:.82rem">
          <span style="font-weight:700">末尾${d}</span>
          <span style="color:var(--text3);margin-left:4px">${diffs.length}回</span>
          <span style="display:block;font-size:.8rem" class="${avg>=0?'diff-pos':'diff-neg'}">${avg>=0?'+':''}${(avg/1000).toFixed(1)}k</span>
        </div>`;
      }).join('')}
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function esc(str) {
  return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function fmt(yen) {
  if (yen == null) return '--';
  const sign = yen >= 0 ? '+' : '';
  return sign + yen.toLocaleString() + '円';
}

// ---------------------------------------------------------------------------
// Keyboard shortcuts
// ---------------------------------------------------------------------------
// 推測ページ表示中、テキスト入力にフォーカスがない状態でキーを押すとカウントが増える
// キーとelement名の対応は機種ごとに最初の文字で自動マッピング
const KEY_SHORTCUTS = {
  'b': ['BB確率', 'BIG確率'],
  'r': ['RB確率', 'REG確率'],
  'g': ['ブドウ確率'],
  'w': ['スイカ確率'],
  'c': ['チェリー+RB確率', '強チェリー確率'],
  's': ['単独RB確率'],
  'a': ['ART確率'],
};

let _shortcutToast;
document.addEventListener('keydown', (e) => {
  // 推測ページかつ機種選択済みのとき
  if (!document.getElementById('page-estimate').classList.contains('active')) return;
  if (!state.currentMachine) return;
  // 入力フィールドにフォーカスしているときは無視
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;

  const key = e.key.toLowerCase();
  const targets = KEY_SHORTCUTS[key];
  if (!targets) return;

  const inputs = document.querySelectorAll('.count-input');
  let hit = false;
  for (const inp of inputs) {
    if (targets.some(t => inp.dataset.el?.startsWith(t) || (inp.dataset.el && t.includes(inp.dataset.el)))) {
      inp.value = (parseInt(inp.value) || 0) + 1;
      inp.dispatchEvent(new Event('input'));
      hit = true;
      break;
    }
  }
  // 前者でヒットしなければ前方一致で試みる
  if (!hit) {
    for (const inp of inputs) {
      if (inp.dataset.el && targets.some(t => inp.dataset.el.toLowerCase().startsWith(key))) {
        inp.value = (parseInt(inp.value) || 0) + 1;
        inp.dispatchEvent(new Event('input'));
        break;
      }
    }
  }
  e.preventDefault();
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
async function init() {
  await checkConnection();
  await loadMachineSelect();
  await populateSessionFilters();

  // ヘッダー日付表示
  const today = new Date();
  const dayNames = ['日', '月', '火', '水', '木', '金', '土'];
  const dateEl = document.getElementById('header-date');
  if (dateEl) {
    dateEl.textContent = `${today.getMonth()+1}/${today.getDate()}(${dayNames[today.getDay()]})`;
  }

  // 本日の曜日・日付を自動セット
  if (!document.getElementById('est-weekday').value) {
    // JSのgetDay(): 0=日, 1=月 ... 6=土
    // UIは 0=月,1=火,...,6=日 の順
    const jsDay = today.getDay(); // 0=Sun...6=Sat
    const uiDay = jsDay === 0 ? 6 : jsDay - 1; // Sun→6, Mon→0, ...
    document.getElementById('est-weekday').value = String(uiDay);
  }
  if (!document.getElementById('est-dom').value) {
    document.getElementById('est-dom').value = String(today.getDate());
  }

  // 前回のドラフトを復元
  try {
    const raw = localStorage.getItem('pachi_draft');
    if (raw) {
      const draft = JSON.parse(raw);
      if (draft.machine && state.machines.includes(draft.machine)) {
        estMachine.value = draft.machine;
        estMachine.dispatchEvent(new Event('change'));
      }
    }
  } catch { /* ignore */ }

  // API死活監視（30秒ごと）
  setInterval(checkConnection, 30000);
}

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------

let _dailyChart = null;
let _trendChart = null;
let _monthlyBarChart = null;
let _machineBreakdownChart = null;

const CHART_COLORS = {
  up:   '#10b981',
  down: '#f43f5e',
  line: '#818cf8',
  grid: 'rgba(255,255,255,0.05)',
  text: 'rgba(180,200,230,0.55)',
};

function chartDefaults() {
  return {
    responsive: true,
    maintainAspectRatio: true,
    animation: { duration: 500, easing: 'easeOutQuart' },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: 'rgba(12,17,32,0.95)',
        borderColor: 'rgba(255,255,255,0.1)',
        borderWidth: 1,
        titleColor: 'rgba(180,200,230,0.7)',
        bodyColor: '#eef2ff',
        padding: 10,
        cornerRadius: 8,
        callbacks: {
          label: ctx => {
            const v = ctx.parsed.y ?? ctx.parsed.x;
            return (v > 0 ? '+' : '') + v.toLocaleString() + (ctx.dataset.unit || '');
          }
        }
      }
    },
    scales: {
      x: {
        ticks: { color: CHART_COLORS.text, maxRotation: 45, font: { size: 10 } },
        grid: { color: CHART_COLORS.grid },
        border: { color: 'rgba(255,255,255,0.06)' },
      },
      y: {
        ticks: { color: CHART_COLORS.text, font: { size: 10 },
          callback: v => (v > 0 ? '+' : '') + v.toLocaleString() },
        grid: { color: CHART_COLORS.grid },
        border: { color: 'rgba(255,255,255,0.06)' },
      }
    }
  };
}

// 日別収支棒グラフ（収支ページ）
async function renderDailyProfitChart(sessions) {
  const card = document.getElementById('daily-chart-card');
  if (!sessions || sessions.length === 0) { card.style.display = 'none'; return; }

  // 日付ごとに集計
  const byDate = {};
  for (const s of sessions) {
    const d = s.date || '';
    if (!d) continue;
    if (!byDate[d]) byDate[d] = 0;
    byDate[d] += (s.diff_yen || 0);
  }
  const dates = Object.keys(byDate).sort();
  if (dates.length < 2) { card.style.display = 'none'; return; }

  card.style.display = 'block';
  const ctx = document.getElementById('daily-profit-chart').getContext('2d');
  if (_dailyChart) _dailyChart.destroy();

  const values = dates.map(d => byDate[d]);
  _dailyChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: dates.map(d => d.slice(5)), // MM-DD
      datasets: [{
        data: values,
        backgroundColor: values.map(v => v >= 0 ? 'rgba(16,185,129,0.75)' : 'rgba(244,63,94,0.75)'),
        borderColor:     values.map(v => v >= 0 ? '#10b981' : '#f43f5e'),
        borderWidth: 1,
        borderRadius: 4,
        unit: '円',
      }]
    },
    options: {
      ...chartDefaults(),
      plugins: {
        ...chartDefaults().plugins,
        tooltip: {
          callbacks: {
            label: ctx => (ctx.parsed.y >= 0 ? '+' : '') + ctx.parsed.y.toLocaleString() + '円'
          }
        }
      }
    }
  });
}

// 月別収支バーチャート
function renderMonthlyBarChart(sessions) {
  const canvas = document.getElementById('monthly-bar-chart');
  if (!canvas) return;
  const byMonth = {};
  for (const s of sessions) {
    const m = (s.date || '').slice(0, 7);
    if (!m) continue;
    byMonth[m] = (byMonth[m] || 0) + (s.diff_yen || 0);
  }
  const months = Object.keys(byMonth).sort();
  if (months.length < 2) { canvas.style.display = 'none'; return; }
  canvas.style.display = 'block';
  const values = months.map(m => byMonth[m]);
  if (_monthlyBarChart) _monthlyBarChart.destroy();
  _monthlyBarChart = new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: {
      labels: months.map(m => m.slice(5) + '月'),
      datasets: [{
        data: values,
        backgroundColor: values.map(v => v >= 0 ? 'rgba(16,185,129,0.75)' : 'rgba(244,63,94,0.75)'),
        borderColor: values.map(v => v >= 0 ? '#10b981' : '#f43f5e'),
        borderWidth: 1,
        borderRadius: 5,
      }]
    },
    options: {
      ...chartDefaults(),
      plugins: {
        ...chartDefaults().plugins,
        tooltip: { callbacks: { label: ctx => (ctx.parsed.y >= 0 ? '+' : '') + ctx.parsed.y.toLocaleString() + '円' } }
      }
    }
  });
}

// 機種別収支横棒チャート
function renderMachineBreakdownChart(sessions) {
  const card = document.getElementById('machine-breakdown-card');
  if (!card || sessions.length < 3) { if (card) card.style.display = 'none'; return; }
  const byMachine = {};
  for (const s of sessions) {
    const m = s.machine_name || '不明';
    byMachine[m] = (byMachine[m] || 0) + (s.diff_yen || 0);
  }
  const sorted = Object.entries(byMachine).sort(([,a],[,b]) => b - a);
  const topPos = sorted.filter(([,v]) => v >= 0).slice(0, 4);
  const topNeg = sorted.filter(([,v]) => v < 0).slice(-2);
  const display = [...topPos, ...topNeg];
  if (display.length < 2) { card.style.display = 'none'; return; }
  card.style.display = 'block';
  const labels = display.map(([m]) => m);
  const values = display.map(([,v]) => v);
  if (_machineBreakdownChart) _machineBreakdownChart.destroy();
  const defaults = chartDefaults();
  _machineBreakdownChart = new Chart(
    document.getElementById('machine-breakdown-chart').getContext('2d'),
    {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          data: values,
          backgroundColor: values.map(v => v >= 0 ? 'rgba(16,185,129,0.75)' : 'rgba(244,63,94,0.75)'),
          borderColor: values.map(v => v >= 0 ? '#10b981' : '#f43f5e'),
          borderWidth: 1,
          borderRadius: 4,
        }]
      },
      options: {
        ...defaults,
        indexAxis: 'y',
        plugins: {
          ...defaults.plugins,
          tooltip: { callbacks: { label: ctx => (ctx.parsed.x >= 0 ? '+' : '') + ctx.parsed.x.toLocaleString() + '円' } }
        },
        scales: {
          x: { ticks: { color: CHART_COLORS.text, font: { size: 10 }, callback: v => (v >= 0 ? '+' : '') + v.toLocaleString() }, grid: { color: CHART_COLORS.grid } },
          y: { ticks: { color: CHART_COLORS.text, font: { size: 10 } }, grid: { color: CHART_COLORS.grid } }
        }
      }
    }
  );
}

// 今日の狙い台セレクター（店傾向ページ）
async function loadTodayDowMachines(hall) {
  const card  = document.getElementById('today-dow-machines-card');
  const title = document.getElementById('today-dow-machines-title');
  const body  = document.getElementById('today-dow-machines-body');
  if (!card) return;
  try {
    const rows = await apiFetch(
      `/api/hall/weekday_machine_stats?hall_name=${encodeURIComponent(hall)}&days=90`
    );
    if (!rows || rows.length === 0) { card.style.display = 'none'; return; }

    // 今日の曜日を取得
    const dowNames = ['日','月','火','水','木','金','土'];
    const todayDow = dowNames[new Date().getDay()];

    // 今日の曜日で絞り込み
    const todayRows = rows.filter(r => r.weekday === todayDow)
                          .sort((a, b) => b.avg_diff - a.avg_diff)
                          .slice(0, 5);

    if (todayRows.length === 0) { card.style.display = 'none'; return; }

    card.style.display = 'block';
    title.textContent = `${todayDow}曜日に強い機種`;

    const maxAbs = Math.max(...todayRows.map(r => Math.abs(r.avg_diff)), 1);
    const sign = v => v >= 0 ? `+${v}` : `${v}`;

    body.innerHTML = todayRows.map((r, i) => {
      const col = r.avg_diff >= 0 ? 'var(--success)' : 'var(--danger)';
      const pct = Math.round(Math.abs(r.avg_diff) / maxAbs * 100);
      return `<div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border)">
        <span style="font-size:.68rem;color:var(--text3);width:16px;text-align:center;flex-shrink:0">${i+1}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:.88rem;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(r.machine_name)}</div>
          <div style="height:3px;background:var(--bg3);border-radius:2px;margin-top:4px">
            <div style="width:${pct}%;height:100%;background:${col};border-radius:2px"></div>
          </div>
        </div>
        <div style="text-align:right;flex-shrink:0">
          <div style="font-weight:900;color:${col};font-size:.92rem">${sign(r.avg_diff)}枚</div>
          <div style="font-size:.62rem;color:var(--text3)">${r.count}日 勝${r.win_rate}%</div>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    if (card) card.style.display = 'none';
  }
}

async function loadMachineSettingTendency(hall) {
  const card = document.getElementById('machine-tendency-card');
  const body = document.getElementById('machine-tendency-body');
  if (!card) return;
  try {
    const rows = await apiFetch(
      `/api/hall/machine_setting_tendency?hall_name=${encodeURIComponent(hall)}&days=60`
    );
    if (!rows || rows.length === 0) { card.style.display = 'none'; return; }

    // 理論値との比較がある or est_setting が高い機種のみ表示（最大8機種）
    const useful = rows.filter(r => r.est_setting !== null).slice(0, 8);
    if (!useful.length) { card.style.display = 'none'; return; }

    card.style.display = 'block';
    const setColor = s => s >= 5 ? 'var(--success)' : s >= 3.5 ? 'var(--warning)' : s >= 2.5 ? '#f97316' : 'var(--danger)';

    body.innerHTML = useful.map(r => {
      const estS = r.est_setting || 0;
      const highPct = r.high_setting_prob ? Math.round(r.high_setting_prob * 100) : 0;
      const col = setColor(estS);
      // 設定分布バーを小さく表示
      const dist = r.setting_dist || {};
      const distBar = Object.entries(dist).map(([s, p]) => {
        const w = Math.round(p * 100);
        const c = parseInt(s) >= 4 ? 'var(--primary-h)' : parseInt(s) >= 2 ? 'var(--text3)' : 'rgba(255,255,255,0.15)';
        return `<div title="設定${s}: ${Math.round(p*100)}%" style="flex:${w};height:4px;background:${c};border-radius:2px"></div>`;
      }).join('');
      // BB確率の理論値比較
      let bbNote = '';
      if (r.theory_bb_range && r.avg_bb_pct) {
        const [lo, hi] = r.theory_bb_range;
        const obs = r.avg_bb_pct;
        const inRange = obs >= lo * 0.9 && obs <= hi * 1.1;
        bbNote = `<span style="font-size:.65rem;color:${inRange?'var(--text3)':'#f97316'}">BB実測${obs.toFixed(3)}% [${lo.toFixed(3)}〜${hi.toFixed(3)}%]</span>`;
      }
      return `<div style="padding:7px 0;border-bottom:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between;align-items:flex-start">
          <div style="font-size:.85rem;font-weight:700;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.machine_name)}</div>
          <div style="text-align:right;flex-shrink:0;margin-left:8px">
            <span style="font-size:1rem;font-weight:900;color:${col}">設定${estS.toFixed(1)}</span>
            <span style="font-size:.65rem;color:var(--text3);margin-left:4px">高設定${highPct}%</span>
          </div>
        </div>
        <div style="display:flex;gap:2px;margin:4px 0">${distBar}</div>
        <div style="display:flex;gap:8px;align-items:center">${bbNote}<span style="font-size:.65rem;color:var(--text3)">${r.unit_cnt}台 ${r.records}件</span></div>
      </div>`;
    }).join('');
  } catch(e) {
    if (card) card.style.display = 'none';
  }
}

async function loadTodayTargets(hall) {
  const card = document.getElementById('today-targets-card');
  const title = document.getElementById('today-targets-title');
  const body = document.getElementById('today-targets-body');
  if (!card) return;
  try {
    const data = await apiFetch(`/api/hall/today_targets?hall_name=${encodeURIComponent(hall)}`);
    if (!data || (!data.seats.length && !data.best_tail && !data.best_machine)) {
      card.style.display = 'none';
      return;
    }
    card.style.display = 'block';
    title.textContent = `今日(${data.today_weekday}曜日)の狙い台`;
    let html = '';
    if (data.seats.length) {
      html += `<div style="font-size:.68rem;color:var(--text3);margin-bottom:8px;text-transform:uppercase;letter-spacing:.08em">複合スコア順（曜日傾向・安定性・直近トレンド統合）</div>`;
      data.seats.forEach((s, i) => {
        const medals = ['1位', '2位', '3位'];
        const medalCols = ['var(--warning)', 'var(--text2)', '#cd7f32'];
        const col = s.avg_diff >= 0 ? 'var(--success)' : 'var(--danger)';
        const sign = v => v >= 0 ? `+${v}` : `${v}`;
        // 安定性バー (0〜1)
        const stab = (s.stability || 0);
        const stabW = Math.round(stab * 100);
        const stabCol = stab >= 0.7 ? 'var(--success)' : stab >= 0.4 ? 'var(--warning)' : 'var(--danger)';
        // 同曜日avg
        const dowBadge = s.avg_same_dow !== undefined && s.avg_same_dow !== s.avg_diff
          ? `<span style="font-size:.68rem;color:var(--primary-h);background:rgba(124,127,245,.12);padding:1px 6px;border-radius:4px">${data.today_weekday}曜 ${sign(s.avg_same_dow)}枚</span>`
          : '';
        // 直近7日
        const trendBadge = s.avg_7d !== null && s.avg_7d !== undefined
          ? `<span style="font-size:.68rem;color:${s.avg_7d >= s.avg_diff ? 'var(--success)' : 'var(--text3)'};background:var(--bg3);padding:1px 6px;border-radius:4px">直近7日 ${sign(s.avg_7d)}枚</span>`
          : '';
        html += `<div style="padding:10px 0;border-bottom:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px">
            <span style="font-size:.65rem;font-weight:900;color:${medalCols[i]};background:rgba(255,255,255,.04);padding:2px 7px;border-radius:4px;flex-shrink:0">${medals[i]}</span>
            <div style="font-weight:800;font-size:.92rem;flex:1">${esc(s.machine_name)} <span style="color:var(--text3);font-weight:400">${s.seat_number}番</span></div>
            <div style="font-weight:900;color:${col};font-size:1.05rem">${sign(s.avg_diff)}枚</div>
          </div>
          <div style="display:flex;gap:5px;flex-wrap:wrap;margin-bottom:6px">
            ${dowBadge}${trendBadge}
            <span style="font-size:.68rem;color:var(--text3);background:var(--bg3);padding:1px 6px;border-radius:4px">${s.days}日 / 勝率${s.win_rate}%</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            <span style="font-size:.62rem;color:var(--text3);flex-shrink:0">安定性</span>
            <div style="flex:1;height:4px;background:var(--bg3);border-radius:2px">
              <div style="width:${stabW}%;height:100%;background:${stabCol};border-radius:2px;transition:width .6s"></div>
            </div>
            <span style="font-size:.62rem;color:var(--text3)">${stabW}%</span>
          </div>
        </div>`;
      });
    }
    if (data.best_tail || data.best_machine) {
      html += `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">`;
      if (data.best_tail) {
        html += `<div style="background:rgba(124,127,245,.1);border:1px solid rgba(124,127,245,.25);border-radius:8px;padding:7px 13px;font-size:.8rem">
          好調末尾: <strong style="color:var(--primary-h)">${data.best_tail.replace('末尾', '末尾 ')}</strong></div>`;
      }
      if (data.best_machine) {
        html += `<div style="background:rgba(16,185,129,.08);border:1px solid rgba(16,185,129,.25);border-radius:8px;padding:7px 13px;font-size:.8rem">
          好調機種: <strong style="color:var(--success)">${esc(data.best_machine)}</strong></div>`;
      }
      html += `</div>`;
    }
    body.innerHTML = html;
  } catch(e) {
    card.style.display = 'none';
  }
}

// 機種別差枚トレンド折れ線グラフ（店傾向ページ）
async function renderMachineTrendChart(hall, machineName) {
  const card = document.getElementById('machine-trend-card');
  const title = document.getElementById('machine-trend-title');
  title.textContent = `📉 ${machineName} — 差枚推移`;
  card.style.display = 'block';
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  try {
    const rows = await apiFetch(
      `/api/hall/machine_trend?hall_name=${encodeURIComponent(hall)}&machine_name=${encodeURIComponent(machineName)}&days=60`
    );
    if (!rows || rows.length < 2) {
      card.style.display = 'none';
      showToast('データが少なすぎます（2日分以上必要）');
      return;
    }

    const sorted = [...rows].sort((a, b) => a.report_date.localeCompare(b.report_date));
    const labels = sorted.map(r => r.report_date.slice(5));
    const diffs  = sorted.map(r => r.avg_diff_coins ?? null);
    const evs    = sorted.map(r => r.ev_pct ?? null);

    const ctx = document.getElementById('machine-trend-chart').getContext('2d');
    if (_trendChart) _trendChart.destroy();

    _trendChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: '平均差枚',
            data: diffs,
            borderColor: '#818cf8',
            backgroundColor: 'rgba(129,140,248,0.12)',
            borderWidth: 2.5,
            pointRadius: 3,
            pointBackgroundColor: '#818cf8',
            tension: 0.35,
            fill: true,
            yAxisID: 'y',
            unit: '枚',
          },
          {
            label: '出率%',
            data: evs,
            borderColor: '#f59e0b',
            backgroundColor: 'transparent',
            borderWidth: 1.5,
            pointRadius: 2,
            tension: 0.35,
            borderDash: [5, 4],
            yAxisID: 'y2',
            unit: '%',
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: {
          legend: {
            display: true,
            labels: { color: CHART_COLORS.text, font: { size: 11 }, boxWidth: 16 }
          },
          tooltip: {
            callbacks: {
              label: ctx => {
                const v = ctx.parsed.y;
                const u = ctx.dataset.unit || '';
                return `${ctx.dataset.label}: ${v > 0 ? '+' : ''}${v}${u}`;
              }
            }
          }
        },
        scales: {
          x: {
            ticks: { color: CHART_COLORS.text, maxRotation: 45, font: { size: 10 } },
            grid: { color: CHART_COLORS.grid },
          },
          y: {
            position: 'left',
            ticks: { color: CHART_COLORS.text, font: { size: 10 },
              callback: v => (v > 0 ? '+' : '') + v.toLocaleString() },
            grid: { color: CHART_COLORS.grid },
          },
          y2: {
            position: 'right',
            ticks: { color: '#f59e0b', font: { size: 10 },
              callback: v => v + '%' },
            grid: { display: false },
          }
        }
      }
    });
    // 機種クリック後、台番ランキングも表示
    loadMachineSeatRanking(hall, machineName);
  } catch(e) {
    card.style.display = 'none';
    showToast('トレンドデータ取得失敗: ' + e.message, 'error');
  }
}

async function loadMachineSeatRanking(hall, machineName) {
  let card = document.getElementById('machine-seat-ranking-card');
  if (!card) {
    // カードが存在しない場合は動的生成
    card = document.createElement('div');
    card.id = 'machine-seat-ranking-card';
    card.className = 'card';
    const trendCard = document.getElementById('machine-trend-card');
    if (trendCard && trendCard.parentNode)
      trendCard.parentNode.insertBefore(card, trendCard.nextSibling);
  }
  card.style.display = 'block';
  card.innerHTML = `<div class="card-title">${esc(machineName)} — 台番スコアランキング</div><p class="hint">読み込み中...</p>`;

  try {
    const rows = await apiFetch(
      `/api/hall/machine_seat_ranking?hall_name=${encodeURIComponent(hall)}&machine_name=${encodeURIComponent(machineName)}&days=30`
    );
    if (!rows || rows.length === 0) { card.style.display = 'none'; return; }

    const sign = v => v >= 0 ? `+${v}` : `${v}`;
    const items = rows.slice(0, 10).map((r, i) => {
      const col = r.avg_diff >= 0 ? 'var(--success)' : 'var(--danger)';
      const stabW = Math.round((r.stability || 0) * 100);
      const stabCol = r.stability >= 0.7 ? 'var(--success)' : r.stability >= 0.4 ? 'var(--warning)' : 'var(--danger)';
      const dowTxt = r.avg_same_dow !== r.avg_diff
        ? `<span style="font-size:.65rem;color:var(--primary-h);background:rgba(124,127,245,.1);padding:1px 5px;border-radius:3px">今日曜 ${sign(r.avg_same_dow)}</span>` : '';
      return `<div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border)">
        <span style="font-size:.7rem;color:var(--text3);width:18px;text-align:center;flex-shrink:0">${i+1}</span>
        <div style="flex:1">
          <div style="font-size:.88rem;font-weight:700">${r.seat_number}番台 ${dowTxt}</div>
          <div style="display:flex;align-items:center;gap:5px;margin-top:3px">
            <div style="flex:1;height:3px;background:var(--bg3);border-radius:2px">
              <div style="width:${stabW}%;height:100%;background:${stabCol};border-radius:2px"></div>
            </div>
            <span style="font-size:.6rem;color:var(--text3)">安定${stabW}%</span>
          </div>
        </div>
        <div style="text-align:right">
          <div style="font-weight:900;color:${col};font-size:.95rem">${sign(r.avg_diff)}枚</div>
          <div style="font-size:.62rem;color:var(--text3)">${r.days}日 勝${r.win_rate}%</div>
        </div>
      </div>`;
    }).join('');

    card.innerHTML = `<div class="card-title">${esc(machineName)} — 台番スコアランキング</div>${items}`;
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch(e) {
    card.style.display = 'none';
  }
}

init();

// ---------------------------------------------------------------------------
// マップページ
// ---------------------------------------------------------------------------
let _hallMap = null;
let _mapLoaded = false;

async function loadMapPage() {
  const hint = document.getElementById('map-hint');

  // Leaflet マップ初期化（1回だけ）
  if (!_hallMap) {
    _hallMap = L.map('hall-map', { zoomControl: true }).setView([34.76, 135.63], 12);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© OpenStreetMap contributors',
      maxZoom: 18,
    }).addTo(_hallMap);
  }

  hint.textContent = 'データ読み込み中...';

  try {
    const halls = await fetch('/api/map/halls?days=30').then(r => r.json());

    // 既存マーカーをクリア
    _hallMap.eachLayer(layer => {
      if (layer instanceof L.CircleMarker) _hallMap.removeLayer(layer);
    });

    if (!halls.length) {
      hint.textContent = 'データのある店舗がありません。店傾向タブから「取得」してください。';
      return;
    }

    hint.textContent = `${halls.length}店舗表示中 / マーカーをタップで詳細`;

    halls.forEach(h => {
      const radius = 10 + Math.round(h.score * 14); // 強いほど大きく
      const marker = L.circleMarker([h.lat, h.lng], {
        radius,
        color: h.color,
        fillColor: h.color,
        fillOpacity: 0.82,
        weight: 2,
        opacity: 1,
      }).addTo(_hallMap);

      const sign = h.avg_diff >= 0 ? '+' : '';
      marker.bindPopup(`
        <div style="min-width:160px;font-size:13px;line-height:1.7">
          <strong>${h.hall_name}</strong><br>
          <span style="color:${h.color};font-weight:bold">平均差枚 ${sign}${h.avg_diff.toLocaleString()}</span><br>
          <span style="color:#888">勝率 ${h.win_rate}% / ${h.days_cnt}日分データ</span><br>
          <button onclick="switchToHall('${h.hall_name}')"
            style="margin-top:6px;width:100%;padding:5px;background:#6366f1;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px">
            店傾向を見る
          </button>
        </div>
      `);
    });

    // 全マーカーが見えるようにズーム調整（初回のみ）
    if (!_mapLoaded) {
      _mapLoaded = true;
      const coords = halls.map(h => [h.lat, h.lng]);
      if (coords.length === 1) {
        _hallMap.setView(coords[0], 14);
      } else {
        _hallMap.fitBounds(coords, { padding: [30, 30] });
      }
    }
    // マップサイズ再計算（タブ切り替え後に必要）
    setTimeout(() => _hallMap.invalidateSize(), 100);

  } catch(e) {
    hint.textContent = 'マップデータ取得失敗: ' + e.message;
  }
}

// ポップアップから店傾向へ遷移
window.switchToHall = function(hallName) {
  const sel = document.getElementById('hall-select');
  // セレクターに存在するか確認
  let found = false;
  for (const opt of sel.options) {
    if (opt.value === hallName) { sel.value = hallName; found = true; break; }
  }
  if (!found) {
    // カスタム入力
    sel.value = '__custom__';
    const ci = document.getElementById('hall-custom-input');
    ci.style.display = '';
    ci.value = hallName;
  }
  switchTab('hall');
};

// ============================================================
// AI ページ
// ============================================================

let aiChatHistory = [];

async function loadAiPage() {
  // ステータス確認
  try {
    const st = await fetch('/api/ai/status').then(r => r.json());
    const badge = document.getElementById('ai-status-badge');
    if (st.available) {
      badge.textContent = '利用可能';
      badge.style.background = '#276749';
      badge.style.color = '#9ae6b4';
    } else {
      badge.textContent = 'APIキー未設定';
      badge.style.background = '#744210';
      badge.style.color = '#fbd38d';
    }
  } catch {}

  // ホール選択を同期
  const hallSel = document.getElementById('hall-select');
  const aiHallSel = document.getElementById('ai-hall-select');
  if (hallSel && aiHallSel) {
    aiHallSel.innerHTML = hallSel.innerHTML;
    aiHallSel.value = hallSel.value;
  }
}

function getAiHall() {
  const sel = document.getElementById('ai-hall-select');
  return sel ? sel.value : 'ベガスベガス大東店';
}

// B. 自動レポート生成
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('ai-report-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('ai-report-btn');
    const out = document.getElementById('ai-report-output');
    btn.disabled = true;
    btn.textContent = '生成中...';
    out.style.display = 'block';
    out.textContent = '分析中です。しばらくお待ちください...';
    try {
      const data = await fetch(`/api/ai/report?hall_name=${encodeURIComponent(getAiHall())}`).then(r => r.json());
      out.textContent = data.report;
    } catch (e) {
      out.textContent = 'エラーが発生しました: ' + e.message;
    } finally {
      btn.disabled = false;
      btn.textContent = '生成';
    }
  });

  // A. チャット送信
  const sendChat = async () => {
    const input = document.getElementById('ai-chat-input');
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    appendChatMessage('user', msg);

    const thinkingEl = appendChatMessage('ai', '...');
    try {
      const data = await fetch('/api/ai/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg, hall_name: getAiHall(), history: aiChatHistory }),
      }).then(r => r.json());
      thinkingEl.textContent = data.reply;
      aiChatHistory.push({ role: 'user', content: msg });
      aiChatHistory.push({ role: 'assistant', content: data.reply });
      if (aiChatHistory.length > 12) aiChatHistory = aiChatHistory.slice(-12);
    } catch (e) {
      thinkingEl.textContent = 'エラー: ' + e.message;
    }
  };

  document.getElementById('ai-chat-send')?.addEventListener('click', sendChat);
  document.getElementById('ai-chat-input')?.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });

  // クイックボタン
  document.querySelectorAll('.ai-quick-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const input = document.getElementById('ai-chat-input');
      if (input) { input.value = btn.dataset.q; sendChat(); }
    });
  });
});

function appendChatMessage(role, text) {
  const container = document.getElementById('ai-chat-messages');
  const el = document.createElement('div');
  el.className = `chat-bubble ${role === 'user' ? 'user' : 'assistant'}`;
  el.style.whiteSpace = 'pre-wrap';
  el.textContent = text;
  container.appendChild(el);
  container.scrollTop = container.scrollHeight;
  return el;
}
