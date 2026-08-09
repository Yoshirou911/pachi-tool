import {
  JUDGMENT_LABELS,
  assessQuick,
  buildGuideRows,
  buildPerformanceSeries,
  calculateSummary,
  minutesUntilClosing,
  money,
} from './core.mjs';

const APP_VERSION = '1.9.7';
const VERSION_SEEN_KEY = 'pachi-version-seen';
const API_ORIGIN = window.location.hostname === 'yoshirou911.github.io'
  ? 'https://pachi-tool.fly.dev'
  : '';
const apiUrl = path => `${API_ORIGIN}${path}`;
let releaseInfo = {
  version: APP_VERSION,
  released_on: '2026-08-10',
  channel: '公開版',
  patch_notes: [{
    version: APP_VERSION,
    released_on: '2026-08-10',
    title: 'どこからでもホーム・全機能へ移動',
    items: ['左上のPACHI TOOLからホームへ戻る', '右上に常時表示する全機能メニューを追加', 'ハイエナと狙い台の機能をメニュー内で整理'],
  }],
};
const DB_NAME = 'pachi-tool-mobile';
const STORE_NAME = 'app-state';
const STATE_KEY = 'main';
const defaultState = {
  version: 1,
  budget: { starting_bankroll: 0, loss_limit_yen: 0 },
  candidates: [],
  plans: [],
  results: [],
  settings: { closing_time: '22:45' },
};

function clone(value) {
  return globalThis.structuredClone ? globalThis.structuredClone(value) : JSON.parse(JSON.stringify(value));
}

let profiles = [];
let state = clone(defaultState);
let lastAssessment = null;
let dbPromise = null;
let toastTimer = null;
let targetSearchData = null;
let activeModule = 'home';
let targetMapData = null;
let targetHeatMap = null;
let targetHeatLayer = null;
let trendData = null;
let floorData = null;
let floorEditorSeats = [];
let targetHallOptions = [];

function byId(id) { return document.getElementById(id); }
function esc(value) {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function newId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
function safeUrl(raw) {
  try {
    const url = new URL(raw, location.href);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
  } catch (_) { return ''; }
}
function todayValue() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
}
function tomorrowValue() {
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  return `${tomorrow.getFullYear()}-${String(tomorrow.getMonth() + 1).padStart(2, '0')}-${String(tomorrow.getDate()).padStart(2, '0')}`;
}
function showToast(message) {
  const toast = byId('toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 2200);
}

function releaseDateLabel(value) {
  return String(value || '').replace(/-/g, '/');
}

function renderVersionInfo() {
  const seen = localStorage.getItem(VERSION_SEEN_KEY) === releaseInfo.version;
  byId('mobile-version-label').textContent = `v${releaseInfo.version}`;
  byId('mobile-version-button').classList.toggle('seen', seen);
  byId('settings-version-new').classList.toggle('seen', seen);
  byId('settings-version-summary').textContent = `現在 v${releaseInfo.version}・更新日 ${releaseDateLabel(releaseInfo.released_on)}`;
  byId('mobile-release-current').innerHTML = `<strong>PACHI TOOL v${esc(releaseInfo.version)}</strong>${esc(releaseInfo.channel)}・${releaseDateLabel(releaseInfo.released_on)}公開`;
  byId('mobile-patch-notes').innerHTML = (releaseInfo.patch_notes || []).map(note => `
    <article class="patch-note">
      <div class="patch-note-head"><strong>v${esc(note.version)}</strong><time>${releaseDateLabel(note.released_on)}</time></div>
      <p>${esc(note.title)}</p>
      <ul>${(note.items || []).map(item => `<li>${esc(item)}</li>`).join('')}</ul>
    </article>`).join('');
  byId('app-version').textContent = `PACHI TOOL Mobile v${releaseInfo.version}・期待値データ ${profiles.length}条件`;
}

async function loadVersionInfo() {
  try {
    const response = await fetch(apiUrl(`/api/version?ts=${Date.now()}`), { cache: 'no-store' });
    if (response.ok) releaseInfo = await response.json();
  } catch (_) { /* オフライン時は同梱情報を表示 */ }
  renderVersionInfo();
}

function markVersionSeen() {
  localStorage.setItem(VERSION_SEEN_KEY, releaseInfo.version);
  renderVersionInfo();
}

function openDatabase() {
  if (!('indexedDB' in globalThis)) return Promise.reject(new Error('IndexedDB unavailable'));
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE_NAME)) request.result.createObjectStore(STORE_NAME);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  return dbPromise;
}

