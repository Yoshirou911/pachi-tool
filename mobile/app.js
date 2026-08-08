import {
  JUDGMENT_LABELS,
  assessQuick,
  buildGuideRows,
  calculateSummary,
  minutesUntilClosing,
  money,
} from './core.mjs';

const APP_VERSION = '1.0.0';
const DB_NAME = 'pachi-tool-mobile';
const STORE_NAME = 'app-state';
const STATE_KEY = 'main';
const defaultState = {
  version: 1,
  budget: { starting_bankroll: 0, loss_limit_yen: 0 },
  candidates: [],
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
function showToast(message) {
  const toast = byId('toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 2200);
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
    results: Array.isArray(saved?.results) ? saved.results : [],
  };
}

async function loadCatalog() {
  const response = await fetch('./catalog.json');
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
  alert.textContent = summary.configured ? '' : '最初に「設定」から運用資金と損失上限を入力してください。資金未設定では打つ候補を出しません。';
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
    return `<article class="guide-row">
      <div class="guide-row-top">
        <span class="signal signal-${esc(assessment.judgment)}">${esc(JUDGMENT_LABELS[assessment.judgment] || assessment.judgment)}</span>
        <div class="guide-main"><strong>${esc(profile.machine_name)}</strong><small>${esc(profile.condition_label)}</small></div>
      </div>
      <div class="guide-data">
        <div><small>現在値</small><strong>${Number(point.value).toLocaleString('ja-JP')}${esc(profile.unit_label)}</strong></div>
        <div><small>期待値</small><strong class="${Number(point.ev_yen) >= 0 ? 'money-up' : 'money-down'}">${money(point.ev_yen, true)}</strong></div>
        <div><small>必要資金</small><strong>${money(worst)}</strong></div>
      </div>
      <div class="guide-actions">
        <button class="mini-button" type="button" data-check-profile="${profile.id}" data-check-value="${Number(point.value)}">10秒判定へ</button>
        ${source ? `<a class="source-link" href="${esc(source)}" target="_blank" rel="noopener">出典を見る</a>` : ''}
      </div>
    </article>`;
  }).join('');
}

function renderQuickResult(result, profile, currentValue) {
  const label = JUDGMENT_LABELS[result.judgment] || result.judgment;
  const warnings = (result.warnings || []).map(item => `<li>${esc(item)}</li>`).join('');
  byId('quick-result').innerHTML = `
    <div class="decision-head"><div><span class="eyebrow">DECISION</span><h2>${esc(label)}</h2></div><span class="signal signal-${esc(result.judgment)}">${result.actionable ? '候補' : '停止'}</span></div>
    <p class="decision-reason">${esc(result.reason)}</p>
    <div class="result-metrics">
      <div><small>現在値</small><strong>${Number(currentValue).toLocaleString('ja-JP')}${esc(profile.unit_label)}</strong></div>
      <div><small>開始ライン</small><strong>${Number(profile.start_threshold).toLocaleString('ja-JP')}${esc(profile.unit_label)}</strong></div>
      <div><small>期待値</small><strong class="money-up">${money(result.expected_value_yen, true)}</strong></div>
      <div><small>必要資金</small><strong>${money(result.worst_case_investment_yen)}</strong></div>
      <div><small>閉店まで</small><strong>${result.minutes_until_close}分</strong></div>
      <div><small>許容資金</small><strong>${money(calculateSummary(state).risk_capacity_yen)}</strong></div>
    </div>
    ${warnings ? `<ul class="warning-list">${warnings}</ul>` : ''}
    <div class="stop-rule"><b>やめどき</b>${esc(profile.stop_rule || '未登録')}</div>
    ${result.actionable ? '<button id="save-candidate-button" class="secondary-button" type="button">候補台として保存</button>' : ''}`;
}

function renderCandidates() {
  const list = byId('candidate-list');
  if (!state.candidates.length) {
    list.innerHTML = '<p class="empty">保存した候補台はありません</p>';
    return;
  }
  list.innerHTML = state.candidates.map(candidate => `<article class="list-card">
    <div class="list-card-head"><span class="signal signal-target">候補</span><div class="list-card-main"><strong>${esc(candidate.machine_name)}</strong><small>${esc(candidate.condition_label)}</small></div></div>
    <p class="card-meta">現在 ${Number(candidate.current_value).toLocaleString('ja-JP')}${esc(candidate.unit_label)}・期待値 ${money(candidate.expected_value_yen, true)}・必要資金 ${money(candidate.worst_case_investment_yen)}</p>
    <div class="card-actions"><button class="play" type="button" data-result-candidate="${esc(candidate.id)}">実戦結果を入力</button><button class="skip" type="button" data-skip-candidate="${esc(candidate.id)}">見送る</button></div>
  </article>`).join('');
}

function renderResults() {
  const list = byId('result-list');
  if (!state.results.length) {
    list.innerHTML = '<p class="empty">実戦結果はまだありません</p>';
    return;
  }
  list.innerHTML = [...state.results].sort((a, b) => String(b.played_on).localeCompare(String(a.played_on))).map(result => {
    const net = Number(result.returns_yen) - Number(result.investment_yen);
    return `<article class="list-card"><div class="list-card-head"><div class="list-card-main"><strong>${esc(result.machine_name)}</strong><small>${esc(result.played_on)}・${result.played_minutes || 0}分</small></div><strong class="${net >= 0 ? 'result-profit' : 'result-loss'}">${money(net, true)}</strong></div><p class="card-meta">投資 ${money(result.investment_yen)}・回収 ${money(result.returns_yen)}${result.notes ? `・${esc(result.notes)}` : ''}</p></article>`;
  }).join('');
}

function renderSettings() {
  byId('budget-bankroll').value = state.budget.starting_bankroll || '';
  byId('budget-loss').value = state.budget.loss_limit_yen || '';
  byId('quick-close').value = state.settings.closing_time || '22:45';
  byId('app-version').textContent = `PACHI TOOL Mobile v${APP_VERSION}・期待値データ ${profiles.length}条件`;
}

function renderAll() {
  renderSummary();
  renderGuide();
  renderCandidates();
  renderResults();
  renderSettings();
}

function showScreen(name) {
  document.querySelectorAll('.screen').forEach(screen => screen.classList.toggle('active', screen.id === `screen-${name}`));
  document.querySelectorAll('.nav-button').forEach(button => button.classList.toggle('active', button.dataset.screen === name));
  scrollTo({ top: 0, behavior: 'smooth' });
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
byId('guide-search').addEventListener('input', renderGuide);
byId('guide-mode').addEventListener('change', renderGuide);
byId('quick-machine').addEventListener('change', () => populateProfileOptions());
byId('quick-profile').addEventListener('change', () => syncConditions(currentProfile()));
byId('quick-close').addEventListener('change', async event => {
  state.settings.closing_time = event.target.value;
  await writeLocalState();
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

byId('dialog-close').addEventListener('click', () => byId('result-dialog').close());
byId('result-form').addEventListener('submit', async event => {
  event.preventDefault();
  const candidateId = byId('result-candidate-id').value;
  const candidate = state.candidates.find(item => item.id === candidateId);
  if (!candidate) return;
  state.results.unshift({
    id: newId(),
    candidate_id: candidate.id,
    machine_name: candidate.machine_name,
    played_on: byId('result-date').value,
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

async function initialize() {
  try {
    [state] = await Promise.all([readLocalState(), loadCatalog()]);
    state = normalizeState(state);
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