async function readLocalState() {
  try {
    const db = await openDatabase();
    const saved = await new Promise((resolve, reject) => {
      const request = db.transaction(STORE_NAME, 'readonly').objectStore(STORE_NAME).get(STATE_KEY);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    return saved || clone(defaultState);
  } catch (_) {
    try { return JSON.parse(localStorage.getItem(DB_NAME)) || clone(defaultState); }
    catch (_) { return clone(defaultState); }
  }
}

async function writeLocalState() {
  state.version = 1;
  try {
    const db = await openDatabase();
    await new Promise((resolve, reject) => {
      const request = db.transaction(STORE_NAME, 'readwrite').objectStore(STORE_NAME).put(state, STATE_KEY);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
    });
  } catch (_) {
    localStorage.setItem(DB_NAME, JSON.stringify(state));
  }
}

function normalizeState(saved) {
  return {
    ...clone(defaultState),
    ...saved,
    budget: { ...defaultState.budget, ...(saved?.budget || {}) },
    settings: { ...defaultState.settings, ...(saved?.settings || {}) },
    candidates: Array.isArray(saved?.candidates) ? saved.candidates : [],
    plans: Array.isArray(saved?.plans) ? saved.plans : [],
    results: Array.isArray(saved?.results) ? saved.results : [],
  };
}

async function loadCatalog() {
  const response = await fetch(`./catalog.json?v=${APP_VERSION}`);
  if (!response.ok) throw new Error(`期待値データを取得できません（${response.status}）`);
  const catalog = await response.json();
  profiles = (catalog.profiles || []).map((profile, index) => ({ ...profile, id: index + 1 }));
}

function currentProfile() {
  return profiles.find(profile => profile.id === Number(byId('quick-profile').value));
}

function syncConditions(profile) {
  if (!profile) return;
  if (['equivalent', '56', 'other'].includes(profile.exchange_type)) byId('quick-exchange').value = profile.exchange_type;
  if (['cash', 'medals'].includes(profile.funding_mode)) byId('quick-funding').value = profile.funding_mode;
  if (['normal', 'reset_confirmed'].includes(profile.reset_status)) byId('quick-reset').value = profile.reset_status;
  byId('quick-current-label').textContent = `${profile.metric_name}（${profile.unit_label}）`;
  byId('quick-current-unit').textContent = profile.unit_label || 'G';
}

function populateMachines(preferredProfileId = null) {
  const machineSelect = byId('quick-machine');
  const names = [...new Set(profiles.map(profile => profile.machine_name))].sort((a, b) => a.localeCompare(b, 'ja'));
  const preferred = profiles.find(profile => profile.id === Number(preferredProfileId));
  const previous = preferred?.machine_name || machineSelect.value || names[0] || '';
  machineSelect.innerHTML = names.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
  if (names.includes(previous)) machineSelect.value = previous;
  populateProfileOptions(preferredProfileId);
}

function populateProfileOptions(preferredProfileId = null) {
  const machine = byId('quick-machine').value;
  const matches = profiles.filter(profile => profile.machine_name === machine);
  const select = byId('quick-profile');
  select.innerHTML = matches.map(profile => `<option value="${profile.id}">${esc(profile.condition_label)}</option>`).join('');
  if (matches.some(profile => profile.id === Number(preferredProfileId))) select.value = String(preferredProfileId);
  syncConditions(currentProfile());
  byId('quick-result').innerHTML = '';
  lastAssessment = null;
}

function renderSummary() {
  const summary = calculateSummary(state);
  byId('summary-bankroll').textContent = summary.configured ? money(summary.current_bankroll) : '--';
  byId('summary-loss').textContent = summary.configured ? money(summary.remaining_loss_yen) : '--';
  byId('summary-net').textContent = money(summary.net_profit_yen, true);
  byId('summary-net').className = summary.net_profit_yen >= 0 ? 'money-up' : 'money-down';
  byId('summary-plays').textContent = `${summary.plays}回`;
  const alert = byId('setup-alert');
  alert.innerHTML = summary.configured ? '' : '<div><strong>最初に資金を設定</strong><span>安全に判定するため、運用資金と損失上限を入力してください。</span></div><button type="button" data-screen-target="settings">設定する</button>';
  alert.classList.toggle('hidden', summary.configured);
  return summary;
}

function renderGuide() {
  const summary = calculateSummary(state);
  const mode = byId('guide-mode').value;
  const search = byId('guide-search').value;
  const rows = buildGuideRows(profiles, summary, mode, search);
  byId('guide-count').textContent = `${rows.length}件`;
  const list = byId('guide-list');
  if (!rows.length) {
    list.innerHTML = '<p class="empty">一致する期待値データがありません</p>';
    return;
  }
  list.innerHTML = rows.map(({ profile, point, assessment }) => {
    const source = safeUrl(profile.source_url);
    const worst = point.worst_case_yen ?? profile.worst_case_investment_yen;
    const needsSetup = !summary.configured && assessment.judgment === 'insufficient_funds';
    const judgment = needsSetup ? 'setup' : assessment.judgment;
    const judgmentLabel = needsSetup ? '資金未設定' : (JUDGMENT_LABELS[assessment.judgment] || assessment.judgment);
    return `<article class="guide-row">
      <div class="guide-row-top">
        <div class="guide-main"><strong>${esc(profile.machine_name)}</strong><small>${esc(profile.condition_label)}</small></div>
        <span class="signal signal-${esc(judgment)}">${esc(judgmentLabel)}</span>
      </div>
      <div class="guide-data">
        <div><small>狙い始め</small><strong>${Number(point.value).toLocaleString('ja-JP')}${esc(profile.unit_label)}〜</strong></div>
        <div class="ev-metric"><small>期待値</small><strong class="${Number(point.ev_yen) >= 0 ? 'money-up' : 'money-down'}">${money(point.ev_yen, true)}</strong></div>
        <div><small>必要資金</small><strong>${money(worst)}</strong></div>
      </div>
      <div class="guide-actions">
        <button type="button" data-check-profile="${profile.id}" data-check-value="${Number(point.value)}">この条件で判定</button>
        ${source ? `<a class="source-link" href="${esc(source)}" target="_blank" rel="noopener">出典</a>` : ''}
      </div>
    </article>`;
  }).join('');
}

function renderCatalogScope() {
  const machines = [...new Set(profiles.map(profile => profile.machine_name))];
  byId('catalog-scope').innerHTML = `<strong>現在はスマスロ${machines.length}機種・${profiles.length}条件に対応</strong><span>${machines.map(esc).join('・')}が対象です。表示データは例ではなく、複数情報を照合して登録した目安です。設定狙い・パチンコには未対応です。</span>`;
}

function renderQuickResult(result, profile, currentValue) {
  const label = JUDGMENT_LABELS[result.judgment] || result.judgment;
  const warnings = (result.warnings || []).map(item => `<li>${esc(item)}</li>`).join('');
  byId('quick-result').innerHTML = `
    <div class="decision-card ${esc(result.judgment)}">
      <div class="decision-head"><div><span class="page-step">判定結果</span><h2>${esc(label)}</h2></div><span class="signal signal-${esc(result.judgment)}">${result.actionable ? '打てる' : '停止'}</span></div>
      <p class="decision-reason">${esc(result.reason)}</p>
      <div class="decision-highlight">
        <div><small>期待値</small><strong class="${Number(result.expected_value_yen) >= 0 ? 'money-up' : ''}">${money(result.expected_value_yen, true)}</strong></div>
        <div><small>必要資金</small><strong>${money(result.worst_case_investment_yen)}</strong></div>
      </div>
      <div class="result-metrics">
        <div><small>現在</small><strong>${Number(currentValue).toLocaleString('ja-JP')}${esc(profile.unit_label)}</strong></div>
        <div><small>狙い始め</small><strong>${Number(profile.start_threshold).toLocaleString('ja-JP')}${esc(profile.unit_label)}〜</strong></div>
        <div><small>閉店まで</small><strong>${result.minutes_until_close}分</strong></div>
        <div><small>使える資金</small><strong>${money(calculateSummary(state).risk_capacity_yen)}</strong></div>
      </div>
      <div class="input-rule"><b>この判定で見る数字</b><strong>${esc(profile.metric_name)}（${esc(profile.unit_label)}）</strong>${profile.notes ? `<span>${esc(profile.notes)}</span>` : ''}</div>
      ${warnings ? `<ul class="warning-list">${warnings}</ul>` : ''}
      <div class="stop-rule"><b>やめどき</b>${esc(profile.stop_rule || '未登録')}</div>
      <div class="decision-actions">
        ${result.actionable ? '<button id="save-candidate-button" class="primary-button" type="button">この台を候補に保存</button>' : ''}
        <button class="secondary-button" type="button" data-screen-target="guide">ほかの狙い目を見る</button>
      </div>
    </div>`;
}

function renderCandidates() {
  const list = byId('candidate-list');
  byId('candidate-count').textContent = `${state.candidates.length}台`;
  if (!state.candidates.length) {
    list.innerHTML = '<p class="empty">保存した台はまだありません。<br>判定結果から「この台を候補に保存」を押すと、ここに残せます。</p>';
    return;
  }
  list.innerHTML = state.candidates.map(candidate => `<article class="list-card">
    <div class="list-card-head"><span class="signal signal-target">候補</span><div class="list-card-main"><strong>${esc(candidate.machine_name)}</strong><small>${esc(candidate.condition_label)}</small></div></div>
    <p class="card-meta">現在 ${Number(candidate.current_value).toLocaleString('ja-JP')}${esc(candidate.unit_label)}・期待値 ${money(candidate.expected_value_yen, true)}・必要資金 ${money(candidate.worst_case_investment_yen)}</p>
    <div class="card-actions"><button class="play" type="button" data-result-candidate="${esc(candidate.id)}">実戦結果を入力</button><button class="skip" type="button" data-skip-candidate="${esc(candidate.id)}">見送る</button></div>
  </article>`).join('');
}

function renderPlans() {
  const list = byId('planner-list');
  const priorityLabels = { 1: '最優先', 2: '候補', 3: '抑え' };
  const strategyLabels = { reset: 'リセット狙い', carryover: '据え置き狙い', setting: '設定狙い', morning: '朝一挙動を見る' };
  const plans = [...state.plans].sort((a, b) => Number(a.priority || 2) - Number(b.priority || 2) || String(a.created_at).localeCompare(String(b.created_at)));
  byId('planner-count').textContent = `${plans.length}台`;
  if (!plans.length) {
    list.innerHTML = '<p class="empty">明日の狙い台はまだありません。<br>店舗と台番号を登録すると、優先順に並びます。</p>';
    return;
  }
  list.innerHTML = plans.map(plan => {
    const checked = plan.status === 'checked';
    return `<article class="list-card plan-card ${checked ? 'plan-checked' : ''}">
      <div class="list-card-head">
        <span class="signal ${checked ? 'signal-target' : `priority-${esc(plan.priority || 2)}`}">${checked ? '確認済み' : esc(priorityLabels[plan.priority] || '候補')}</span>
        <div class="list-card-main"><strong>${esc(plan.machine_name)}${plan.machine_number ? ` <em>台${esc(plan.machine_number)}</em>` : ''}</strong><small>${esc(plan.visit_date)}・${esc(plan.store_name)}</small></div>
      </div>
      <div class="plan-facts">
        <span><small>狙い方</small><b>${esc(strategyLabels[plan.strategy] || plan.strategy)}</b></span>
        <span><small>前日最終</small><b>${plan.previous_games === null || plan.previous_games === '' ? '未入力' : `${Number(plan.previous_games).toLocaleString('ja-JP')}G`}</b></span>
      </div>
      ${plan.notes ? `<p class="card-meta plan-notes">${esc(plan.notes)}</p>` : ''}
      <div class="card-actions"><button class="play" type="button" data-plan-toggle="${esc(plan.id)}">${checked ? '未確認に戻す' : '朝一確認済みにする'}</button><button class="skip" type="button" data-plan-remove="${esc(plan.id)}">削除</button></div>
    </article>`;
  }).join('');
}

function signedCoins(value) {
  const number = Number(value || 0);
  return `${number >= 0 ? '+' : ''}${number.toLocaleString('ja-JP')}枚`;
}

function renderTargetSearch() {
  const container = byId('target-search-results');
  if (!targetSearchData) {
    container.innerHTML = '';
    return;
  }
  const halls = targetSearchData.halls || [];
  const insufficient = targetSearchData.insufficient_halls || [];
  if (!halls.length) {
    container.innerHTML = `<div class="target-empty"><strong>おすすめを出せるデータがありません</strong><span>${esc(targetSearchData.notice || '取得日数が増えるまでお待ちください。')}</span></div>`;
    return;
  }
  container.innerHTML = `
    <div class="target-result-heading"><div><span class="page-step">${esc(targetSearchData.visit_date)} ${esc(targetSearchData.weekday)}曜日</span><h2>店舗・狙い機種ランキング</h2></div><span class="count-badge">${halls.length}店</span></div>
    ${halls.map((hall, hallIndex) => `<article class="target-hall-card">
      <div class="target-hall-head">
        <span class="target-rank">${hall.rank}</span>
        <div><strong>${esc(hall.hall_name)}</strong><small>${esc(hall.basis)}・最終 ${esc(hall.latest_date)}</small></div>
        <div class="target-score"><b>${hall.score}</b><small>点</small></div>
      </div>
      <div class="target-metrics"><span><small>店舗平均</small><b class="${hall.avg_diff >= 0 ? 'money-up' : 'money-down'}">${signedCoins(hall.avg_diff)}</b></span><span><small>プラス日率</small><b>${hall.positive_rate}%</b></span><span><small>実績</small><b>${hall.sample_days}日</b></span><span><small>信頼度</small><b class="confidence-${hall.confidence === '高' ? 'high' : hall.confidence === '中' ? 'mid' : 'low'}">${esc(hall.confidence)}</b></span></div>
      <div class="target-reasons">${(hall.reasons || []).map(reason => `<span>${esc(reason)}</span>`).join('')}</div>
      <div class="target-machine-list">
        ${(hall.target_machines || []).slice(0, 3).map((machine, machineIndex) => `<div class="target-machine-row">
          <div><strong>${esc(machine.machine_name)}</strong><small>${machine.sample_days}日・プラス率${machine.positive_rate}%・平均${signedCoins(machine.avg_diff)}</small></div>
          <span>${machine.score}点</span>
          <button type="button" data-target-hall-index="${hallIndex}" data-target-machine-index="${machineIndex}">朝一候補に保存</button>
        </div>`).join('') || '<p class="fine-print">機種別候補はまだ材料不足です。</p>'}
      </div>
    </article>`).join('')}
    ${insufficient.length ? `<details class="insufficient-halls"><summary>データ不足の店舗 ${insufficient.length}店</summary><div>${insufficient.map(item => `<span>${esc(item.hall_name)}：${esc(item.reason)}</span>`).join('')}</div></details>` : ''}
    <p class="fine-print">${esc(targetSearchData.notice)}</p>`;
}

function compactCoins(value) {
  const number = Number(value || 0);
  const abs = Math.abs(number);
  return `${number >= 0 ? '+' : '-'}${abs >= 10000 ? `${(abs / 10000).toFixed(1)}万` : Math.round(abs).toLocaleString('ja-JP')}枚`;
}

function renderMiniTrend(rows, labelKey) {
  if (!rows?.length) return '<p class="fine-print">長期データはまだありません。</p>';
  const maxAbs = Math.max(1, ...rows.map(row => Math.abs(Number(row.avg_diff || 0))));
  return `<div class="mini-trend">${rows.map(row => {
    const value = Number(row.avg_diff || 0);
    const width = Math.max(3, Math.round(Math.abs(value) / maxAbs * 100));
    return `<div><span>${esc(row[labelKey])}</span><i><b class="${value >= 0 ? 'trend-up' : 'trend-down'}" style="width:${width}%"></b></i><em class="${value >= 0 ? 'money-up' : 'money-down'}">${compactCoins(value)}</em></div>`;
  }).join('')}</div>`;
}

function renderTargetMapDetail(hall) {
  const detail = byId('target-map-detail');
  if (!hall) {
    detail.innerHTML = '<p class="empty">店舗を選ぶと長期傾向を表示します。</p>';
    return;
  }
  detail.innerHTML = `<article class="panel heat-detail-panel">
    <div class="heat-detail-head"><div><span class="page-step">${esc(targetMapData.visit_date)}の分析</span><h2>${esc(hall.hall_name)}</h2></div><div class="heat-score" style="--heat-color:${esc(hall.color)}"><b>${hall.score}</b><small>点</small><span>${esc(hall.heat_level)}</span></div></div>
    <div class="target-metrics"><span><small>指定日推定</small><b class="${hall.projected_diff >= 0 ? 'money-up' : 'money-down'}">${hall.projected_diff == null ? '--' : signedCoins(hall.projected_diff)}</b></span><span><small>プラス日率</small><b>${hall.positive_rate == null ? '--' : `${hall.positive_rate}%`}</b></span><span><small>信頼度</small><b>${esc(hall.confidence)}</b></span><span><small>長期実績</small><b>${hall.long_term?.sample_days || 0}日</b></span></div>
    <div class="target-reasons">${(hall.reasons || []).map(reason => `<span>${esc(reason)}</span>`).join('')}</div>
    <div class="long-trend-block"><h3>月ごとの長期推移</h3>${renderMiniTrend(hall.monthly_trend, 'month')}</div>
    <div class="long-trend-block"><h3>曜日ごとの長期傾向</h3>${renderMiniTrend(hall.weekday_profile, 'weekday')}</div>
    <div class="map-machine-list"><h3>この日の狙い機種</h3>${(hall.target_machines || []).slice(0, 3).map(machine => `<div><strong>${esc(machine.machine_name)}</strong><span class="${machine.avg_diff >= 0 ? 'money-up' : 'money-down'}">${signedCoins(machine.avg_diff)}</span><small>${machine.sample_days}日・${machine.score}点</small></div>`).join('') || '<p class="fine-print">機種候補は材料不足です。</p>'}</div>
  </article>`;
}

function renderTargetHeatMap() {
  const mapElement = byId('target-heat-map');
  const halls = targetMapData?.halls || [];
  if (!halls.length) {
    if (targetHeatMap) {
      targetHeatMap.remove();
      targetHeatMap = null;
      targetHeatLayer = null;
    }
    mapElement.innerHTML = '<div class="map-fallback">表示できる店舗データがありません。</div>';
    renderTargetMapDetail(null);
    return;
  }
  if (!globalThis.L) {
    mapElement.innerHTML = `<div class="map-fallback">${halls.map(hall => `<button type="button" data-map-hall="${esc(hall.hall_name)}"><b style="background:${esc(hall.color)}"></b><span>${esc(hall.hall_name)}</span><strong>${hall.score}点</strong></button>`).join('')}</div>`;
    mapElement.querySelectorAll('[data-map-hall]').forEach(button => button.addEventListener('click', () => renderTargetMapDetail(halls.find(hall => hall.hall_name === button.dataset.mapHall))));
    renderTargetMapDetail(halls[0]);
    return;
  }
  if (!targetHeatMap) {
    mapElement.innerHTML = '';
    targetHeatMap = L.map(mapElement, { zoomControl: true }).setView([targetMapData.center.lat, targetMapData.center.lng], 13);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap', maxZoom: 18 }).addTo(targetHeatMap);
  }
  if (targetHeatLayer) targetHeatLayer.remove();
  targetHeatLayer = L.layerGroup().addTo(targetHeatMap);
  const bounds = [];
  halls.forEach(hall => {
    const radius = hall.score ? 12 + hall.score * .22 : 9;
    const marker = L.circleMarker([hall.lat, hall.lng], { radius, color: hall.color, fillColor: hall.color, fillOpacity: .72, weight: 3 }).addTo(targetHeatLayer);
    marker.bindTooltip(esc(`${hall.rank ? `${hall.rank}位 ` : ''}${hall.hall_name} ${hall.score}点`), { direction: 'top' });
    marker.on('click', () => renderTargetMapDetail(hall));
    bounds.push([hall.lat, hall.lng]);
  });
  if (bounds.length > 1) targetHeatMap.fitBounds(bounds, { padding: [24, 24], maxZoom: 14 });
  else targetHeatMap.setView(bounds[0], 14);
  setTimeout(() => targetHeatMap.invalidateSize(), 50);
  renderTargetMapDetail(halls[0]);
}

async function loadTargetHeatMap() {
  const visitDate = byId('target-map-date').value || tomorrowValue();
  byId('target-visit-date').value = visitDate;
  const longDays = byId('target-map-long-days').value;
  const status = byId('target-map-status');
  byId('target-map-button').disabled = true;
  status.textContent = '指定日の店舗熱量と長期傾向を計算中...';
  try {
    const response = await fetch(apiUrl(`/api/map/target_heat?visit_date=${encodeURIComponent(visitDate)}&days=120&long_days=${encodeURIComponent(longDays)}`));
    if (!response.ok) throw new Error(`マップAPI ${response.status}`);
    targetMapData = await response.json();
    renderTargetHeatMap();
    status.textContent = `${targetMapData.visit_date}（${targetMapData.weekday}）・${targetMapData.halls.length}店舗を表示中`;
  } catch (error) {
    status.textContent = `マップを取得できません：${error.message}`;
  } finally {
    byId('target-map-button').disabled = false;
  }
}

async function loadTargetHallOptions() {
  if (targetHallOptions.length) return targetHallOptions;
  try {
    const response = await fetch(apiUrl('/api/scrape/halls'));
    if (!response.ok) throw new Error(`店舗一覧 ${response.status}`);
    const rows = await response.json();
    const localPriority = new Map([['キコーナ四條畷店', 0], ['ひま・わり四條畷店', 1], ['キコーナ野崎店', 2]]);
    targetHallOptions = rows.filter(row => row.enabled).sort((a, b) => {
      const aPriority = localPriority.has(a.hall_name) ? localPriority.get(a.hall_name) : 99;
      const bPriority = localPriority.has(b.hall_name) ? localPriority.get(b.hall_name) : 99;
      return aPriority - bPriority || (b.db_record_count || 0) - (a.db_record_count || 0);
    });
  } catch (_) {
    targetHallOptions = [
      { hall_name: 'ニコニコ住道店' }, { hall_name: 'ベガスベガス大東店' },
      { hall_name: 'マルハン大東店' }, { hall_name: 'スーパーコスモプレミアム大東店' },
      { hall_name: 'キコーナ四條畷店' }, { hall_name: 'ひま・わり四條畷店' },
    ];
  }
  const options = targetHallOptions.map(row => `<option value="${esc(row.hall_name)}">${esc(row.hall_name)}</option>`).join('');
  ['trend-hall', 'floor-hall'].forEach(id => {
    const select = byId(id);
    const current = select.value;
    select.innerHTML = options;
    if ([...select.options].some(option => option.value === current)) select.value = current;
  });
  return targetHallOptions;
}

function renderProfileBars(rows, labelKey, valueKey = 'avg_diff') {
  if (!rows?.length) return '<p class="empty">分析できるデータがありません。</p>';
  const maxAbs = Math.max(1, ...rows.map(row => Math.abs(Number(row[valueKey] || 0))));
  return `<div class="profile-bars">${rows.map(row => {
    const value = Number(row[valueKey] || 0);
    const width = Math.max(4, Math.round(Math.abs(value) / maxAbs * 100));
    return `<div><span>${esc(row[labelKey])}</span><i><b class="${value >= 0 ? 'trend-up' : 'trend-down'}" style="width:${width}%"></b></i><strong class="${value >= 0 ? 'money-up' : 'money-down'}">${signedCoins(value)}</strong><small>${row.sample_days || 0}日</small></div>`;
  }).join('')}</div>`;
}

function renderTrendCalendar(rows) {
  if (!rows?.length) return '<p class="empty">カレンダー実績がありません。</p>';
  return `<div class="trend-calendar">${rows.slice(-42).map(row => {
    const score = Number(row.score || 0);
    const color = score >= 70 ? '#f43f5e' : score >= 58 ? '#f97316' : score >= 48 ? '#eab308' : '#38bdf8';
    return `<button type="button" style="--day-color:${color}" title="平均${signedCoins(row.avg_diff)}"><span>${esc(row.date.slice(5).replace('-', '/'))}</span><b>${score}</b><small class="${row.avg_diff >= 0 ? 'money-up' : 'money-down'}">${compactCoins(row.avg_diff)}</small></button>`;
  }).join('')}</div>`;
}

function renderTrendProfile(aiResult = null) {
  const container = byId('trend-results');
  if (!trendData || trendData.status !== '分析済み') {
    container.innerHTML = `<p class="empty">${esc(trendData?.notice || 'この店舗の公開実績をまだ取得できていません。')}</p>`;
    return;
  }
  const topMachines = trendData.machine_profile || [];
  const aiSummary = aiResult?.summary || '';
  const engineLabel = aiResult?.engine || '統計エンジン';
  container.innerHTML = `
    <article class="panel trend-summary-card">
      <div class="trend-summary-head"><div><span class="page-step">店舗カルテ</span><h2>${esc(trendData.hall_name)}</h2><small>${trendData.first_date}〜${trendData.latest_date}</small></div><div class="confidence-seal confidence-${trendData.confidence === '高' ? 'high' : trendData.confidence === '中' ? 'mid' : 'low'}"><b>${trendData.confidence}</b><span>信頼度</span></div></div>
      <div class="target-metrics"><span><small>収集日数</small><b>${trendData.sample_days}日</b></span><span><small>全体平均</small><b class="${trendData.overall.avg_diff >= 0 ? 'money-up' : 'money-down'}">${signedCoins(trendData.overall.avg_diff)}</b></span><span><small>プラス日率</small><b>${trendData.overall.positive_day_rate}%</b></span><span><small>基準日</small><b>${esc(trendData.reference_date.slice(5))}</b></span></div>
      <div class="target-reasons">${trendData.insights.map(item => `<span>${esc(item)}</span>`).join('')}</div>
    </article>
    <article class="panel ai-insight-card"><div class="section-bar"><div><span class="page-step">AI / 統計</span><h2>この店のクセ</h2></div><span class="count-badge">${esc(engineLabel)}</span></div><p>${esc(aiSummary || trendData.insights.join('\n')).replace(/\n/g, '<br>')}</p></article>
    <article class="panel"><div class="section-bar"><h2>直近42日のカレンダー</h2><span class="count-badge">色が強さ</span></div>${renderTrendCalendar(trendData.calendar)}</article>
    <article class="panel"><div class="section-bar"><h2>曜日ごとのクセ</h2></div>${renderProfileBars(trendData.weekday_profile, 'weekday')}</article>
    <article class="panel"><div class="section-bar"><h2>日付末尾のクセ</h2></div>${renderProfileBars(trendData.digit_profile, 'digit')}</article>
    <article class="panel"><div class="section-bar"><h2>次の注目日</h2></div><div class="next-hot-dates">${(trendData.next_dates || []).map(item => `<div class="${item.score >= 60 ? 'hot' : ''}"><strong>${esc(item.date.slice(5).replace('-', '/'))}</strong><span>${esc(item.weekday)}曜</span><b>${item.score}点</b><small>${esc(item.evidence)}</small></div>`).join('')}</div></article>
    <article class="panel"><div class="section-bar"><h2>扱いが強い機種</h2><span class="count-badge">上位10</span></div><div class="trend-machine-list">${topMachines.slice(0, 10).map((machine, index) => `<div><span class="target-rank">${index + 1}</span><div><strong>${esc(machine.machine_name)}</strong><small>${machine.sample_days}日・プラス率${machine.positive_rate}%${machine.trend == null ? '' : `・直近差${signedCoins(machine.trend)}`}</small></div><b class="${machine.avg_diff >= 0 ? 'money-up' : 'money-down'}">${signedCoins(machine.avg_diff)}</b><button type="button" data-trend-machine="${index}">朝一候補に保存</button></div>`).join('')}</div></article>
    <details class="insufficient-halls"><summary>出典と注意事項</summary><div>${(trendData.source_urls || []).map(url => `<a href="${esc(safeUrl(url))}" target="_blank" rel="noopener">公開データ</a>`).join('') || '<span>取得元URLはデータ内にありません。</span>'}<span>${esc(trendData.notice)}</span></div></details>`;
}

async function loadTrendProfile() {
  await loadTargetHallOptions();
  const hall = byId('trend-hall').value;
  const visitDate = byId('trend-date').value || tomorrowValue();
  const days = byId('trend-days').value;
  const status = byId('trend-status');
  byId('trend-button').disabled = true;
  status.textContent = '曜日・日付・機種・次の注目日を分析中...';
  try {
    const query = `hall_name=${encodeURIComponent(hall)}&visit_date=${encodeURIComponent(visitDate)}&days=${encodeURIComponent(days)}`;
    const [profileResponse, aiResponse] = await Promise.all([
      fetch(apiUrl(`/api/hall/trend_profile?${query}`)),
      fetch(apiUrl(`/api/ai/hall_profile?${query}`)).catch(() => null),
    ]);
    if (!profileResponse.ok) throw new Error(`傾向API ${profileResponse.status}`);
    trendData = await profileResponse.json();
    const aiData = aiResponse?.ok ? await aiResponse.json() : null;
    renderTrendProfile(aiData);
    status.textContent = `${trendData.sample_days || 0}日分・信頼度${trendData.confidence || '不足'}で分析`;
  } catch (error) {
    status.textContent = `傾向分析を取得できません：${error.message}`;
  } finally {
    byId('trend-button').disabled = false;
  }
}

function renderFloorSeatDetail(seat) {
  const detail = byId('floor-seat-detail');
  if (!seat) {
    detail.innerHTML = '<p class="empty">座席を押すと根拠と実績を表示します。</p>';
    return;
  }
  detail.innerHTML = `<article class="panel seat-detail-card"><div class="seat-detail-head"><div><span class="page-step">台番号 ${seat.seat_number}</span><h2>${esc(seat.machine_name || '機種未登録')}</h2><small>${esc(seat.island_name || '')}</small></div><div class="heat-score" style="--heat-color:${esc(seat.color)}"><b>${seat.score ?? '--'}</b><small>点</small><span>${esc(seat.heat_level)}</span></div></div><div class="target-metrics"><span><small>指定日推定</small><b class="${seat.estimate >= 0 ? 'money-up' : 'money-down'}">${seat.estimate == null ? '--' : signedCoins(seat.estimate)}</b></span><span><small>プラス率</small><b>${seat.positive_rate == null ? '--' : `${seat.positive_rate}%`}</b></span><span><small>実績</small><b>${seat.sample_days}日</b></span><span><small>当日結果</small><b>${seat.actual ? signedCoins(seat.actual.diff) : '--'}</b></span></div><div class="target-reasons">${seat.reasons.map(reason => `<span>${esc(reason)}</span>`).join('')}</div><button type="button" class="primary-button plan-button" data-floor-plan-seat="${seat.seat_number}">この座席を作戦に保存</button></article>`;
}

function renderFloorMap() {
  const canvas = byId('floor-map-canvas');
  const meta = byId('floor-map-meta');
  const seats = floorData?.seats || [];
  const layout = floorData?.layout;
  if (!layout || !seats.length) {
    canvas.innerHTML = '<div class="floor-empty"><strong>座席マップはまだありません</strong><span>下の「店内マップを登録・修正」から、出典URLと台番号範囲を登録できます。</span></div>';
    canvas.style.aspectRatio = '5 / 3';
    meta.innerHTML = `<span>${esc(floorData?.status || '未登録')}</span><small>${esc(floorData?.notice || '')}</small>`;
    renderFloorSeatDetail(null);
    return;
  }
  canvas.style.aspectRatio = `${layout.width} / ${layout.height}`;
  canvas.innerHTML = seats.map(seat => `<button class="floor-seat" type="button" data-floor-seat="${seat.seat_number}" style="left:${seat.x / layout.width * 100}%;top:${seat.y / layout.height * 100}%;width:${seat.width / layout.width * 100}%;height:${seat.height / layout.height * 100}%;--seat-color:${esc(seat.color)}" title="${esc(`${seat.seat_number}番 ${seat.machine_name}`)}"><b>${seat.seat_number}</b><span>${seat.score ?? '--'}</span></button>`).join('');
  meta.innerHTML = `<span>${esc(layout.floor_name)}・${esc(layout.verification_status)}</span><small>${esc(layout.source_label || '利用者登録マップ')}・台番号実績 ${floorData.data_coverage.seat_count}台</small>${layout.source_url ? `<a href="${esc(safeUrl(layout.source_url))}" target="_blank" rel="noopener">出典を開く</a>` : ''}`;
  canvas.querySelectorAll('[data-floor-seat]').forEach(button => button.addEventListener('click', () => renderFloorSeatDetail(seats.find(seat => seat.seat_number === Number(button.dataset.floorSeat)))));
  renderFloorSeatDetail(seats[0]);
}

async function loadFloorHeat() {
  await loadTargetHallOptions();
  const hall = byId('floor-hall').value;
  const visitDate = byId('floor-date').value || tomorrowValue();
  const days = byId('floor-days').value;
  const status = byId('floor-status');
  byId('floor-button').disabled = true;
  status.textContent = '台番号・曜日・並び・機種傾向を分析中...';
  try {
    const response = await fetch(apiUrl(`/api/layouts/seat_heat?hall_name=${encodeURIComponent(hall)}&visit_date=${encodeURIComponent(visitDate)}&days=${encodeURIComponent(days)}`));
    if (!response.ok) throw new Error(`座席API ${response.status}`);
    floorData = await response.json();
    floorEditorSeats = floorData.seats.map(seat => ({ seat_number: seat.seat_number, machine_name: seat.machine_name || '', island_name: seat.island_name || '', x: seat.x, y: seat.y, width: seat.width, height: seat.height, rotation: seat.rotation || 0 }));
    byId('floor-valid-from').value = floorData.layout.valid_from || todayValue();
    byId('floor-source-label').value = floorData.layout.source_label || '';
    byId('floor-source-url').value = floorData.layout.source_url || '';
    byId('floor-source-kind').value = floorData.layout.source_kind || 'manual';
    byId('floor-verification').value = floorData.layout.verification_status || '未確認';
    byId('floor-notes').value = floorData.layout.notes || '';
    renderFloorMap();
    status.textContent = `${floorData.status}・${floorData.data_coverage.history_rows}件の台番号実績を使用`;
    if (!floorData.seats.length) document.querySelector('.floor-editor').open = true;
  } catch (error) {
    status.textContent = `座席マップを取得できません：${error.message}`;
  } finally {
    byId('floor-button').disabled = false;
  }
}

function createAutoFloorSeats() {
  const start = Number(byId('floor-seat-start').value);
  const end = Number(byId('floor-seat-end').value);
  if (!start || !end || end < start || end - start > 299) {
    showToast('開始・終了台番号を正しく入力してください（最大300台）');
    return;
  }
  const island = byId('floor-island-name').value.trim() || 'スロット島';
  const numbers = Array.from({ length: end - start + 1 }, (_, index) => start + index);
  floorEditorSeats = numbers.map((seatNumber, index) => ({
    seat_number: seatNumber, machine_name: island, island_name: island,
    x: 55 + (index % 14) * 64, y: 70 + Math.floor(index / 14) * 100,
    width: 52, height: 44, rotation: 0,
  }));
  const height = Math.max(420, 170 + Math.ceil(numbers.length / 14) * 100);
  floorData = { hall_name: byId('floor-hall').value, status: '仮配置', notice: '公式マップまたは現地で位置を確認してください。', data_coverage: { history_rows: 0, seat_count: 0 }, layout: { id: null, hall_name: byId('floor-hall').value, floor_name: 'スロットフロア', valid_from: byId('floor-valid-from').value || todayValue(), width: 1000, height, source_url: byId('floor-source-url').value, source_label: byId('floor-source-label').value || '利用者登録マップ', source_kind: byId('floor-source-kind').value, verification_status: byId('floor-verification').value, notes: byId('floor-notes').value, generated: false }, seats: floorEditorSeats.map(seat => ({ ...seat, score: null, color: '#64748b', heat_level: 'データ不足', reasons: ['配置確認待ち'], sample_days: 0 })) };
  renderFloorMap();
  showToast(`${numbers.length}台を仮配置しました`);
}

async function saveFloorLayout(event) {
  event.preventDefault();
  if (!floorEditorSeats.length) {
    showToast('先に台番号を自動整列してください');
    return;
  }
  const sourceUrl = byId('floor-source-url').value.trim();
  const sourceLabel = byId('floor-source-label').value.trim();
  const body = {
    hall_name: byId('floor-hall').value,
    floor_name: 'スロットフロア',
    valid_from: byId('floor-valid-from').value || todayValue(),
    width: 1000,
    height: floorData?.layout?.height || 700,
    source_url: sourceUrl,
    source_label: sourceLabel || (sourceUrl ? '公開店内マップ' : '利用者登録マップ'),
    source_kind: byId('floor-source-kind').value,
    verification_status: byId('floor-verification').value,
    notes: byId('floor-notes').value.trim(),
    seats: floorEditorSeats,
  };
  byId('floor-save-layout').disabled = true;
  try {
    const response = await fetch(apiUrl('/api/layouts'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!response.ok) throw new Error(`保存API ${response.status}`);
    showToast('店内マップを保存しました');
    await loadFloorHeat();
  } catch (error) {
    showToast(`保存できません：${error.message}`);
  } finally {
    byId('floor-save-layout').disabled = false;
  }
}

function parseFloorResultCsv(text) {
  const parseLine = line => {
    const values = []; let value = ''; let quoted = false;
    for (let i = 0; i < line.length; i += 1) {
      const char = line[i];
      if (char === '"' && quoted && line[i + 1] === '"') { value += '"'; i += 1; }
      else if (char === '"') quoted = !quoted;
      else if (char === ',' && !quoted) { values.push(value.trim()); value = ''; }
      else value += char;
    }
    values.push(value.trim());
    return values;
  };
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/).filter(line => line.trim());
  if (lines.length < 2) throw new Error('見出しと1件以上のデータが必要です');
  const aliases = {
    seat_number: ['seat_number', '台番号', '台番'], machine_name: ['machine_name', '機種名', '機種'],
    diff_coins: ['diff_coins', '差枚'], games: ['games', 'g数', 'ゲーム数'],
  };
  const headers = parseLine(lines[0]).map(value => value.toLowerCase());
  const indexes = Object.fromEntries(Object.entries(aliases).map(([key, values]) => [key, headers.findIndex(header => values.includes(header))]));
  if (indexes.seat_number < 0 || indexes.diff_coins < 0) throw new Error('「台番号」と「差枚」列が必要です');
  return lines.slice(1).map((line, index) => {
    const cols = parseLine(line);
    const seat = Number(cols[indexes.seat_number]);
    const diff = Number(String(cols[indexes.diff_coins]).replace(/[+,枚]/g, ''));
    const games = indexes.games >= 0 && cols[indexes.games] !== '' ? Number(String(cols[indexes.games]).replace(/[,Gg]/g, '')) : null;
    if (!Number.isInteger(seat) || !Number.isFinite(diff) || (games != null && !Number.isFinite(games))) throw new Error(`${index + 2}行目の数値を確認してください`);
    return { seat_number: seat, machine_name: indexes.machine_name >= 0 ? cols[indexes.machine_name] : '', diff_coins: diff, games };
  });
}

async function saveFloorResults(rows, sourceLabel = '') {
  const status = byId('floor-result-status');
  status.textContent = `${rows.length}件を保存中...`;
  const response = await fetch(apiUrl('/api/layouts/seat_results'), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      hall_name: byId('floor-hall').value,
      report_date: byId('floor-result-date').value || todayValue(),
      source_label: sourceLabel || byId('floor-result-source').value.trim() || '現地入力',
      rows,
    }),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.detail || `保存API ${response.status}`);
  status.textContent = `${result.report_date}・${result.message}`;
  showToast(result.message);
  await loadFloorHeat();
}

async function runTargetSearch() {
  const visitDate = byId('target-visit-date').value;
  byId('target-map-date').value = visitDate;
  const days = byId('target-search-days').value;
  const status = byId('target-search-status');
  status.textContent = '店舗・曜日・機種データを分析中...';
  byId('target-search-button').disabled = true;
  try {
    const response = await fetch(apiUrl(`/api/hall/target_search?visit_date=${encodeURIComponent(visitDate)}&days=${encodeURIComponent(days)}`));
    if (!response.ok) throw new Error(`分析API ${response.status}`);
    targetSearchData = await response.json();
    renderTargetSearch();
    status.textContent = `${targetSearchData.generated_at}時点の公開データで分析しました。`;
  } catch (error) {
    targetSearchData = null;
    renderTargetSearch();
    status.textContent = `分析サーバーに接続できません：${error.message}`;
  } finally {
    byId('target-search-button').disabled = false;
  }
}

function renderPerformance() {
  const performance = buildPerformanceSeries(state.results);
  const summary = byId('performance-summary');
  const chart = byId('performance-chart');
  const note = byId('performance-note');
  byId('performance-count').textContent = `${performance.tracked_count}件`;

  if (!performance.tracked_count) {
    summary.innerHTML = '<div><small>積んだ期待値</small><strong>--</strong></div><div><small>実収支</small><strong>--</strong></div><div><small>期待値との差</small><strong>--</strong></div>';
    chart.innerHTML = '<div class="performance-empty">次の実戦から自動で記録します。<br>判定した台を候補に保存し、実戦結果を入力してください。</div>';
    chart.setAttribute('aria-label', '比較できる実戦記録はまだありません');
    note.textContent = '期待値は長期的な平均の目安です。1回ごとの勝ち負けとは一致しません。';
    return;
  }

  const gapClass = performance.gap_yen >= 0 ? 'money-up' : 'money-down';
  summary.innerHTML = `
    <div><small>積んだ期待値</small><strong>${money(performance.total_expected_yen, true)}</strong></div>
    <div><small>実収支</small><strong class="${performance.total_actual_yen >= 0 ? 'money-up' : 'money-down'}">${money(performance.total_actual_yen, true)}</strong></div>
    <div><small>期待値との差</small><strong class="${gapClass}">${money(performance.gap_yen, true)}</strong></div>`;

  const width = 640;
  const height = 250;
  const padX = 48;
  const padY = 28;
  const values = [0, ...performance.points.flatMap(point => [point.cumulative_expected_yen, point.cumulative_actual_yen])];
  let minValue = Math.min(...values);
  let maxValue = Math.max(...values);
  const span = Math.max(2000, maxValue - minValue);
  minValue -= span * .12;
  maxValue += span * .12;
  const x = index => padX + (index / performance.tracked_count) * (width - padX * 2);
  const y = value => padY + ((maxValue - value) / (maxValue - minValue)) * (height - padY * 2);
  const expectedPoints = [`${x(0)},${y(0)}`, ...performance.points.map(point => `${x(point.index)},${y(point.cumulative_expected_yen)}`)].join(' ');
  const actualPoints = [`${x(0)},${y(0)}`, ...performance.points.map(point => `${x(point.index)},${y(point.cumulative_actual_yen)}`)].join(' ');
  const last = performance.points.at(-1);
  const grid = Array.from({ length: 5 }, (_, index) => {
    const gridY = padY + index * ((height - padY * 2) / 4);
    return `<line class="chart-grid" x1="${padX}" y1="${gridY}" x2="${width - padX}" y2="${gridY}"/>`;
  }).join('');
  const compact = value => `${value < 0 ? '-' : ''}${Math.abs(value) >= 10000 ? `${(Math.abs(value) / 10000).toFixed(1)}万` : Math.round(Math.abs(value)).toLocaleString('ja-JP')}円`;

  chart.innerHTML = `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true">
    ${grid}
    <line class="chart-zero" x1="${padX}" y1="${y(0)}" x2="${width - padX}" y2="${y(0)}"/>
    <text class="chart-label" x="4" y="${padY + 5}">${compact(maxValue)}</text>
    <text class="chart-label" x="4" y="${height - padY + 5}">${compact(minValue)}</text>
    <text class="chart-label" x="${padX}" y="${height - 5}">開始</text>
    <text class="chart-label" x="${width - padX}" y="${height - 5}" text-anchor="end">${performance.tracked_count}回</text>
    <polyline class="chart-expected" points="${expectedPoints}"/>
    <polyline class="chart-actual" points="${actualPoints}"/>
    <circle class="chart-dot-expected" cx="${x(last.index)}" cy="${y(last.cumulative_expected_yen)}" r="6"/>
    <circle class="chart-dot-actual" cx="${x(last.index)}" cy="${y(last.cumulative_actual_yen)}" r="6"/>
  </svg>`;
  chart.setAttribute('aria-label', `実戦${performance.tracked_count}件。累積期待値${money(performance.total_expected_yen)}、累積実収支${money(performance.total_actual_yen)}、差${money(performance.gap_yen, true)}`);
  const direction = performance.gap_yen >= 0 ? '上回っています' : '下回っています';
  const sampleNote = performance.tracked_count < 10 ? ` まだ${performance.tracked_count}件なので、短期のブレが大きい段階です。` : '';
  note.innerHTML = `実収支は積んだ期待値を<strong class="${gapClass}">${money(Math.abs(performance.gap_yen))}</strong>${direction}。${sampleNote}長期の傾向で判断してください。`;
}

function renderResults() {
  const list = byId('result-list');
  if (!state.results.length) {
    list.innerHTML = '<p class="empty">実戦記録はまだありません。<br>候補台の「実戦結果を入力」から記録できます。</p>';
    return;
  }
  list.innerHTML = [...state.results].sort((a, b) => String(b.played_on).localeCompare(String(a.played_on))).map(result => {
    const net = Number(result.returns_yen) - Number(result.investment_yen);
    const hasExpected = result.expected_value_yen !== null && result.expected_value_yen !== undefined && Number.isFinite(Number(result.expected_value_yen));
    const gap = hasExpected ? net - Number(result.expected_value_yen) : null;
    return `<article class="list-card"><div class="list-card-head"><div class="list-card-main"><strong>${esc(result.machine_name)}</strong><small>${esc(result.played_on)}・${result.played_minutes || 0}分</small></div><strong class="${net >= 0 ? 'result-profit' : 'result-loss'}">${money(net, true)}</strong></div><p class="card-meta">投資 ${money(result.investment_yen)}・回収 ${money(result.returns_yen)}${hasExpected ? `・期待値 ${money(result.expected_value_yen, true)}・差 ${money(gap, true)}` : '・期待値記録なし'}${result.notes ? `・${esc(result.notes)}` : ''}</p></article>`;
  }).join('');
}

function renderSettings() {
  byId('budget-bankroll').value = state.budget.starting_bankroll || '';
  byId('budget-loss').value = state.budget.loss_limit_yen || '';
  byId('quick-close').value = state.settings.closing_time || '22:45';
  renderVersionInfo();
}

function renderAll() {
  renderSummary();
  renderCatalogScope();
  renderGuide();
  renderCandidates();
  renderPlans();
  renderPerformance();
  renderResults();
  renderSettings();
}

function setMobileMenu(open) {
  const overlay = byId('mobile-menu-overlay');
  const trigger = byId('mobile-menu-button');
  overlay.hidden = !open;
  trigger.setAttribute('aria-expanded', String(open));
  trigger.setAttribute('aria-label', open ? 'メニューを閉じる' : 'メニューを開く');
  document.body.classList.toggle('menu-open', open);
  if (open) setTimeout(() => byId('mobile-menu-close').focus(), 0);
}

function showScreen(name) {
  if (name === 'home') activeModule = 'home';
  else if (['check', 'guide'].includes(name)) activeModule = 'hyena';
  else if (['planner', 'trend', 'target-map', 'floor-map', 'strategy'].includes(name)) activeModule = 'target';
  const navName = name;
  document.querySelectorAll('.screen').forEach(screen => screen.classList.toggle('active', screen.id === `screen-${name}`));
  const nav = document.querySelector('.bottom-nav');
  nav.hidden = activeModule === 'home';
  let visibleNavCount = 0;
  document.querySelectorAll('.nav-button').forEach(button => {
    const allowedModules = (button.dataset.moduleNav || '').split(' ').filter(Boolean);
    const visible = button.dataset.screen === 'home' || allowedModules.includes(activeModule);
    button.hidden = !visible;
    if (visible) visibleNavCount += 1;
    const active = button.dataset.screen === navName;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  nav.style.setProperty('--visible-nav-count', String(Math.max(1, visibleNavCount)));
  document.querySelectorAll('.menu-item[data-screen-target]').forEach(button => {
    button.classList.toggle('active', button.dataset.screenTarget === name);
  });
  document.body.dataset.module = activeModule;
  byId('brand-mode-label').textContent = activeModule === 'hyena' ? 'ハイエナ専用' : activeModule === 'target' ? '狙い台捜索専用' : 'スマスロ攻略ホーム';
  scrollTo({ top: 0, behavior: 'smooth' });
  if (name === 'target-map' && !targetMapData) setTimeout(loadTargetHeatMap, 80);
  if (name === 'trend' && !trendData) setTimeout(loadTrendProfile, 80);
  if (name === 'floor-map' && !floorData) setTimeout(loadFloorHeat, 80);
}

function updateNetworkBadge() {
  const badge = byId('network-badge');
  badge.textContent = navigator.onLine ? 'オンライン' : 'オフライン';
  badge.classList.toggle('offline', !navigator.onLine);
}

document.querySelector('.bottom-nav').addEventListener('click', event => {
  const button = event.target.closest('[data-screen]');
  if (button) showScreen(button.dataset.screen);
});
document.addEventListener('click', event => {
  const target = event.target.closest('[data-screen-target]');
  if (target) {
    showScreen(target.dataset.screenTarget);
    if (target.hasAttribute('data-menu-screen')) setMobileMenu(false);
  }
});
byId('mobile-menu-button').addEventListener('click', () => {
  setMobileMenu(byId('mobile-menu-overlay').hidden);
});
byId('mobile-menu-close').addEventListener('click', () => setMobileMenu(false));
byId('mobile-menu-overlay').addEventListener('click', event => {
  if (event.target.closest('[data-menu-close]')) setMobileMenu(false);
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !byId('mobile-menu-overlay').hidden) setMobileMenu(false);
});
byId('guide-search').addEventListener('input', renderGuide);
byId('guide-mode').addEventListener('change', renderGuide);
byId('quick-machine').addEventListener('change', () => populateProfileOptions());
byId('quick-profile').addEventListener('change', () => syncConditions(currentProfile()));
byId('quick-close').addEventListener('change', async event => {
  state.settings.closing_time = event.target.value;
  await writeLocalState();
});

byId('target-search-form').addEventListener('submit', async event => {
  event.preventDefault();
  await runTargetSearch();
});

byId('target-map-form').addEventListener('submit', async event => {
  event.preventDefault();
  await loadTargetHeatMap();
});

byId('trend-form').addEventListener('submit', async event => {
  event.preventDefault();
  await loadTrendProfile();
});

byId('floor-form').addEventListener('submit', async event => {
  event.preventDefault();
  await loadFloorHeat();
});

byId('floor-auto-layout').addEventListener('click', createAutoFloorSeats);
byId('floor-editor-form').addEventListener('submit', saveFloorLayout);
byId('floor-result-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = byId('floor-result-save');
  button.disabled = true;
  try {
    await saveFloorResults([{
      seat_number: Number(byId('floor-result-number').value),
      machine_name: byId('floor-result-machine').value.trim(),
      diff_coins: Number(byId('floor-result-diff').value),
      games: byId('floor-result-games').value === '' ? null : Number(byId('floor-result-games').value),
    }]);
    ['floor-result-number', 'floor-result-machine', 'floor-result-diff', 'floor-result-games'].forEach(id => { byId(id).value = ''; });
  } catch (error) { showToast(`保存できません：${error.message}`); }
  finally { button.disabled = false; }
});
byId('floor-result-csv').addEventListener('change', async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  try { await saveFloorResults(parseFloorResultCsv(await file.text()), `CSV: ${file.name}`); }
  catch (error) { showToast(`CSVを読めません：${error.message}`); }
  finally { event.target.value = ''; }
});
byId('floor-result-template').addEventListener('click', () => {
  const blob = new Blob(['台番号,機種名,差枚,G数\n501,スマスロ北斗の拳,1800,7200\n'], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob); const anchor = document.createElement('a');
  anchor.href = url; anchor.download = 'seat-results-template.csv'; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

async function saveAnalysisPlan({ visitDate, storeName, machineName, seatNumber = '', notes }) {
  const duplicate = state.plans.some(plan => plan.visit_date === visitDate && plan.store_name === storeName && plan.machine_name === machineName && String(plan.machine_number || '') === String(seatNumber || ''));
  if (duplicate) {
    showToast('同じ候補はすでに作戦に入っています');
    return;
  }
  state.plans.push({
    id: newId(), created_at: new Date().toISOString(), visit_date: visitDate,
    store_name: storeName, machine_name: machineName, machine_number: seatNumber ? String(seatNumber) : '',
    previous_games: '', strategy: 'setting', priority: 1, notes, checked: false,
  });
  await writeLocalState();
  renderPlans();
  showToast('朝一の作戦に保存しました');
}

byId('trend-results').addEventListener('click', async event => {
  const button = event.target.closest('[data-trend-machine]');
  if (!button || !trendData) return;
  const machine = trendData.machine_profile?.[Number(button.dataset.trendMachine)];
  if (!machine) return;
  await saveAnalysisPlan({
    visitDate: byId('trend-date').value, storeName: trendData.hall_name, machineName: machine.machine_name,
    notes: `店舗傾向分析：${machine.score}点・${machine.sample_days}日・平均${signedCoins(machine.avg_diff)}`,
  });
});

byId('floor-seat-detail').addEventListener('click', async event => {
  const button = event.target.closest('[data-floor-plan-seat]');
  if (!button || !floorData) return;
  const seat = floorData.seats.find(item => item.seat_number === Number(button.dataset.floorPlanSeat));
  if (!seat) return;
  await saveAnalysisPlan({
    visitDate: byId('floor-date').value, storeName: floorData.hall_name,
    machineName: seat.machine_name || '機種未登録', seatNumber: seat.seat_number,
    notes: `店内マップ：${seat.score ?? '未採点'}点・${seat.heat_level}。${seat.reasons.join('／')}`,
  });
});

byId('target-search-results').addEventListener('click', async event => {
  const button = event.target.closest('[data-target-hall-index]');
  if (!button || !targetSearchData) return;
  const hall = targetSearchData.halls?.[Number(button.dataset.targetHallIndex)];
  const machine = hall?.target_machines?.[Number(button.dataset.targetMachineIndex)];
  if (!hall || !machine) return;
  const duplicate = state.plans.some(plan => plan.visit_date === targetSearchData.visit_date && plan.store_name === hall.hall_name && plan.machine_name === machine.machine_name);
  if (duplicate) {
    showToast('同じ候補はすでに作戦に入っています');
    return;
  }
  state.plans.push({
    id: newId(),
    created_at: new Date().toISOString(),
    visit_date: targetSearchData.visit_date,
    store_name: hall.hall_name,
    machine_name: machine.machine_name,
    machine_number: '',
    previous_games: null,
    strategy: 'setting',
    priority: hall.rank === 1 ? 1 : 2,
    notes: `分析候補：店舗${hall.score}点（信頼度${hall.confidence}）／機種${machine.score}点／平均${signedCoins(machine.avg_diff)}／${machine.sample_days}日`,
    status: 'planned',
  });
  await writeLocalState();
  renderPlans();
  showToast('分析候補を朝一の作戦に追加しました');
});

byId('guide-list').addEventListener('click', event => {
  const button = event.target.closest('[data-check-profile]');
  if (!button) return;
  populateMachines(Number(button.dataset.checkProfile));
  byId('quick-current').value = button.dataset.checkValue;
  showScreen('check');
  setTimeout(() => byId('quick-current').focus(), 250);
});

byId('quick-form').addEventListener('submit', event => {
  event.preventDefault();
  const profile = currentProfile();
  const currentValue = Number(byId('quick-current').value);
  const result = assessQuick({
    profile,
    currentValue,
    riskCapacityYen: calculateSummary(state).risk_capacity_yen,
    exchangeType: byId('quick-exchange').value,
    fundingMode: byId('quick-funding').value,
    resetStatus: byId('quick-reset').value,
    minutesUntilClose: minutesUntilClosing(byId('quick-close').value),
  });
  lastAssessment = { result, profile, currentValue };
  renderQuickResult(result, profile, currentValue);
  setTimeout(() => byId('quick-result').scrollIntoView({ behavior: 'smooth', block: 'start' }), 60);
});

byId('quick-result').addEventListener('click', async event => {
  if (!event.target.closest('#save-candidate-button') || !lastAssessment?.result.actionable) return;
  const { result, profile, currentValue } = lastAssessment;
  state.candidates.unshift({
    id: newId(),
    created_at: new Date().toISOString(),
    catalog_key: profile.catalog_key,
    machine_name: profile.machine_name,
    condition_label: profile.condition_label,
    unit_label: profile.unit_label,
    current_value: currentValue,
    expected_value_yen: result.expected_value_yen,
    worst_case_investment_yen: result.worst_case_investment_yen,
  });
  await writeLocalState();
  renderCandidates();
  showScreen('guide');
  showToast('候補台に保存しました');
});

byId('candidate-list').addEventListener('click', async event => {
  const resultButton = event.target.closest('[data-result-candidate]');
  const skipButton = event.target.closest('[data-skip-candidate]');
  if (resultButton) {
    const candidate = state.candidates.find(item => item.id === resultButton.dataset.resultCandidate);
    if (!candidate) return;
    byId('result-candidate-id').value = candidate.id;
    byId('result-machine').textContent = candidate.machine_name;
    byId('result-date').value = todayValue();
    byId('result-investment').value = '';
    byId('result-returns').value = '';
    byId('result-minutes').value = '';
    byId('result-notes').value = '';
    byId('result-dialog').showModal();
  } else if (skipButton) {
    state.candidates = state.candidates.filter(item => item.id !== skipButton.dataset.skipCandidate);
    await writeLocalState();
    renderCandidates();
    showToast('候補台を見送りました');
  }
});

byId('planner-form').addEventListener('submit', async event => {
  event.preventDefault();
  state.plans.push({
    id: newId(),
    created_at: new Date().toISOString(),
    visit_date: byId('plan-date').value,
    store_name: byId('plan-store').value.trim(),
    machine_name: byId('plan-machine').value.trim(),
    machine_number: byId('plan-number').value.trim(),
    previous_games: byId('plan-previous-games').value === '' ? null : Number(byId('plan-previous-games').value),
    strategy: byId('plan-strategy').value,
    priority: Number(byId('plan-priority').value),
    notes: byId('plan-notes').value.trim(),
    status: 'planned',
  });
  await writeLocalState();
  renderPlans();
  byId('plan-machine').value = '';
  byId('plan-number').value = '';
  byId('plan-previous-games').value = '';
  byId('plan-notes').value = '';
  showToast('明日の狙い台に追加しました');
});

byId('planner-list').addEventListener('click', async event => {
  const toggle = event.target.closest('[data-plan-toggle]');
  const remove = event.target.closest('[data-plan-remove]');
  if (toggle) {
    const plan = state.plans.find(item => item.id === toggle.dataset.planToggle);
    if (!plan) return;
    plan.status = plan.status === 'checked' ? 'planned' : 'checked';
    await writeLocalState();
    renderPlans();
    showToast(plan.status === 'checked' ? '朝一確認済みにしました' : '未確認に戻しました');
  } else if (remove) {
    state.plans = state.plans.filter(item => item.id !== remove.dataset.planRemove);
    await writeLocalState();
    renderPlans();
    showToast('狙い台から削除しました');
  }
});

byId('dialog-close').addEventListener('click', () => byId('result-dialog').close());
byId('result-form').addEventListener('submit', async event => {
  event.preventDefault();
  const candidateId = byId('result-candidate-id').value;
  const candidate = state.candidates.find(item => item.id === candidateId);
  if (!candidate) return;
  state.results.unshift({
    id: newId(),
    candidate_id: candidate.id,
    created_at: new Date().toISOString(),
    machine_name: candidate.machine_name,
    played_on: byId('result-date').value,
    expected_value_yen: candidate.expected_value_yen !== null && candidate.expected_value_yen !== undefined ? Number(candidate.expected_value_yen) : null,
    investment_yen: Number(byId('result-investment').value),
    returns_yen: Number(byId('result-returns').value),
    played_minutes: Number(byId('result-minutes').value || 0),
    notes: byId('result-notes').value.trim(),
  });
  state.candidates = state.candidates.filter(item => item.id !== candidate.id);
  await writeLocalState();
  byId('result-dialog').close();
  renderAll();
  showScreen('results');
  showToast('実戦結果を保存しました');
});

byId('budget-form').addEventListener('submit', async event => {
  event.preventDefault();
  state.budget.starting_bankroll = Number(byId('budget-bankroll').value);
  state.budget.loss_limit_yen = Number(byId('budget-loss').value);
  await writeLocalState();
  renderAll();
  showScreen('check');
  showToast('資金設定を保存しました');
});

byId('export-button').addEventListener('click', () => {
  const payload = JSON.stringify({ app: 'PACHI TOOL Mobile', exported_at: new Date().toISOString(), state }, null, 2);
  const url = URL.createObjectURL(new Blob([payload], { type: 'application/json' }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `pachi-tool-backup-${todayValue()}.json`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  showToast('バックアップを書き出しました');
});

byId('import-file').addEventListener('change', async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    const parsed = JSON.parse(await file.text());
    if (parsed.app !== 'PACHI TOOL Mobile' || !parsed.state) throw new Error('形式が違います');
    state = normalizeState(parsed.state);
    await writeLocalState();
    renderAll();
    showToast('バックアップを復元しました');
  } catch (error) {
    showToast(`読み込み失敗：${error.message}`);
  } finally {
    event.target.value = '';
  }
});

byId('reset-button').addEventListener('click', async () => {
  if (!confirm('このiPhone内の資金設定・候補台・実戦結果をすべて削除しますか？')) return;
  state = clone(defaultState);
  await writeLocalState();
  renderAll();
  showToast('端末内データを初期化しました');
});

window.addEventListener('online', updateNetworkBadge);
window.addEventListener('offline', updateNetworkBadge);

byId('mobile-version-button').addEventListener('click', () => {
  showScreen('settings');
  const notes = byId('patch-notes-group');
  notes.open = true;
  markVersionSeen();
  setTimeout(() => notes.scrollIntoView({ behavior: 'smooth', block: 'start' }), 80);
});

byId('patch-notes-group').addEventListener('toggle', event => {
  if (event.currentTarget.open) markVersionSeen();
});

async function initialize() {
  try {
    [state] = await Promise.all([readLocalState(), loadCatalog(), loadVersionInfo()]);
    state = normalizeState(state);
    byId('plan-date').value = tomorrowValue();
    byId('target-visit-date').value = tomorrowValue();
    byId('target-map-date').value = tomorrowValue();
    byId('trend-date').value = tomorrowValue();
    byId('floor-date').value = tomorrowValue();
    byId('floor-valid-from').value = todayValue();
    byId('floor-result-date').value = todayValue();
    await loadTargetHallOptions();
    populateMachines();
    renderAll();
    updateNetworkBadge();
    if ('serviceWorker' in navigator) {
      await navigator.serviceWorker.register('./sw.js', { scope: './' });
    }
  } catch (error) {
    byId('guide-list').innerHTML = `<p class="empty">起動失敗：${esc(error.message)}<br>一度オンラインで開き直してください。</p>`;
    updateNetworkBadge();
  }
}

initialize();
