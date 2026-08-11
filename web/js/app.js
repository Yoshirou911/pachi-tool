/* pachi-tool フロントエンド SPA */
'use strict';

const API = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
  ? ''   // same origin (FastAPI が静的配信)
  : '';

// ---------------------------------------------------------------------------
// API クライアント
// ---------------------------------------------------------------------------
// サーバー側で PACHI_ACCESS_TOKEN を設定した場合のみ要求されるアクセスキー。
// 未設定のサーバー（ローカル/デスクトップ版）に対しては何も送らず、従来どおり動く。
const TOKEN_KEY = 'pachi_access_token';
function getStoredToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}
function setStoredToken(token) {
  try { localStorage.setItem(TOKEN_KEY, token); } catch { /* ignore */ }
}

async function apiFetch(path, opts = {}, _retried = false) {
  const url = API + path;
  let res;
  try {
    const headers = opts.body ? { 'Content-Type': 'application/json', ...(opts.headers || {}) } : (opts.headers || {});
    const token = getStoredToken();
    if (token) headers['X-Pachi-Token'] = token;
    res = await fetch(url, { ...opts, headers });
  } catch {
    throw new Error('サーバーに接続できません。起動状態とネットワークを確認してください');
  }
  if (res.status === 401 && !_retried) {
    const entered = window.prompt('アクセスキーを入力してください');
    if (entered) {
      setStoredToken(entered);
      return apiFetch(path, opts, true);
    }
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

const api = {
  getVersion: () => apiFetch(`/api/version?ts=${Date.now()}`),
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
  getOpportunityDashboard: (month) => apiFetch(`/api/opportunity/dashboard?month=${encodeURIComponent(month)}`),
  quickAssessOpportunity: (body) => apiFetch('/api/opportunity/quick-assess', { method: 'POST', body: JSON.stringify(body) }),
  createOpportunityProfile: (body) => apiFetch('/api/opportunity/profiles', { method: 'POST', body: JSON.stringify(body) }),
  deleteOpportunityProfile: (id) => apiFetch(`/api/opportunity/profiles/${id}`, { method: 'DELETE' }),
  createOpportunityCandidate: (body) => apiFetch('/api/opportunity/candidates', { method: 'POST', body: JSON.stringify(body) }),
  updateOpportunityCandidate: (id, body) => apiFetch(`/api/opportunity/candidates/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  createOpportunityResult: (id, body) => apiFetch(`/api/opportunity/candidates/${id}/result`, { method: 'POST', body: JSON.stringify(body) }),
  updateOpportunityBudget: (body) => apiFetch('/api/opportunity/budget', { method: 'PUT', body: JSON.stringify(body) }),
  getOpportunityCrawlerStatus: () => apiFetch('/api/opportunity/crawler/status'),
  getOpportunityCrawlerCandidates: () => apiFetch('/api/opportunity/crawler/candidates?limit=50'),
  runOpportunityCrawler: () => apiFetch('/api/opportunity/crawler/run?max_profiles=100', { method: 'POST' }),
  approveOpportunityCrawlerCandidate: (id) => apiFetch(`/api/opportunity/crawler/candidates/${id}/approve`, { method: 'POST', body: JSON.stringify({ note: 'PC画面で出典と差分を確認して承認' }) }),
  rejectOpportunityCrawlerCandidate: (id) => apiFetch(`/api/opportunity/crawler/candidates/${id}/reject`, { method: 'POST', body: JSON.stringify({ note: 'PC画面で不採用' }) }),
};

const VERSION_SEEN_KEY = 'pachi-version-seen';
let desktopReleaseInfo = null;

function versionDateLabel(value) {
  return String(value || '').replace(/-/g, '/');
}

function renderDesktopVersion() {
  if (!desktopReleaseInfo) return;
  const seen = localStorage.getItem(VERSION_SEEN_KEY) === desktopReleaseInfo.version;
  document.getElementById('desktop-version-label').textContent = `v${desktopReleaseInfo.version}`;
  document.getElementById('desktop-version-button').classList.toggle('seen', seen);
  document.getElementById('desktop-release-current').innerHTML = `<strong>PACHI TOOL v${esc(desktopReleaseInfo.version)}</strong>${esc(desktopReleaseInfo.channel)}・${versionDateLabel(desktopReleaseInfo.released_on)}公開`;
  document.getElementById('desktop-patch-notes').innerHTML = (desktopReleaseInfo.patch_notes || []).map(note => `
    <article class="desktop-patch-note">
      <div class="desktop-patch-note-head"><strong>v${esc(note.version)}</strong><time>${versionDateLabel(note.released_on)}</time></div>
      <h3>${esc(note.title)}</h3>
      <ul>${(note.items || []).map(item => `<li>${esc(item)}</li>`).join('')}</ul>
    </article>`).join('');
}

async function loadDesktopVersion() {
  try {
    desktopReleaseInfo = await api.getVersion();
    renderDesktopVersion();
  } catch (_) {
    document.getElementById('desktop-version-label').textContent = 'v2.5.0';
  }
}

function openVersionNotes() {
  document.getElementById('version-overlay').style.display = 'flex';
  if (desktopReleaseInfo) localStorage.setItem(VERSION_SEEN_KEY, desktopReleaseInfo.version);
  renderDesktopVersion();
}

function closeVersionNotes() {
  document.getElementById('version-overlay').style.display = 'none';
}

document.getElementById('desktop-version-button').addEventListener('click', openVersionNotes);
document.getElementById('version-close').addEventListener('click', closeVersionNotes);
document.getElementById('version-overlay').addEventListener('click', event => {
  if (event.target === event.currentTarget) closeVersionNotes();
});

// ---------------------------------------------------------------------------
// Loading helpers
// ---------------------------------------------------------------------------
function loadingHtml(lines = 2) {
  const rows = Array.from({ length: lines }, (_, i) =>
    `<div style="height:${i === 0 ? 14 : 10}px;width:${i === 0 ? 70 : 50}%;background:rgba(255,255,255,.06);border-radius:4px;animation:pulse 1.4s ease-in-out infinite ${i * 0.15}s"></div>`
  ).join('');
  return `<div style="display:flex;flex-direction:column;gap:8px;padding:10px 0">${rows}</div>`;
}

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
const sidebarToggle = document.getElementById('sidebar-toggle');
const sidebarBackdrop = document.getElementById('sidebar-backdrop');
const mainNavigation = document.getElementById('main-navigation');
const desktopSidebar = window.matchMedia('(min-width: 900px)');
const navBackButton = document.getElementById('nav-back');
const navForwardButton = document.getElementById('nav-forward');
let navigationEntries = ['home'];
let navigationIndex = 0;
let activeModule = 'home';
let desktopTargetSearchData = null;
let desktopTrendProfileData = null;
let desktopFloorData = null;
let desktopFloorEditorSeats = [];
let desktopHallOptions = [];
const TARGET_MODULE_TABS = new Set(['target-search', 'trend-profile', 'floor-map', 'estimate', 'hall', 'map', 'ai']);
const HYENA_MODULE_TABS = new Set(['opportunity', 'machines', 'session']);

function updateModuleNavigation() {
  document.body.dataset.module = activeModule;
  let visibleCount = 0;
  document.querySelectorAll('.tab-btn').forEach(button => {
    const moduleName = button.dataset.moduleNav;
    const visible = button.dataset.tab === 'home' || moduleName === activeModule;
    button.hidden = !visible;
    if (visible) visibleCount += 1;
  });
  const modeTitle = document.getElementById('sidebar-mode-title');
  const modeSubtitle = document.getElementById('sidebar-mode-subtitle');
  const help = document.getElementById('sidebar-help');
  if (activeModule === 'hyena') {
    modeTitle.textContent = 'ハイエナ専用';
    modeSubtitle.textContent = 'HYENA MODE';
    help.textContent = '空き台判定・期待値・実戦記録だけを表示しています';
  } else if (activeModule === 'target') {
    modeTitle.textContent = '狙い台捜索専用';
    modeSubtitle.textContent = 'TARGET MODE';
    help.textContent = '店舗・曜日・機種の分析機能だけを表示しています';
  } else {
    modeTitle.textContent = 'モード選択';
    modeSubtitle.textContent = 'CHOOSE A MODE';
    help.textContent = 'ホームから攻略モードを選んでください';
  }
  mainNavigation.classList.toggle('module-home-only', visibleCount === 1);
}

function updateNavigationButtons() {
  navBackButton.disabled = navigationIndex <= 0;
  navForwardButton.disabled = navigationIndex >= navigationEntries.length - 1;
}

function recordNavigation(tabId) {
  if (navigationEntries[navigationIndex] === tabId) return;
  navigationEntries = navigationEntries.slice(0, navigationIndex + 1);
  navigationEntries.push(tabId);
  navigationIndex = navigationEntries.length - 1;
}

function sidebarIsOpen() {
  return desktopSidebar.matches
    ? !document.body.classList.contains('sidebar-closed')
    : document.body.classList.contains('sidebar-open');
}

function updateSidebarA11y() {
  const isOpen = sidebarIsOpen();
  sidebarToggle.setAttribute('aria-expanded', String(isOpen));
  sidebarToggle.setAttribute('aria-label', isOpen ? 'メニューを閉じる' : 'メニューを開く');
  mainNavigation.toggleAttribute('inert', !isOpen);
  mainNavigation.setAttribute('aria-hidden', String(!isOpen));
}

function setSidebar(open) {
  if (desktopSidebar.matches) {
    document.body.classList.remove('sidebar-open');
    document.body.classList.toggle('sidebar-closed', !open);
    localStorage.setItem('pachi_sidebar_closed', String(!open));
  } else {
    document.body.classList.remove('sidebar-closed');
    document.body.classList.toggle('sidebar-open', open);
  }
  updateSidebarA11y();
}

function initializeSidebar() {
  const open = desktopSidebar.matches
    ? localStorage.getItem('pachi_sidebar_closed') !== 'true'
    : false;
  setSidebar(open);
}

sidebarToggle.addEventListener('click', () => setSidebar(!sidebarIsOpen()));
sidebarBackdrop.addEventListener('click', () => setSidebar(false));
desktopSidebar.addEventListener('change', initializeSidebar);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && sidebarIsOpen()) setSidebar(false);
});
initializeSidebar();

function switchTab(tabId, options = {}) {
  const { record = true, resetHistory = false } = options;
  if (!document.getElementById(`page-${tabId}`)) return;
  if (tabId === 'home') activeModule = 'home';
  else if (TARGET_MODULE_TABS.has(tabId)) activeModule = 'target';
  else if (HYENA_MODULE_TABS.has(tabId)) activeModule = 'hyena';
  updateModuleNavigation();
  if (resetHistory) {
    navigationEntries = [tabId];
    navigationIndex = 0;
  } else if (record) {
    recordNavigation(tabId);
  }
  document.querySelectorAll('.tab-btn').forEach(b => {
    const active = b.dataset.tab === tabId;
    b.classList.toggle('active', active);
    if (active) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === `page-${tabId}`));
  window.scrollTo(0, 0);
  localStorage.setItem('pachi_last_tab', tabId);
  if (!desktopSidebar.matches) setSidebar(false);
  if (tabId === 'session') loadSessions();
  if (tabId === 'hall') {
    const last = localStorage.getItem('pachi_last_hall_tab') || 'compare';
    switchHallTab(last);
  }
  if (tabId === 'map') loadMapPage();
  if (tabId === 'trend-profile' && !desktopTrendProfileData) loadDesktopTrendProfile();
  if (tabId === 'floor-map' && !desktopFloorData) loadDesktopFloorHeat();
  if (tabId === 'ai') loadAiPage();
  if (tabId === 'machines') loadMachinesPage();
  if (tabId === 'opportunity') loadOpportunityPage();
  if (tabId === 'target-search' && !desktopTargetSearchData) loadDesktopTargetSearch();
  updateNavigationButtons();
}

function navigateHistory(direction) {
  const nextIndex = navigationIndex + direction;
  if (nextIndex < 0 || nextIndex >= navigationEntries.length) return;
  navigationIndex = nextIndex;
  switchTab(navigationEntries[navigationIndex], { record: false });
}

navBackButton.addEventListener('click', () => navigateHistory(-1));
navForwardButton.addEventListener('click', () => navigateHistory(1));
document.addEventListener('keydown', event => {
  if (!event.altKey) return;
  if (event.key === 'ArrowLeft') {
    event.preventDefault();
    navigateHistory(-1);
  } else if (event.key === 'ArrowRight') {
    event.preventDefault();
    navigateHistory(1);
  }
});
updateNavigationButtons();

function switchHallTab(htabId) {
  document.querySelectorAll('.hall-sub-btn').forEach(b => b.classList.toggle('active', b.dataset.htab === htabId));
  document.querySelectorAll('.htab-pane').forEach(p => {
    p.classList.toggle('active', p.id === `htab-${htabId}`);
  });
  localStorage.setItem('pachi_last_hall_tab', htabId);
  if (htabId === 'compare') loadHallCompare();
  if (htabId === 'detail') loadHallPage();
  if (htabId === 'admin') loadScrapeManager();
  if (htabId === 'calendar') initCalendar();
}

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

document.getElementById('page-home').addEventListener('click', event => {
  const destination = event.target.closest('[data-home-destination]');
  if (destination) switchTab(destination.dataset.homeDestination);
});

function localDateValue(offsetDays = 0) {
  const value = new Date();
  value.setDate(value.getDate() + offsetDays);
  return value.toLocaleDateString('sv');
}

function desktopSignedCoins(value) {
  const number = Number(value || 0);
  return `${number >= 0 ? '+' : ''}${number.toLocaleString('ja-JP')}枚`;
}

function renderDesktopTargetSearch(data) {
  const container = document.getElementById('desktop-target-results');
  const halls = data.halls || [];
  const insufficient = data.insufficient_halls || [];
  if (!halls.length) {
    container.innerHTML = `<div class="card desktop-target-empty"><strong>おすすめを出せるデータがありません</strong><span>${esc(data.notice || '取得日数が増えるまでお待ちください。')}</span></div>`;
    return;
  }
  container.innerHTML = `
    <div class="desktop-target-heading"><div><span class="page-eyebrow">${esc(data.visit_date)} ${esc(data.weekday)}曜日</span><h2>店舗・狙い機種ランキング</h2></div><span class="badge">${halls.length}店舗</span></div>
    ${halls.map(hall => `<article class="card desktop-target-hall">
      <div class="desktop-target-hall-head"><span class="desktop-target-rank">${hall.rank}</span><div><h3>${esc(hall.hall_name)}</h3><p>${esc(hall.basis)}・最終データ ${esc(hall.latest_date)}</p></div><div class="desktop-target-score"><b>${hall.score}</b><small>点</small></div></div>
      <div class="desktop-target-metrics"><span><small>店舗平均</small><b class="${hall.avg_diff >= 0 ? 'text-up' : 'text-down'}">${desktopSignedCoins(hall.avg_diff)}</b></span><span><small>プラス日率</small><b>${hall.positive_rate}%</b></span><span><small>実績日数</small><b>${hall.sample_days}日</b></span><span><small>信頼度</small><b class="target-confidence-${hall.confidence === '高' ? 'high' : hall.confidence === '中' ? 'mid' : 'low'}">${esc(hall.confidence)}</b></span></div>
      <div class="desktop-target-reasons">${(hall.reasons || []).map(reason => `<span>${esc(reason)}</span>`).join('')}</div>
      <div class="desktop-target-machines">
        ${(hall.target_machines || []).slice(0, 5).map((machine, index) => `<div><span class="machine-order">${index + 1}</span><strong>${esc(machine.machine_name)}</strong><small>${machine.sample_days}日・信頼${machine.reliability_pct ?? 0}%・プラス率${machine.positive_rate}%</small><b class="${machine.avg_diff >= 0 ? 'text-up' : 'text-down'}">補正 ${desktopSignedCoins(machine.avg_diff)}</b><em>${machine.score}点</em></div>`).join('') || '<p>機種別候補はまだ材料不足です。</p>'}
      </div>
    </article>`).join('')}
    ${insufficient.length ? `<details class="desktop-insufficient"><summary>データ不足の店舗 ${insufficient.length}店</summary><div>${insufficient.map(item => `<span>${esc(item.hall_name)}：${esc(item.reason)}</span>`).join('')}</div></details>` : ''}
    <p class="target-search-disclaimer">${esc(data.notice)}</p>`;
}

async function loadDesktopTargetSearch() {
  const dateInput = document.getElementById('desktop-target-date');
  const daysInput = document.getElementById('desktop-target-days');
  const button = document.getElementById('desktop-target-search-button');
  const status = document.getElementById('desktop-target-status');
  if (!dateInput.value) dateInput.value = localDateValue(1);
  button.disabled = true;
  status.textContent = '店舗・曜日・機種データを分析中...';
  try {
    desktopTargetSearchData = await apiFetch(`/api/hall/target_search?visit_date=${encodeURIComponent(dateInput.value)}&days=${encodeURIComponent(daysInput.value)}`);
    renderDesktopTargetSearch(desktopTargetSearchData);
    status.textContent = `${desktopTargetSearchData.generated_at}時点の公開データで分析しました。`;
  } catch (error) {
    desktopTargetSearchData = null;
    document.getElementById('desktop-target-results').innerHTML = `<div class="card desktop-target-empty"><strong>分析できませんでした</strong><span>${esc(error.message)}</span></div>`;
    status.textContent = '分析サーバーとの接続を確認してください。';
  } finally {
    button.disabled = false;
  }
}

document.getElementById('desktop-target-search-form').addEventListener('submit', event => {
  event.preventDefault();
  loadDesktopTargetSearch();
});

document.getElementById('desktop-target-date').value = localDateValue(1);

function safeExternalUrl(raw) {
  try {
    const url = new URL(raw, window.location.href);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
  } catch { return ''; }
}

async function loadDesktopHallOptions() {
  if (!desktopHallOptions.length) {
    try {
      const rows = await apiFetch('/api/scrape/halls');
      desktopHallOptions = (rows || []).filter(row => row.hall_name);
    } catch { /* fallback below */ }
  }
  if (!desktopHallOptions.length) {
    desktopHallOptions = [{ hall_name: 'キコーナ四條畷店' }, { hall_name: 'ひま・わり四條畷店' }];
  }
  const localPriority = new Map([['キコーナ四條畷店', 0], ['ひま・わり四條畷店', 1], ['キコーナ野崎店', 2]]);
  desktopHallOptions.sort((a, b) => {
    const aPriority = localPriority.has(a.hall_name) ? localPriority.get(a.hall_name) : 99;
    const bPriority = localPriority.has(b.hall_name) ? localPriority.get(b.hall_name) : 99;
    return aPriority - bPriority || (b.db_record_count || 0) - (a.db_record_count || 0);
  });
  const html = desktopHallOptions.map(row => `<option value="${esc(row.hall_name)}">${esc(row.hall_name)}</option>`).join('');
  ['desktop-trend-hall', 'desktop-floor-hall'].forEach(id => {
    const select = document.getElementById(id);
    if (!select) return;
    const current = select.value;
    select.innerHTML = html;
    if ([...select.options].some(option => option.value === current)) select.value = current;
  });
}

function desktopProfileBars(rows, labelKey) {
  if (!rows?.length) return '<p class="desktop-analysis-empty">分析できる実績がありません。</p>';
  const max = Math.max(1, ...rows.map(row => Math.abs(Number(row.avg_diff || 0))));
  return `<div class="desktop-profile-bars">${rows.map(row => {
    const value = Number(row.avg_diff || 0);
    const width = Math.max(4, Math.round(Math.abs(value) / max * 100));
    return `<div><span>${esc(row[labelKey])}</span><i><b class="${value >= 0 ? 'profile-up' : 'profile-down'}" style="width:${width}%"></b></i><strong class="${value >= 0 ? 'money-up' : 'money-down'}">${desktopSignedCoins(value)}</strong><small>${row.sample_days || 0}日</small></div>`;
  }).join('')}</div>`;
}

function renderDesktopTrendProfile(aiResult = null) {
  const root = document.getElementById('desktop-trend-results');
  const data = desktopTrendProfileData;
  if (!data || data.status !== '分析済み') {
    root.innerHTML = `<div class="card desktop-analysis-empty"><strong>まだ分析できる実績がありません</strong><span>${esc(data?.notice || '公開データを収集すると自動で表示されます。')}</span></div>`;
    return;
  }
  const aiText = aiResult?.summary || (data.insights || []).map(item => `・${item}`).join('\n');
  const sources = (data.source_urls || []).map((url, index) => {
    const safe = safeExternalUrl(url);
    return safe ? `<a href="${esc(safe)}" target="_blank" rel="noopener">出典${index + 1}</a>` : '';
  }).join('');
  root.innerHTML = `
    <article class="card desktop-profile-hero">
      <div><span class="page-eyebrow">${esc(data.reference_date)}までの実績</span><h2>${esc(data.hall_name)}</h2><p>${data.sample_days}日分・信頼度 ${esc(data.confidence)}</p></div>
      <div class="desktop-profile-score"><small>全体平均</small><b class="${data.overall.avg_diff >= 0 ? 'money-up' : 'money-down'}">${desktopSignedCoins(data.overall.avg_diff)}</b><span>プラス日率 ${data.overall.positive_day_rate}%</span></div>
    </article>
    <article class="card desktop-ai-brief"><div class="card-title card-title-row"><span>分析の要点</span><em>${esc(aiResult?.engine || '統計エンジン')}</em></div><p>${esc(aiText).replace(/\n/g, '<br>')}</p><small>断定ではなく、取得済み実績から見た傾向です。</small></article>
    <div class="desktop-analysis-grid">
      <article class="card"><div class="card-title">曜日別</div>${desktopProfileBars(data.weekday_profile, 'weekday')}</article>
      <article class="card"><div class="card-title">日付末尾別</div>${desktopProfileBars(data.digit_profile, 'digit')}</article>
    </div>
    <article class="card"><div class="card-title card-title-row"><span>直近42日の実績カレンダー</span><small>点数は店舗内の相対評価</small></div><div class="desktop-trend-calendar">${(data.calendar || []).slice(-42).map(day => `<div style="--calendar-score:${Math.max(0, Math.min(100, Number(day.score || 0)))}%"><span>${esc(day.date.slice(5).replace('-', '/'))}</span><b>${day.score}</b><small class="${day.avg_diff >= 0 ? 'money-up' : 'money-down'}">${desktopSignedCoins(day.avg_diff)}</small></div>`).join('')}</div></article>
    <article class="card"><div class="card-title">次の注目日</div><div class="desktop-next-dates">${(data.next_dates || []).map(day => `<div class="${day.score >= 60 ? 'hot' : ''}"><strong>${esc(day.date.slice(5).replace('-', '/'))}</strong><span>${esc(day.weekday)}曜</span><b>${day.score}点</b><small>${esc(day.evidence)}</small></div>`).join('')}</div></article>
    <article class="card"><div class="card-title">扱いが強い機種</div><div class="desktop-profile-machines">${(data.machine_profile || []).slice(0, 12).map((machine, index) => `<div><span>${index + 1}</span><strong>${esc(machine.machine_name)}</strong><small>${machine.sample_days}日・信頼${machine.reliability_pct ?? 0}%・プラス率${machine.positive_rate}%</small><b class="${machine.avg_diff >= 0 ? 'money-up' : 'money-down'}">補正 ${desktopSignedCoins(machine.avg_diff)}</b><em>${machine.score}点</em></div>`).join('')}</div></article>
    <details class="card desktop-analysis-sources"><summary>出典と注意事項</summary><div>${sources || '<span>データ内に出典URLはありません。</span>'}<p>${esc(data.notice)}</p></div></details>`;
}

async function loadDesktopTrendProfile() {
  await loadDesktopHallOptions();
  const hall = document.getElementById('desktop-trend-hall').value;
  const visitDate = document.getElementById('desktop-trend-date').value || localDateValue(1);
  const days = document.getElementById('desktop-trend-days').value;
  const button = document.getElementById('desktop-trend-button');
  const status = document.getElementById('desktop-trend-status');
  button.disabled = true;
  status.textContent = '公開実績と日付パターンを分析中...';
  try {
    const query = new URLSearchParams({ hall_name: hall, visit_date: visitDate, days });
    const [profile, aiResult] = await Promise.all([
      apiFetch(`/api/hall/trend_profile?${query}`),
      apiFetch(`/api/ai/hall_profile?${query}`).catch(() => null),
    ]);
    desktopTrendProfileData = profile;
    renderDesktopTrendProfile(aiResult);
    status.textContent = profile.status === '分析済み' ? `${profile.sample_days}日分・信頼度${profile.confidence}で分析しました。` : profile.notice;
  } catch (error) {
    desktopTrendProfileData = null;
    document.getElementById('desktop-trend-results').innerHTML = `<div class="card desktop-analysis-empty"><strong>分析できませんでした</strong><span>${esc(error.message)}</span></div>`;
    status.textContent = '分析サーバーとの接続を確認してください。';
  } finally { button.disabled = false; }
}

function renderDesktopFloorDetail(seat) {
  const detail = document.getElementById('desktop-floor-detail');
  if (!seat) {
    detail.innerHTML = '<div class="desktop-floor-empty">座席を選ぶと分析根拠を表示します。</div>';
    return;
  }
  detail.innerHTML = `<div class="desktop-seat-detail-head"><div><span>台番号 ${seat.seat_number}</span><h2>${esc(seat.machine_name || '機種未登録')}</h2><small>${esc(seat.island_name || '')}</small></div><div style="--seat-color:${esc(seat.color)}"><b>${seat.score ?? '--'}</b><small>点</small><span>${esc(seat.heat_level)}</span></div></div><div class="desktop-seat-metrics"><div><small>指定日推定</small><strong class="${seat.estimate >= 0 ? 'money-up' : 'money-down'}">${seat.estimate == null ? '--' : desktopSignedCoins(seat.estimate)}</strong></div><div><small>プラス率</small><strong>${seat.positive_rate == null ? '--' : `${seat.positive_rate}%`}</strong></div><div><small>実績</small><strong>${seat.sample_days}日</strong></div><div><small>当日結果</small><strong>${seat.actual ? desktopSignedCoins(seat.actual.diff) : '--'}</strong></div></div><div class="desktop-seat-reasons">${(seat.reasons || []).map(reason => `<span>${esc(reason)}</span>`).join('')}</div>`;
}

function renderDesktopFloorMap() {
  const canvas = document.getElementById('desktop-floor-canvas');
  const meta = document.getElementById('desktop-floor-meta');
  const data = desktopFloorData;
  if (!data?.layout || !data.seats?.length) {
    canvas.innerHTML = '<div class="desktop-floor-empty"><strong>座席マップはまだありません</strong><span>下の登録欄から、出典と台番号範囲を登録できます。</span></div>';
    canvas.style.aspectRatio = 'auto';
    meta.innerHTML = `<span>${esc(data?.status || '未登録')}</span><small>${esc(data?.notice || '')}</small>`;
    renderDesktopFloorDetail(null);
    return;
  }
  const layout = data.layout;
  canvas.style.aspectRatio = `${layout.width} / ${layout.height}`;
  canvas.innerHTML = data.seats.map(seat => `<button type="button" class="desktop-floor-seat" data-desktop-seat="${seat.seat_number}" style="left:${seat.x / layout.width * 100}%;top:${seat.y / layout.height * 100}%;width:${seat.width / layout.width * 100}%;height:${seat.height / layout.height * 100}%;--seat-color:${esc(seat.color)}"><b>${seat.seat_number}</b><span>${seat.score ?? '--'}</span></button>`).join('');
  const source = safeExternalUrl(layout.source_url);
  meta.innerHTML = `<span>${esc(layout.floor_name)}・${esc(layout.verification_status)}</span><small>${esc(layout.source_label || '利用者登録マップ')}・台番号実績 ${data.data_coverage.seat_count}台</small>${source ? `<a href="${esc(source)}" target="_blank" rel="noopener">出典を開く</a>` : ''}`;
  canvas.querySelectorAll('[data-desktop-seat]').forEach(button => button.addEventListener('click', () => renderDesktopFloorDetail(data.seats.find(seat => seat.seat_number === Number(button.dataset.desktopSeat)))));
  renderDesktopFloorDetail(data.seats[0]);
}

function syncDesktopFloorEditor() {
  const layout = desktopFloorData?.layout;
  if (!layout) return;
  document.getElementById('desktop-layout-valid-from').value = layout.valid_from || localDateValue();
  document.getElementById('desktop-layout-source-label').value = layout.source_label || '';
  document.getElementById('desktop-layout-source-url').value = layout.source_url || '';
  document.getElementById('desktop-layout-verification').value = layout.verification_status || '未確認';
  document.getElementById('desktop-layout-notes').value = layout.notes || '';
  desktopFloorEditorSeats = (desktopFloorData.seats || []).map(seat => ({ seat_number: seat.seat_number, machine_name: seat.machine_name || '', island_name: seat.island_name || '', x: seat.x, y: seat.y, width: seat.width, height: seat.height, rotation: seat.rotation || 0 }));
}

async function loadDesktopFloorHeat() {
  await loadDesktopHallOptions();
  const hall = document.getElementById('desktop-floor-hall').value;
  const visitDate = document.getElementById('desktop-floor-date').value || localDateValue(1);
  const days = document.getElementById('desktop-floor-days').value;
  const button = document.getElementById('desktop-floor-button');
  const status = document.getElementById('desktop-floor-status');
  button.disabled = true;
  status.textContent = '店内レイアウトと台番号実績を照合中...';
  try {
    desktopFloorData = await apiFetch(`/api/layouts/seat_heat?hall_name=${encodeURIComponent(hall)}&visit_date=${encodeURIComponent(visitDate)}&days=${encodeURIComponent(days)}`);
    syncDesktopFloorEditor();
    renderDesktopFloorMap();
    status.textContent = `${desktopFloorData.status}・${desktopFloorData.data_coverage.history_rows}件の台番号実績を使用`;
  } catch (error) {
    desktopFloorData = null;
    document.getElementById('desktop-floor-canvas').innerHTML = `<div class="desktop-floor-empty"><strong>表示できませんでした</strong><span>${esc(error.message)}</span></div>`;
    status.textContent = '分析サーバーとの接続を確認してください。';
  } finally { button.disabled = false; }
}

function createDesktopAutoLayout() {
  const start = Number(document.getElementById('desktop-layout-seat-start').value);
  const end = Number(document.getElementById('desktop-layout-seat-end').value);
  if (!start || !end || end < start) return showToast('正しい開始・終了台番号を入力してください', 'error');
  if (end - start + 1 > 300) return showToast('一度に登録できるのは300台までです', 'error');
  const island = document.getElementById('desktop-layout-island').value.trim() || 'スロット島';
  const numbers = Array.from({ length: end - start + 1 }, (_, index) => start + index);
  desktopFloorEditorSeats = numbers.map((seatNumber, index) => ({ seat_number: seatNumber, machine_name: island, island_name: island, x: 55 + (index % 14) * 64, y: 70 + Math.floor(index / 14) * 100, width: 52, height: 44, rotation: 0 }));
  const height = Math.max(420, 170 + Math.ceil(numbers.length / 14) * 100);
  desktopFloorData = { hall_name: document.getElementById('desktop-floor-hall').value, status: '仮配置', notice: '公式情報または現地で位置を確認してください。', data_coverage: { history_rows: 0, seat_count: 0 }, layout: { id: null, hall_name: document.getElementById('desktop-floor-hall').value, floor_name: 'スロットフロア', valid_from: document.getElementById('desktop-layout-valid-from').value || localDateValue(), width: 1000, height, source_url: document.getElementById('desktop-layout-source-url').value, source_label: document.getElementById('desktop-layout-source-label').value || '利用者登録マップ', verification_status: document.getElementById('desktop-layout-verification').value, notes: '仮配置です。実際の位置を確認してください。' }, seats: desktopFloorEditorSeats.map(seat => ({ ...seat, score: null, color: '#64748b', heat_level: 'データ不足', reasons: ['配置確認待ち'], sample_days: 0 })) };
  renderDesktopFloorMap();
  showToast(`${numbers.length}台を仮配置しました`, 'success');
}

async function saveDesktopFloorLayout(event) {
  event.preventDefault();
  if (!desktopFloorEditorSeats.length) return showToast('先に台番号を仮配置してください', 'error');
  const sourceUrl = document.getElementById('desktop-layout-source-url').value.trim();
  const sourceLabel = document.getElementById('desktop-layout-source-label').value.trim();
  const body = {
    hall_name: document.getElementById('desktop-floor-hall').value,
    floor_name: 'スロットフロア',
    valid_from: document.getElementById('desktop-layout-valid-from').value || localDateValue(),
    width: desktopFloorData?.layout?.width || 1000,
    height: desktopFloorData?.layout?.height || 700,
    source_url: sourceUrl,
    source_label: sourceLabel || (sourceUrl ? '公開店内マップ' : '利用者登録マップ'),
    source_kind: sourceUrl ? 'official' : 'manual',
    verification_status: document.getElementById('desktop-layout-verification').value,
    notes: document.getElementById('desktop-layout-notes').value.trim(),
    seats: desktopFloorEditorSeats,
  };
  const button = document.getElementById('desktop-layout-save');
  button.disabled = true;
  try {
    await apiFetch('/api/layouts', { method: 'POST', body: JSON.stringify(body) });
    showToast('店内マップを保存しました', 'success');
    await loadDesktopFloorHeat();
  } catch (error) { showToast(error.message, 'error'); }
  finally { button.disabled = false; }
}

function parseSeatResultCsv(text) {
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
  const indexOf = key => headers.findIndex(header => aliases[key].includes(header));
  const indexes = Object.fromEntries(Object.keys(aliases).map(key => [key, indexOf(key)]));
  if (indexes.seat_number < 0 || indexes.diff_coins < 0) throw new Error('「台番号」と「差枚」列が必要です');
  return lines.slice(1).map((line, lineIndex) => {
    const cols = parseLine(line);
    const seat = Number(cols[indexes.seat_number]);
    const diff = Number(String(cols[indexes.diff_coins]).replace(/[+,枚]/g, ''));
    const games = indexes.games >= 0 && cols[indexes.games] !== '' ? Number(String(cols[indexes.games]).replace(/[,Gg]/g, '')) : null;
    if (!Number.isInteger(seat) || !Number.isFinite(diff) || (games != null && !Number.isFinite(games))) throw new Error(`${lineIndex + 2}行目の数値を確認してください`);
    return { seat_number: seat, machine_name: indexes.machine_name >= 0 ? cols[indexes.machine_name] : '', diff_coins: diff, games };
  });
}

async function saveDesktopSeatResults(rows, sourceLabel) {
  const status = document.getElementById('desktop-seat-result-status');
  const body = {
    hall_name: document.getElementById('desktop-floor-hall').value,
    report_date: document.getElementById('desktop-seat-result-date').value || localDateValue(),
    source_label: sourceLabel || document.getElementById('desktop-seat-result-source').value.trim() || '現地入力',
    rows,
  };
  status.textContent = `${rows.length}件を保存中...`;
  const result = await apiFetch('/api/layouts/seat_results', { method: 'POST', body: JSON.stringify(body) });
  status.textContent = `${result.report_date}・${result.message}`;
  showToast(result.message, 'success');
  await loadDesktopFloorHeat();
}

document.getElementById('desktop-seat-result-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.getElementById('desktop-seat-result-save');
  button.disabled = true;
  try {
    await saveDesktopSeatResults([{
      seat_number: Number(document.getElementById('desktop-seat-result-number').value),
      machine_name: document.getElementById('desktop-seat-result-machine').value.trim(),
      diff_coins: Number(document.getElementById('desktop-seat-result-diff').value),
      games: document.getElementById('desktop-seat-result-games').value === '' ? null : Number(document.getElementById('desktop-seat-result-games').value),
    }]);
    ['desktop-seat-result-number', 'desktop-seat-result-machine', 'desktop-seat-result-diff', 'desktop-seat-result-games'].forEach(id => { document.getElementById(id).value = ''; });
  } catch (error) { showToast(error.message, 'error'); }
  finally { button.disabled = false; }
});

document.getElementById('desktop-seat-result-csv').addEventListener('change', async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  try { await saveDesktopSeatResults(parseSeatResultCsv(await file.text()), `CSV: ${file.name}`); }
  catch (error) { showToast(error.message, 'error'); }
  finally { event.target.value = ''; }
});

document.getElementById('desktop-seat-result-template').addEventListener('click', () => {
  const blob = new Blob(['台番号,機種名,差枚,G数\n501,スマスロ北斗の拳,1800,7200\n'], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob); const anchor = document.createElement('a');
  anchor.href = url; anchor.download = 'seat-results-template.csv'; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

document.getElementById('desktop-trend-form').addEventListener('submit', event => { event.preventDefault(); loadDesktopTrendProfile(); });
document.getElementById('desktop-floor-form').addEventListener('submit', event => { event.preventDefault(); loadDesktopFloorHeat(); });
document.getElementById('desktop-layout-auto').addEventListener('click', createDesktopAutoLayout);
document.getElementById('desktop-layout-form').addEventListener('submit', saveDesktopFloorLayout);
document.getElementById('desktop-trend-date').value = localDateValue(1);
document.getElementById('desktop-floor-date').value = localDateValue(1);
document.getElementById('desktop-layout-valid-from').value = localDateValue();
document.getElementById('desktop-seat-result-date').value = localDateValue();

document.querySelectorAll('.hall-sub-btn').forEach(btn => {
  btn.addEventListener('click', () => switchHallTab(btn.dataset.htab));
});

// 折りたたみカード（イベント委譲で動的追加要素にも対応）
document.addEventListener('click', e => {
  const hd = e.target.closest('.card-col-hd');
  if (hd) hd.closest('.card-col').classList.toggle('closed');
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
// 今日の注目情報（推測ページ先頭カード）
// ---------------------------------------------------------------------------
async function loadWeeklyHighlight() {
  const card = document.getElementById('weekly-highlight-card');
  const body = document.getElementById('weekly-highlight-body');
  const gen = document.getElementById('weekly-generated');
  if (!card || !body) return;
  try {
    const [data, hotMachines] = await Promise.all([
      apiFetch('/api/hall/weekly_summary?days=7').catch(() => null),
      apiFetch('/api/hall/hot_machines?days=14&limit=5').catch(() => []),
    ]);
    const hasWeekly = data && (data.highlights?.length || data.top_halls?.length);
    const hasHot = hotMachines?.length > 0;
    if (!hasWeekly && !hasHot) { card.style.display = 'none'; return; }
    card.style.display = 'block';
    if (gen) gen.textContent = data?.generated_at || '';
    let html = '';
    if (hotMachines?.length) {
      html += `<div style="font-size:.65rem;color:var(--text3);margin-bottom:5px;font-weight:600;letter-spacing:.04em">⚡ 急上昇機種 (直近3日 vs 前週)</div>`;
      html += hotMachines.slice(0, 5).map(h => {
        const sign = h.surge >= 0 ? '+' : '';
        const col = h.surge > 0 ? 'var(--success)' : 'var(--danger)';
        return `<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;padding:3px 0;border-bottom:1px solid rgba(255,255,255,.04)">
          <div style="flex:1;min-width:0">
            <div style="font-size:.74rem;font-weight:700;color:var(--text1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(h.machine_name)}</div>
            <div style="font-size:.6rem;color:var(--text3)">${esc(h.hall_name)} · ${h.days_count}日</div>
          </div>
          <div style="text-align:right;flex-shrink:0">
            <div style="font-size:.72rem;font-weight:700;color:${col}">${sign}${h.surge}枚</div>
            <div style="font-size:.58rem;color:var(--text3)">${h.recent_avg >= 0 ? '+' : ''}${h.recent_avg} / ${h.base_avg >= 0 ? '+' : ''}${h.base_avg}</div>
          </div>
        </div>`;
      }).join('');
    }
    if (data?.top_halls?.length) {
      html += `<div style="margin-top:6px;font-size:.62rem;color:var(--text3);padding-top:5px;border-top:1px solid rgba(255,255,255,.06)">
        週間トップ: ${data.top_halls.slice(0,3).map(h =>
          `<span style="color:var(--text2)">${esc(h.hall_name)} <strong style="color:${h.avg_diff>=0?'var(--success)':'var(--danger)'}">${h.avg_diff>=0?'+':''}${h.avg_diff}</strong></span>`
        ).join(' / ')}
      </div>`;
    }
    body.innerHTML = html || '<span style="color:var(--text3);font-size:.75rem">データなし</span>';
  } catch(e) {
    const c = document.getElementById('weekly-highlight-card');
    if (c) c.style.display = 'none';
  }
}

async function loadTodayHotCard() {
  const card = document.getElementById('today-hot-card');
  const body = document.getElementById('today-hot-body');
  if (!card || !body) return;
  try {
    const today = new Date().toISOString().slice(0, 10);
    const [evData, hotData] = await Promise.all([
      fetch(`/api/events/day?date_str=${today}`).then(r => r.json()).catch(() => ({events:[]})),
      fetch('/api/hall/hot_days?months=2').then(r => r.json()).catch(() => ({hot_days:[]})),
    ]);
    const todayEvents = evData.events || [];
    const todayHotHalls = (hotData.hot_days || []).filter(h => h.date === today);
    todayHotHalls.sort((a, b) => b.z_score - a.z_score);

    if (todayEvents.length === 0 && todayHotHalls.length === 0) {
      card.style.display = 'none';
      return;
    }
    card.style.display = 'block';
    let html = '';
    if (todayHotHalls.length > 0) {
      html += `<div style="margin-bottom:6px"><span style="color:var(--warning);font-weight:700">🔥 今日のデータ傾向: </span>`;
      html += todayHotHalls.slice(0, 3).map(h =>
        `<span style="margin-right:8px">${esc(h.hall_name)} <strong style="color:${h.z_score>=1.5?'var(--success)':'var(--warning)'}">${h.label}</strong>(${h.avg_diff}枚)</span>`
      ).join('');
      html += '</div>';
    }
    if (todayEvents.length > 0) {
      html += `<div><span style="color:var(--primary-h);font-weight:700">📌 今日のイベント: </span>`;
      html += todayEvents.slice(0, 4).map(e =>
        `<span class="ev-badge ev-badge-${e.event_type}" style="margin-right:4px">${esc(e.hall_name)} ${e.event_type}</span>`
      ).join('');
      html += '</div>';
    }
    body.innerHTML = html;
  } catch(e) {
    card.style.display = 'none';
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
const estReadyText = document.getElementById('est-ready-text');
const estReadyBar = document.getElementById('est-ready-bar');

function getEstimateReadiness() {
  const machineReady = Boolean(state.currentMachine && state.currentProfile);
  const games = Number.parseInt(estGames.value, 10) || 0;
  const startedFrom = Number.parseInt(document.getElementById('est-started-from')?.value, 10) || 0;
  const observedGames = games - startedFrom;

  if (!machineReady) return { ready: false, progress: 0, text: 'まず機種を選択してください' };
  if (games <= 0) return { ready: false, progress: 50, text: '総ゲーム数を入力してください' };
  if (startedFrom > games) return { ready: false, progress: 65, text: '引き継ぎG数が総G数を超えています' };

  const invalidCount = [...estCountsList.querySelectorAll('.count-input')].find(input => {
    const value = Number.parseInt(input.value, 10) || 0;
    return value < 0 || value > observedGames;
  });
  if (invalidCount) {
    return { ready: false, progress: 80, text: `${invalidCount.dataset.el}の回数を確認してください` };
  }

  const entered = [...estCountsList.querySelectorAll('.count-input')]
    .filter(input => (Number.parseInt(input.value, 10) || 0) > 0).length;
  const suffix = entered ? `・カウント${entered}項目` : '・事前分布のみ';
  return {
    ready: true,
    progress: 100,
    text: `${state.currentMachine}・実観測${observedGames.toLocaleString()}G${suffix}`,
  };
}

function updateEstimateReadiness() {
  const status = getEstimateReadiness();
  estRunBtn.disabled = !status.ready;
  if (estReadyText) {
    estReadyText.textContent = status.text;
    estReadyText.style.color = status.ready ? 'var(--success)' :
      status.progress >= 65 ? 'var(--warning)' : 'var(--text2)';
  }
  if (estReadyBar) estReadyBar.style.width = `${status.progress}%`;

  const steps = document.querySelectorAll('.flow-step');
  steps.forEach((step, index) => {
    const active = index === 0 ? !state.currentMachine : index === 1 ? !status.ready : status.ready;
    step.classList.toggle('active', active);
  });
  return status;
}

async function loadMachineSelect() {
  try {
    state.machines = await api.getMachines();
    const dl = document.getElementById('est-machine-datalist');
    if (dl) {
      dl.innerHTML = state.machines.map(m => `<option value="${esc(m)}">`).join('');
    }
  } catch (e) {
    showToast('機種一覧の取得に失敗: ' + e.message, 'error');
  }
}

async function loadMachineSnapshot(machineName) {
  const snap = document.getElementById('machine-snapshot');
  if (!snap) return;
  if (!machineName) { snap.style.display = 'none'; return; }
  try {
    const hall = estHall?.value || '';
    const d = await apiFetch(`/api/machine/stats?machine_name=${encodeURIComponent(machineName)}`);
    if (!d || !d.total_sessions) { snap.style.display = 'none'; return; }

    const diffColor = (d.diff_yen || 0) >= 0 ? 'var(--success)' : 'var(--danger)';
    const sign = v => v >= 0 ? `+${v?.toLocaleString()}` : v?.toLocaleString();
    const pinned = isPinnedSeat(hall, machineName, null);

    // 最近のピン済み台を確認
    const pinnedThisMachine = getPinnedSeats().filter(p => p.machine === machineName && (!hall || p.hall === hall));
    const pinnedHint = pinnedThisMachine.length > 0
      ? `<span style="color:var(--primary-h);margin-left:8px">★ ${pinnedThisMachine.map(p=>p.seat+'番台').join(', ')} ピン中</span>`
      : '';

    snap.innerHTML = `
      <div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap">
        <div><span style="color:var(--text3)">実績 </span><strong>${d.total_sessions}回</strong></div>
        <div><span style="color:var(--text3)">収支 </span><strong style="color:${diffColor}">${sign(d.diff_yen)}円</strong></div>
        <div><span style="color:var(--text3)">勝率 </span><strong>${Math.round((d.win_rate||0)*100)}%</strong></div>
        ${d.avg_estimated_setting ? `<div><span style="color:var(--text3)">平均推定設定 </span><strong style="color:var(--primary-h)">${d.avg_estimated_setting}</strong></div>` : ''}
        ${pinnedHint}
      </div>
      ${d.recent_sessions?.length ? `<div style="margin-top:4px;font-size:.68rem;color:var(--text3)">直近: ${d.recent_sessions.slice(0,3).map(s=>`${s.date} ${s.diff_coins>=0?'+':''}${s.diff_coins}枚`).join(' / ')}</div>` : ''}`;
    snap.style.display = 'block';
  } catch(e) {
    snap.style.display = 'none';
  }
}

const _onMachineChange = () => {
  const v = estMachine.value.trim();
  if (state.machines && state.machines.includes(v)) {
    loadMachineSnapshot(v);
    estMachine.dispatchEvent(Object.assign(new Event('_machine_valid'), { detail: v }));
  }
};
estMachine?.addEventListener('change', _onMachineChange);
estMachine?.addEventListener('input', _onMachineChange);

// 台番入力 → 末尾傾向ヒント + 台別過去成績
document.getElementById('est-seat-number')?.addEventListener('input', async function() {
  const hint = document.getElementById('seat-tail-hint');
  if (!hint) return;
  const seatNum = parseInt(this.value);
  if (!seatNum || seatNum < 1) { hint.style.display = 'none'; return; }
  const hall = estHall?.value;
  if (!hall) { hint.style.display = 'none'; return; }
  const tail = seatNum % 10;
  const machine = state.currentMachine;
  const sign = v => v >= 0 ? `+${v}` : `${v}`;

  // 末尾傾向 + 台別詳細を並列取得
  const [tailArr, seatDetail] = await Promise.all([
    apiFetch(`/api/hall/tail_bb_analysis?hall_name=${encodeURIComponent(hall)}&days=180`).catch(() => null),
    (machine && hall)
      ? apiFetch(`/api/hall/seat_detail?hall_name=${encodeURIComponent(hall)}&machine_name=${encodeURIComponent(machine)}&seat_number=${seatNum}&days=90`).catch(() => null)
      : Promise.resolve(null),
  ]);

  let html = '';

  // 末尾ヒント
  if (Array.isArray(tailArr) && tailArr.length) {
    const tailData = tailArr.find(t => t.tail === tail);
    if (tailData) {
      const z = tailData.z_score;
      const col = z >= 0.5 ? 'var(--success)' : z <= -0.5 ? 'var(--danger)' : 'var(--text3)';
      const arrow = z >= 0.5 ? '▲' : z <= -0.5 ? '▼' : '─';
      html += `<div>末尾<strong>${tail}</strong>: <span style="color:${col};font-weight:700">${arrow} z=${sign(z)}σ</span>
        <span style="color:var(--text3);margin-left:6px">BB平均${tailData.avg_bb}回 / ${sign(tailData.avg_diff)}枚</span></div>`;
    }
  }

  // 台別過去成績
  if (seatDetail && seatDetail.total_days >= 2) {
    const sd = seatDetail;
    const diffCol = sd.avg_diff >= 0 ? 'var(--success)' : 'var(--danger)';
    const wrCol = sd.win_rate >= 50 ? 'var(--success)' : 'var(--danger)';
    let badges = '';
    if (sd.win_streak >= 2) badges += `<span style="color:var(--success);font-size:.63rem"> 🔥${sd.win_streak}連勝</span>`;
    if (sd.best_weekday) badges += `<span style="color:var(--primary-h);font-size:.63rem"> ${sd.best_weekday.weekday}曜強</span>`;
    html += `<div style="margin-top:3px;font-size:.7rem">
      <span style="color:var(--text3)">この台${sd.total_days}日 </span>
      <span style="color:${diffCol};font-weight:700">${sign(sd.avg_diff)}枚</span>
      <span style="color:var(--text3);margin-left:4px">勝率<span style="color:${wrCol}">${sd.win_rate}%</span></span>
      ${badges}
    </div>`;
  }

  if (html) {
    hint.innerHTML = html;
    hint.style.display = 'block';
  } else {
    hint.style.display = 'none';
  }
});

let _machineInputTimer;
estMachine.addEventListener('input', () => {
  clearTimeout(_machineInputTimer);
  const name = estMachine.value.trim();
  // datalist はモバイルブラウザによって change の発火タイミングが異なるため、
  // 有効な完全一致候補は input からも確定する。
  if (name && state.machines.includes(name) && name !== state.currentMachine) {
    _machineInputTimer = setTimeout(() => estMachine.dispatchEvent(new Event('change')), 120);
  }
});

estMachine.addEventListener('change', async () => {
  const name = estMachine.value.trim();
  if (name && state.machines && !state.machines.includes(name)) return; // 無効な入力は無視
  state.currentMachine = name;
  state.estimateHistory = [];
  state.minSetting = null;  // 確定演出リセット
  if (!name) {
    estCountsList.innerHTML = '<p class="hint">機種を選択するとカウント入力欄が表示されます</p>';
    estResetBtn.style.display = 'none';
    state.currentProfile = null;
    document.getElementById('est-confirm-section').style.display = 'none';
    updateEstimateReadiness();
    return;
  }
  try {
    state.currentProfile = await api.getMachine(name);
    renderCountInputs(state.currentProfile);
    estResetBtn.style.display = 'block';
    document.getElementById('est-confirm-section').style.display = 'block';
    updateConfirmBadge();
    // 変更検知フォームも更新
    if (cdSection) cdSection.style.display = 'block';
    renderCdCounts(state.currentProfile);
    // 保存したドラフトを復元
    restoreDraft(name);
    updateDerivedCountState(null);
    // 直近セッション表示
    loadRecentSessions(name);
    // 事前分布品質バッジ更新
    loadPriorQuality();
    // 機械割目安テーブル表示
    renderKwHint(state.currentProfile);
    updateEstimateReadiness();
  } catch (e) {
    state.currentProfile = null;
    updateEstimateReadiness();
    showToast('機種データ取得失敗: ' + e.message, 'error');
  }
});

function renderKwHint(profile) {
  let el = document.getElementById('est-kw-hint');
  if (!el) {
    el = document.createElement('div');
    el.id = 'est-kw-hint';
    el.style.cssText = 'margin-top:8px;font-size:.7rem;color:var(--text3);display:none';
    const estResult2 = document.getElementById('est-result');
    if (estResult2) estResult2.parentNode.insertBefore(el, estResult2);
  }
  if (!profile || !profile.machine_kw) { el.style.display = 'none'; return; }
  const kw = profile.machine_kw;
  const settings = profile.settings || Object.keys(kw).sort((a,b) => +a - +b);
  const setColors = {'1':'var(--s1)','2':'var(--s2)','3':'var(--s3)','4':'var(--s4)','5':'var(--s5)','6':'var(--s6)'};
  const gameSamples = [1000, 3000, 6000, 10000];
  const rows = gameSamples.map(g => {
    const cells = settings.map(s => {
      const rate = kw[s];
      const diff = Math.round(g * (rate - 1) * 3);
      const col = diff > 0 ? 'var(--success)' : diff < 0 ? 'var(--danger)' : 'var(--text2)';
      return `<td style="text-align:right;padding:2px 5px;font-size:.65rem;color:${col}">${diff > 0 ? '+' : ''}${diff}</td>`;
    }).join('');
    return `<tr><td style="padding:2px 5px;color:var(--text3);font-size:.65rem;white-space:nowrap">${g.toLocaleString()}G</td>${cells}</tr>`;
  }).join('');
  const headers = settings.map(s =>
    `<th style="padding:2px 5px;text-align:right;font-size:.63rem;color:${setColors[s]||'var(--text2)'}">設定${s}</th>`
  ).join('');
  el.innerHTML = `<div style="font-size:.65rem;color:var(--text3);margin-bottom:3px">1コイン${parseFloat(document.getElementById('denom-select')?.value||1)}円換算・差枚目安（機械割から計算）</div>
    <table style="border-collapse:collapse"><thead><tr><th style="padding:2px 5px;font-size:.63rem"></th>${headers}</tr></thead><tbody>${rows}</tbody></table>`;
  el.style.display = 'block';
}

async function loadPriorQuality() {
  const hall = estHall.value;
  const machine = state.currentMachine;
  if (!hall || !machine) return;

  // インジケーターの挿入先: est-hall の親要素の直後
  let badge = document.getElementById('prior-quality-badge');
  if (!badge) {
    badge = document.createElement('div');
    badge.id = 'prior-quality-badge';
    badge.style.cssText = 'font-size:.72rem;padding:4px 10px;border-radius:6px;margin-top:4px;display:none';
    const hallRow = estHall.closest('.form-row') || estHall.parentElement;
    if (hallRow) hallRow.appendChild(badge);
  }

  try {
    const q = await apiFetch(
      `/api/hall/prior_quality?hall_name=${encodeURIComponent(hall)}&machine_name=${encodeURIComponent(machine)}`
    );
    if (!q || q.records === 0) {
      badge.style.display = 'none';
      return;
    }
    const col = q.quality_score >= 75 ? 'var(--success)' : q.quality_score >= 50 ? 'var(--warning)' : 'var(--danger)';
    const bg  = q.quality_score >= 75 ? 'rgba(16,185,129,.1)' : q.quality_score >= 50 ? 'rgba(251,191,36,.1)' : 'rgba(244,63,94,.1)';
    badge.style.display = 'block';
    badge.style.background = bg;
    badge.style.border = `1px solid ${col}`;
    badge.style.color = col;
    badge.innerHTML = `事前分布品質: <strong>${q.quality_label}</strong> (${q.quality_score}/100) — ${q.records}件 BB${q.bb_coverage}%${q.theory_match ? ' 理論値あり' : ' 理論値なし'} 最終${q.last_date || '?'}`;
  } catch(e) {
    badge.style.display = 'none';
  }
}

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
      <span class="count-name">${esc(el.name)}${el.name.includes('合算') ? '<small>BB/RB入力時は自動除外</small>' : ''}</span>
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
      updateDerivedCountState(input);
      saveDraft();
      updateEstimateReadiness();
      autoEstimate();
    });
  });

  // 直接入力
  estCountsList.querySelectorAll('.count-input').forEach(input => {
    input.addEventListener('input', () => {
      updateDerivedCountState(input);
      saveDraft();
      updateEstimateReadiness();
      autoEstimate();
    });
  });
}

function updateDerivedCountState(changedInput) {
  const combined = [...estCountsList.querySelectorAll('.count-input')]
    .find(input => input.dataset.el?.includes('合算'));
  if (!combined) return;
  const components = [...estCountsList.querySelectorAll('.count-input')]
    .filter(input => input !== combined && /BB|RB|BIG|REG/.test(input.dataset.el || ''));
  const hasComponentCounts = components.some(input => (Number.parseInt(input.value, 10) || 0) > 0);
  if (hasComponentCounts && changedInput !== combined) combined.value = 0;
  combined.disabled = hasComponentCounts;
  combined.closest('.count-row')?.classList.toggle('count-row-muted', hasComponentCounts);
}

// ゲーム数変更でも自動推測
estGames.addEventListener('input', () => { saveDraft(); updateEstimateReadiness(); autoEstimate(); });
document.getElementById('est-started-from')?.addEventListener('input', () => {
  updateEstimateReadiness();
  autoEstimate();
});
estHall.addEventListener('change', () => {
  state.currentHall = estHall.value;
  autoEstimate();
  if (state.currentMachine) loadPriorQuality();
});
estWeekday.addEventListener('change', autoEstimate);
estDom.addEventListener('input', autoEstimate);
estEvent.addEventListener('change', autoEstimate);

let _autoTimer;
let _estimateRequestId = 0;
function autoEstimate() {
  clearTimeout(_autoTimer);
  if (!getEstimateReadiness().ready) return;
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
  const readiness = updateEstimateReadiness();
  if (!readiness.ready) return;
  const counts = getCountValues();
  const games = parseInt(estGames.value) || 0;
  const startedFrom = parseInt(document.getElementById('est-started-from')?.value) || 0;
  const seatNum = parseInt(document.getElementById('est-seat-number')?.value) || null;
  const weekday = estWeekday.value !== '' ? parseInt(estWeekday.value) : null;
  const dom = estDom.value ? parseInt(estDom.value) : null;
  const requestId = ++_estimateRequestId;

  const originalText = estRunBtn.textContent;
  estRunBtn.disabled = true;
  estRunBtn.textContent = '推定中…';
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
      ...(seatNum ? { seat_number: seatNum } : {}),
    });
    if (requestId !== _estimateRequestId) return;
    state.lastEstimate = { result, machine: state.currentMachine, games, startedFrom, counts };
    // 推測履歴を積む（最大20件）
    if (result.expected_setting) {
      state.estimateHistory.push({ games, expected: result.expected_setting, confidence: result.confidence || 0 });
      if (state.estimateHistory.length > 20) state.estimateHistory.shift();
    }
    renderEstimateResult(result);
  } catch (e) {
    if (requestId === _estimateRequestId) showToast('推測エラー: ' + e.message, 'error');
  } finally {
    if (requestId === _estimateRequestId) {
      estRunBtn.textContent = originalText;
      updateEstimateReadiness();
    }
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

  expectedEl.textContent = r.expected_setting != null ? r.expected_setting.toFixed(2) : '--';

  // 90%信用区間をサブテキストで表示
  let ciEl = document.getElementById('res-credible-interval');
  if (!ciEl) {
    ciEl = document.createElement('div');
    ciEl.id = 'res-credible-interval';
    ciEl.style.cssText = 'font-size:.7rem;color:var(--text3);margin-top:2px';
    expectedEl.parentNode.appendChild(ciEl);
  }
  if (r.credible_interval && r.credible_interval[0] != null && r.credible_interval[1] != null) {
    ciEl.textContent = `90%信用区間: 設定${r.credible_interval[0].toFixed(0)}〜${r.credible_interval[1].toFixed(0)}`;
  }

  // 高設定シグナル数サマリー
  {
    let signalEl = document.getElementById('res-signal-summary');
    if (!signalEl) {
      signalEl = document.createElement('div');
      signalEl.id = 'res-signal-summary';
      signalEl.style.cssText = 'margin-top:8px;padding:8px 10px;background:rgba(255,255,255,.03);border-radius:7px;border:1px solid var(--border)';
      barsEl.parentNode.insertBefore(signalEl, barsEl.nextSibling);
    }
    const signals = [];
    // 高設定確率50%以上
    if (r.high_setting_prob >= 0.5)
      signals.push({ icon: '◎', label: `高設定確率${Math.round(r.high_setting_prob*100)}%`, positive: true });
    else if (r.high_setting_prob >= 0.3)
      signals.push({ icon: '△', label: `高設定確率${Math.round(r.high_setting_prob*100)}%`, positive: false });

    // BB方向チェック
    if (r.element_analysis) {
      const bbEl = r.element_analysis.find(e => /BB|BIG|ボーナス合算|AT初当/.test(e.name) && e.observed > 0);
      const rbEl = r.element_analysis.find(e => /RB|REG|単独RB/.test(e.name) && e.observed > 0);
      if (bbEl) signals.push({ icon: bbEl.direction === 'up' ? '◎' : '✕', label: `${bbEl.name} ${bbEl.direction === 'up' ? '↑高め(設'+bbEl.closest_setting+')' : '↓低め'}`, positive: bbEl.direction === 'up' });
      if (rbEl) signals.push({ icon: rbEl.direction === 'up' ? '◎' : '✕', label: `${rbEl.name} ${rbEl.direction === 'up' ? '↑' : '↓'}`, positive: rbEl.direction === 'up' });
    }

    // 信用区間が狭い（高確度）
    if (r.credible_interval) {
      const span = r.credible_interval[1] - r.credible_interval[0];
      if (span <= 2) signals.push({ icon: '◎', label: `信用区間±${(span/2).toFixed(1)}（高精度）`, positive: true });
    }

    // ゲーム数
    const games = parseInt(estGames.value) || 0;
    if (games >= 3000) signals.push({ icon: '◎', label: `${games.toLocaleString()}G（充分なサンプル）`, positive: true });
    else if (games < 1000) signals.push({ icon: '▲', label: `${games}G（サンプル少）`, positive: false });

    const positives = signals.filter(s => s.positive).length;
    const total = signals.length;
    const strengthColor = positives >= 3 ? 'var(--success)' : positives >= 2 ? 'var(--warning)' : 'var(--text3)';
    if (total > 0) {
      signalEl.innerHTML = `<div style="font-size:.65rem;color:var(--text3);margin-bottom:5px;text-transform:uppercase;letter-spacing:.06em">高設定シグナル <span style="color:${strengthColor};font-weight:800">${positives}/${total}</span></div>
        <div style="display:flex;flex-wrap:wrap;gap:4px">${signals.map(s =>
          `<span style="font-size:.66rem;padding:2px 7px;border-radius:4px;background:${s.positive?'rgba(16,185,129,.12)':'rgba(255,255,255,.04)'};color:${s.positive?'var(--success)':'var(--text3)'}">${s.icon} ${s.label}</span>`
        ).join('')}</div>`;
      signalEl.style.display = 'block';
    } else {
      signalEl.style.display = 'none';
    }
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
      ? `<div style="color:#fb923c;font-size:.7rem;margin-top:7px;line-height:1.45">相関の強い組み合わせが${r.correlated_elements.length}件あります。合算値と内訳の重複入力に注意してください。</div>`
      : '';
    powerEl.style.display = 'block';
    powerEl.innerHTML = `<div style="font-size:.68rem;color:var(--text3);margin-bottom:5px;font-weight:600;text-transform:uppercase;letter-spacing:.06em">要素別識別力</div>
      ${sorted.slice(0, 3).map(([name, pow]) => {
        const pct = Math.round(pow / maxPow * 100);
        const col = pct >= 70 ? 'var(--success)' : pct >= 40 ? 'var(--warning)' : 'var(--text3)';
        return `<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">
          <div style="font-size:.72rem;color:var(--text2);width:120px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(name)}</div>
          <div style="flex:1;height:4px;background:var(--bg3);border-radius:2px">
            <div class="anim-bar" style="width:${pct}%;height:100%;background:${col};border-radius:2px"></div>
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
  setTimeout(() => estResult.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100);
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
        credible_interval: r.credible_interval || null,
        element_powers: r.element_powers || null,
        correlated_elements: r.correlated_elements || null,
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

  // 残り期待収益計算（denomSel value × 3コイン × EVで損益）
  const denomYen = parseFloat(document.getElementById('denom-select')?.value || '1');
  const costPer1000G = 3 * denomYen * 1000;
  const profitPer1000G = Math.round(costPer1000G * (ev - 1));
  const profitSign = profitPer1000G >= 0 ? '+' : '';
  const evDetail = `EV ${r.ev_pct?.toFixed(1)}% / 高設定${(highProb*100).toFixed(0)}% / 1000Gあたり${profitSign}${profitPer1000G.toLocaleString()}円`;

  if (ev >= 1.05 || (highProb >= 0.6 && expected >= 5.0)) {
    el.style.display = 'block';
    el.style.background = 'rgba(16,185,129,.1)';
    el.style.border = '1px solid rgba(16,185,129,.25)';
    icon.textContent = '✅';
    text.textContent = 'ヤメ時ではありません — 継続推奨';
    text.style.color = 'var(--success)';
    detail.textContent = evDetail;
  } else if (ev >= 1.00 || highProb >= 0.35) {
    el.style.display = 'block';
    el.style.background = 'rgba(245,158,11,.08)';
    el.style.border = '1px solid rgba(245,158,11,.25)';
    icon.textContent = '⚠️';
    text.textContent = '要判断 — 状況次第で続行';
    text.style.color = 'var(--warning)';
    detail.textContent = evDetail;
  } else if (r.should_retreat || ev < 0.98) {
    el.style.display = 'block';
    el.style.background = 'rgba(244,63,94,.1)';
    el.style.border = '1px solid rgba(244,63,94,.25)';
    icon.textContent = '🚨';
    text.textContent = '撤退推奨';
    text.style.color = 'var(--danger)';
    detail.textContent = r.retreat_reason ? `${r.retreat_reason} / ${evDetail}` : evDetail;
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
  const hasData = estGames.value || Array.from(estCountsList.querySelectorAll('.count-input')).some(i => +i.value > 0);
  if (hasData && !confirm('カウントをリセットしますか？')) return;
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
    loadEstimationAccuracy(hallFilter || null);
  } catch (e) {
    showToast('セッション取得失敗: ' + e.message, 'error');
  }
}

async function loadEstimationAccuracy(hallName) {
  const card = document.getElementById('estimation-accuracy-card');
  const body = document.getElementById('estimation-accuracy-body');
  if (!card) return;
  try {
    const params = hallName ? `?hall_name=${encodeURIComponent(hallName)}&limit=200` : '?limit=200';
    const d = await apiFetch(`/api/sessions/estimation_accuracy${params}`);
    if (!d || d.message || d.total_sessions_analyzed < 5) {
      card.style.display = 'none';
      return;
    }
    const sign = v => v >= 0 ? `+${v}` : `${v}`;
    const dirColor = d.direction_accuracy >= 60 ? 'var(--success)' : d.direction_accuracy >= 50 ? 'var(--warning)' : 'var(--danger)';
    const hiWrColor = (d.high_setting_est_win_rate || 0) >= 55 ? 'var(--success)' : 'var(--warning)';
    const bracketRows = (d.high_prob_brackets || []).map(b =>
      `<tr style="border-bottom:1px solid var(--bg2)">
        <td style="padding:4px 6px;font-size:.73rem">${b.bracket}</td>
        <td style="padding:4px 6px;font-size:.73rem;text-align:right">${b.count}回</td>
        <td style="padding:4px 6px;font-size:.73rem;text-align:right;color:${b.win_rate>=55?'var(--success)':b.win_rate>=45?'var(--warning)':'var(--danger)'}">${b.win_rate}%</td>
        <td style="padding:4px 6px;font-size:.73rem;text-align:right;color:${b.avg_diff>=0?'var(--success)':'var(--danger)'}">${sign(b.avg_diff)}枚</td>
      </tr>`
    ).join('');

    body.innerHTML = `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
        <div style="background:var(--bg2);border-radius:6px;padding:8px;text-align:center">
          <div style="font-size:.65rem;color:var(--text3)">高設定推定時勝率</div>
          <div style="font-size:1.3rem;font-weight:900;color:${hiWrColor}">${d.high_setting_est_win_rate ?? '--'}%</div>
          <div style="font-size:.6rem;color:var(--text3)">${d.high_setting_est_sessions}セッション</div>
        </div>
        <div style="background:var(--bg2);border-radius:6px;padding:8px;text-align:center">
          <div style="font-size:.65rem;color:var(--text3)">方向性精度</div>
          <div style="font-size:1.3rem;font-weight:900;color:${dirColor}">${d.direction_accuracy}%</div>
          <div style="font-size:.6rem;color:var(--text3)">高設定→収益/低→損失の一致率</div>
        </div>
      </div>
      ${bracketRows ? `<div style="font-size:.65rem;color:var(--text3);margin-bottom:4px;font-weight:600">高設定確率別成績</div>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:var(--bg2);color:var(--text2)">
          <th style="padding:4px 6px;text-align:left;font-size:.63rem">確率帯</th>
          <th style="padding:4px 6px;text-align:right;font-size:.63rem">回数</th>
          <th style="padding:4px 6px;text-align:right;font-size:.63rem">勝率</th>
          <th style="padding:4px 6px;text-align:right;font-size:.63rem">平均差枚</th>
        </tr></thead>
        <tbody>${bracketRows}</tbody>
      </table>` : ''}
      <div style="font-size:.62rem;color:var(--text3);margin-top:6px">分析対象: ${d.total_sessions_analyzed}セッション / 全体勝率 ${d.overall_win_rate}%</div>`;
    card.style.display = 'block';
  } catch(e) {
    card.style.display = 'none';
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
        if (![...hallTrendSel.options].some(o => o.value === h)) {
          const o3 = document.createElement('option');
          o3.value = h; o3.textContent = (_isFavHall(h) ? '★ ' : '') + h;
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
    container.innerHTML = `<div class="empty-hint" style="padding:24px 12px">
      <span>📝</span>
      <strong style="color:var(--text2)">実戦記録なし</strong>
      <span>推測ページで推測後、「記録する」ボタンで保存できます</span>
    </div>`;
    return;
  }
  container.innerHTML = sessions.map(s => {
    const diffYen = s.diff_yen || 0;
    const expectedSetting = s.posterior ? calcExpectedSetting(s.posterior) : null;
    const highProb = s.posterior ? Object.entries(s.posterior).filter(([k]) => parseInt(k)>=4).reduce((a,[,p])=>a+p,0) : 0;

    // 自動推定ラベル
    let settingLabel = '';
    if (s.posterior) {
      if (expectedSetting >= 4.5 || highProb >= 0.6) {
        settingLabel = `<span style="background:rgba(16,185,129,.15);color:var(--success);font-size:.6rem;padding:1px 5px;border-radius:3px;font-weight:700">高設定濃厚</span>`;
      } else if (expectedSetting >= 3.5 || highProb >= 0.35) {
        settingLabel = `<span style="background:rgba(251,146,60,.12);color:var(--warning);font-size:.6rem;padding:1px 5px;border-radius:3px;font-weight:700">中間設定</span>`;
      } else if (expectedSetting !== null && expectedSetting < 2.5) {
        settingLabel = `<span style="background:rgba(239,68,68,.1);color:var(--danger);font-size:.6rem;padding:1px 5px;border-radius:3px;font-weight:700">低設定</span>`;
      }
    }

    const tags = [
      s.is_event_day ? '<span class="tag tag-event">イベント</span>' : '',
      s.is_corner ? '<span class="tag tag-corner">角台</span>' : '',
      settingLabel,
    ].filter(Boolean).join('');

    // 設定分布バー（推測結果がある場合）
    let posteriorBar = '';
    if (s.posterior) {
      const entries = Object.entries(s.posterior).sort((a,b) => parseInt(a[0])-parseInt(b[0]));
      const bars = entries.map(([setting, prob]) => {
        const w = Math.round(prob * 100);
        const col = parseInt(setting) >= 4 ? 'var(--primary-h)' : parseInt(setting) >= 2 ? 'var(--text3)' : 'rgba(255,255,255,0.2)';
        return `<div title="設定${setting}: ${Math.round(prob*100)}%" style="flex:${w||1};height:3px;background:${col};border-radius:1px"></div>`;
      }).join('');
      const eSetting = expectedSetting !== null ? expectedSetting.toFixed(1) : '';
      const highCol = highProb >= 0.5 ? 'var(--success)' : highProb >= 0.3 ? 'var(--warning)' : 'var(--text3)';
      posteriorBar = `<div style="margin-top:5px">
        <div style="display:flex;gap:1px;height:3px;border-radius:2px;overflow:hidden;margin-bottom:3px">${bars}</div>
        <div style="font-size:.65rem;color:var(--text3)">推測設定 <strong style="color:var(--text2)">${eSetting}</strong> 高設定 <strong style="color:${highCol}">${Math.round(highProb*100)}%</strong></div>
      </div>`;
    }
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
        </div>
        ${posteriorBar}
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

  // セッションモーダルではフッターを表示
  const footer = document.getElementById('modal-delete')?.closest('.modal-footer');
  if (footer) footer.style.display = '';
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
  // フッターをデフォルト状態に戻す（次回開くときに正しく表示される）
  const footer = document.getElementById('modal-delete')?.closest('.modal-footer');
  if (footer) footer.style.display = '';
}
document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('modal-close2').addEventListener('click', closeModal);
document.getElementById('modal-overlay').addEventListener('click', e => {
  if (e.target === document.getElementById('modal-overlay')) closeModal();
});

// 台詳細モーダル（BB/RBヒストリー）
function _addSeatHistory(hall, machine, seat) {
  const key = 'seat_history_v1';
  let hist = [];
  try { hist = JSON.parse(localStorage.getItem(key) || '[]'); } catch(e) {}
  hist = hist.filter(h => !(h.hall === hall && h.machine === machine && h.seat === seat));
  hist.unshift({ hall, machine, seat, ts: Date.now() });
  localStorage.setItem(key, JSON.stringify(hist.slice(0, 8)));
  _renderSeatHistory();
}

function _renderSeatHistory() {
  const el = document.getElementById('seat-history-list');
  if (!el) return;
  const key = 'seat_history_v1';
  let hist = [];
  try { hist = JSON.parse(localStorage.getItem(key) || '[]'); } catch(e) {}
  const hall = getSelectedHall();
  const filtered = hist.filter(h => !hall || h.hall === hall).slice(0, 5);
  if (!filtered.length) { el.style.display = 'none'; return; }
  el.style.display = 'flex';
  el.innerHTML = filtered.map(h => {
    const encH = encodeURIComponent(h.hall), encM = encodeURIComponent(h.machine);
    return `<button class="btn btn-ghost" style="font-size:.68rem;padding:3px 8px;flex-shrink:0;white-space:nowrap"
      onclick="showSeatDetailModal(decodeURIComponent('${encH}'),decodeURIComponent('${encM}'),${h.seat})">${h.seat}番</button>`;
  }).join('');
}

async function showSeatDetailModal(hall, machineName, seatNumber) {
  const body = document.getElementById('modal-body');
  const overlay = document.getElementById('modal-overlay');
  const footer = document.getElementById('modal-delete')?.closest('.modal-footer');
  const title = document.getElementById('modal-title');
  body.innerHTML = loadingHtml(3);
  if (footer) footer.style.display = 'none';
  if (title) title.textContent = `${machineName} ${seatNumber}番台`;
  overlay.style.display = 'flex';
  _addSeatHistory(hall, machineName, seatNumber);

  try {
    const [data, bbRank] = await Promise.all([
      apiFetch(`/api/hall/seat_detail?hall_name=${encodeURIComponent(hall)}&machine_name=${encodeURIComponent(machineName)}&seat_number=${seatNumber}&days=90`),
      apiFetch(`/api/hall/seat_bb_ranking?hall_name=${encodeURIComponent(hall)}&machine_name=${encodeURIComponent(machineName)}&days=60`).catch(() => []),
    ]);
    if (!data || !data.history?.length) {
      body.innerHTML = `<div style="padding:20px;color:var(--text3)">データなし</div>`;
      return;
    }

    const hist = data.history.slice().reverse(); // 古い順
    const diffs = hist.map(h => h.diff || 0);
    const avgDiff = data.avg_diff || 0;
    const winRate = data.win_rate || 0;
    const sign = v => v >= 0 ? `+${v}` : `${v}`;
    const totalDays = hist.length;

    // 曜日別テーブル
    const dowRows = (data.weekday_stats || []).map(w => {
      const c = w.avg_diff >= 0 ? 'var(--success)' : 'var(--danger)';
      return `<tr>
        <td style="padding:4px 8px;font-size:.78rem">${w.weekday}</td>
        <td style="padding:4px 8px;font-size:.78rem;color:${c};font-weight:700">${sign(w.avg_diff)}枚</td>
        <td style="padding:4px 8px;font-size:.75rem;color:var(--text3)">${w.count}日 / 勝率${w.win_rate}%</td>
      </tr>`;
    }).join('');

    // 差枚ヒストリーリスト
    const histRows = [...hist].reverse().slice(0, 15).map(h => {
      const c = h.diff >= 0 ? 'var(--success)' : 'var(--danger)';
      const bbStr = (h.bb_prob && h.games) ? `BB 1/${Math.round(h.games/h.bb_prob)}` : (h.bb_prob ? `BB ${h.bb_prob}回` : '');
      const rbStr = (h.rb_prob && h.games) ? `RB 1/${Math.round(h.games/h.rb_prob)}` : (h.rb_prob ? `RB ${h.rb_prob}回` : '');
      return `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-size:.75rem">
        <span style="color:var(--text3)">${h.date} ${h.games ? h.games+'G' : ''}</span>
        <span style="color:var(--text3);font-size:.68rem">${[bbStr,rbStr].filter(Boolean).join(' / ')}</span>
        <strong style="color:${c}">${sign(h.diff)}枚</strong>
      </div>`;
    }).join('');

    // 連続好調・BBトレンドバッジ
    const streakBadge = (data.win_streak >= 2)
      ? `<span style="background:rgba(16,185,129,.15);color:var(--success);font-size:.68rem;font-weight:700;padding:2px 8px;border-radius:4px">🔥 ${data.win_streak}連勝中</span>`
      : '';
    const maxStreakBadge = (data.max_streak >= 3 && data.win_streak < data.max_streak)
      ? `<span style="background:rgba(251,191,36,.1);color:var(--warning);font-size:.68rem;padding:2px 8px;border-radius:4px">最長${data.max_streak}連勝</span>`
      : '';
    const bbTrBadge = (data.bb_trend_14d !== null && data.bb_trend_14d !== undefined)
      ? (() => {
          const v = data.bb_trend_14d;
          const col = v > 0.001 ? 'var(--success)' : v < -0.001 ? 'var(--danger)' : 'var(--text3)';
          const arrow = v > 0.001 ? '↑' : v < -0.001 ? '↓' : '→';
          return `<span style="color:${col};font-size:.68rem;font-weight:700;padding:2px 8px;background:rgba(255,255,255,.04);border-radius:4px">BB ${arrow} ${v > 0 ? '+' : ''}${v.toFixed(3)}%</span>`;
        })()
      : '';
    const bestDowBadge = data.best_weekday
      ? `<span style="background:rgba(124,127,245,.1);color:var(--primary-h);font-size:.68rem;padding:2px 8px;border-radius:4px">${data.best_weekday.weekday}曜に強い (平均${data.best_weekday.avg_diff > 0 ? '+' : ''}${data.best_weekday.avg_diff}枚)</span>`
      : '';
    const dateBlockHtml = (data.date_block_analysis && data.date_block_analysis.length > 0)
      ? (() => {
          const best = data.date_block_analysis[0];
          return best.avg_diff > 0
            ? `<div style="font-size:.68rem;color:var(--text3);margin-top:6px">月内傾向: <strong style="color:var(--success)">${best.block}</strong>が最強 (平均+${best.avg_diff}枚 / 勝率${best.win_rate}%)</div>`
            : '';
        })()
      : '';
    body.innerHTML = `
      <div style="margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <span style="font-size:1.05rem;font-weight:800">${esc(machineName)} ${seatNumber}番台</span>
          <button onclick="togglePinSeat(${JSON.stringify(hall)},${JSON.stringify(machineName)},${seatNumber})" id="pin-seat-btn"
            style="background:none;border:1px solid var(--border);border-radius:5px;padding:3px 8px;font-size:.7rem;cursor:pointer;color:var(--text2)">
            ${isPinnedSeat(hall, machineName, seatNumber) ? '★ ピン中' : '☆ ピン'}
          </button>
        </div>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:6px">
          <div style="background:var(--bg2);border-radius:6px;padding:6px;text-align:center">
            <div style="font-size:.58rem;color:var(--text3)">期間</div>
            <div style="font-size:.88rem;font-weight:800;color:var(--text1)">${totalDays}日</div>
          </div>
          <div style="background:var(--bg2);border-radius:6px;padding:6px;text-align:center">
            <div style="font-size:.58rem;color:var(--text3)">平均差枚</div>
            <div style="font-size:.88rem;font-weight:800;color:${avgDiff>=0?'var(--success)':'var(--danger)'}">${sign(avgDiff)}枚</div>
          </div>
          <div style="background:var(--bg2);border-radius:6px;padding:6px;text-align:center">
            <div style="font-size:.58rem;color:var(--text3)">勝率</div>
            <div style="font-size:.88rem;font-weight:800;color:${winRate>=50?'var(--success)':'var(--danger)'}">${winRate}%</div>
          </div>
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">${streakBadge}${maxStreakBadge}${bbTrBadge}${bestDowBadge}</div>
        ${dateBlockHtml}
      </div>
      <canvas id="seat-detail-chart" height="150" style="margin-bottom:12px;display:block;width:100%"></canvas>
      ${dowRows ? `<div style="margin-bottom:10px">
        <div style="font-size:.68rem;color:var(--text3);margin-bottom:4px;font-weight:600;text-transform:uppercase">曜日別実績</div>
        <table style="width:100%;border-collapse:collapse">${dowRows}</table>
      </div>` : ''}
      <div style="font-size:.68rem;color:var(--text3);margin-bottom:4px;font-weight:600;text-transform:uppercase">直近履歴</div>
      ${histRows}
      <div style="margin-top:12px">
        <div style="font-size:.68rem;color:var(--text3);margin-bottom:4px;font-weight:600;text-transform:uppercase">メモ</div>
        <textarea id="seat-memo-ta" placeholder="台のメモ（自動保存）" style="width:100%;box-sizing:border-box;background:var(--bg3);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:.78rem;padding:7px 9px;resize:vertical;min-height:54px;font-family:inherit;outline:none">${esc(localStorage.getItem('pachi_seat_memo_' + hall + '_' + machineName + '_' + seatNumber) || '')}</textarea>
      </div>
      ${bbRank && bbRank.length > 1 ? (() => {
        const thisSeat = bbRank.find(r => r.seat_number === seatNumber);
        const rank = bbRank.findIndex(r => r.seat_number === seatNumber) + 1;
        const rankColor = rank === 1 ? 'var(--success)' : rank <= 3 ? 'var(--warning)' : 'var(--text3)';
        const rows = bbRank.slice(0, 8).map(r => {
          const isCur = r.seat_number === seatNumber;
          const zCol = r.z_score > 0.5 ? 'var(--success)' : r.z_score < -0.5 ? 'var(--danger)' : 'var(--text3)';
          return `<div style="display:flex;align-items:center;gap:6px;padding:3px 6px;border-radius:4px;
            background:${isCur?'rgba(124,127,245,.15)':'transparent'};margin-bottom:2px">
            <span style="font-size:.68rem;color:${isCur?'var(--primary-h)':'var(--text3)'};width:40px;font-weight:${isCur?'700':'400'}">${r.seat_number}番台</span>
            <span style="font-size:.68rem;color:var(--text3)">BB ${r.avg_bb.toFixed(1)}回</span>
            <span style="font-size:.68rem;font-weight:700;color:${zCol}">${r.z_score > 0 ? '+' : ''}${(+r.z_score).toFixed(1)}σ</span>
            ${isCur ? '<span style="font-size:.6rem;background:var(--primary-h);color:#fff;border-radius:3px;padding:0 4px">この台</span>' : ''}
          </div>`;
        }).join('');
        return `<div style="margin-top:12px">
          <div style="font-size:.68rem;color:var(--text3);margin-bottom:6px;font-weight:600;text-transform:uppercase">
            同機種BB確率ランキング
            ${thisSeat ? `<span style="color:${rankColor};margin-left:6px">この台: ${rank}位 (${(+thisSeat.z_score).toFixed(1)}σ)</span>` : ''}
          </div>
          ${rows}
        </div>`;
      })() : ''}
    `;

    // Chart.js — 差枚バー + BB/RB確率ライン（右軸）
    const ctx = document.getElementById('seat-detail-chart')?.getContext('2d');
    if (ctx && hist.length > 1) {
      const labels = hist.map(h => h.date.slice(5));
      const values = diffs;
      // 外れ値の差枚日を強調（平均から2σ以上）
      const avgV = values.reduce((s, v) => s + v, 0) / values.length;
      const stdV = Math.sqrt(values.reduce((s, v) => s + (v-avgV)**2, 0) / values.length) || 1;
      const bgColors = values.map(v => {
        const z = Math.abs(v - avgV) / stdV;
        if (v >= 0) return z >= 2 ? 'rgba(16,185,129,0.7)' : 'rgba(16,185,129,0.3)';
        return z >= 2 ? 'rgba(244,63,94,0.7)' : 'rgba(244,63,94,0.25)';
      });
      const bbProbs = hist.map(h => h.bb_prob ? +(h.bb_prob * 100).toFixed(4) : null);
      const rbProbs = hist.map(h => h.rb_prob ? +(h.rb_prob * 100).toFixed(4) : null);
      const hasBB = bbProbs.some(v => v !== null);
      const hasRB = rbProbs.some(v => v !== null);
      const datasets = [{
        type: 'bar',
        label: '差枚',
        data: values,
        backgroundColor: bgColors,
        borderColor: values.map(v => v >= 0 ? '#10b981' : '#f43f5e'),
        borderWidth: 1,
        yAxisID: 'y',
        order: 3,
      }];
      if (hasBB) datasets.push({
        type: 'line', label: 'BB%',
        data: bbProbs,
        borderColor: 'rgba(124,127,245,0.9)',
        backgroundColor: 'transparent',
        borderWidth: 2,
        pointRadius: 2.5,
        pointBackgroundColor: 'rgba(124,127,245,0.9)',
        tension: 0.3,
        yAxisID: 'y2', order: 1, spanGaps: true,
      });
      if (hasRB) datasets.push({
        type: 'line', label: 'RB%',
        data: rbProbs,
        borderColor: 'rgba(251,146,60,0.75)',
        backgroundColor: 'transparent',
        borderWidth: 1.5,
        borderDash: [4, 3],
        pointRadius: 1.5,
        tension: 0.3,
        yAxisID: 'y2', order: 2, spanGaps: true,
      });
      new Chart(ctx, {
        data: { labels, datasets },
        options: {
          responsive: true,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { display: hasBB || hasRB, labels: { font: { size: 9 }, color: '#6b7280', boxWidth: 10 } },
            tooltip: {
              callbacks: {
                afterLabel: (ctx) => {
                  if (ctx.datasetIndex === 0 && hist[ctx.dataIndex]) {
                    const h = hist[ctx.dataIndex];
                    return h.games ? `${h.games}G` : '';
                  }
                  return '';
                }
              }
            }
          },
          scales: {
            x: { ticks: { font: { size: 9 }, color: '#6b7280', maxTicksLimit: 12 }, grid: { display: false } },
            y: { ticks: { font: { size: 9 }, color: '#6b7280' }, grid: { color: 'rgba(255,255,255,0.05)' } },
            ...(hasBB || hasRB ? { y2: {
              position: 'right',
              ticks: { font: { size: 8 }, color: 'rgba(124,127,245,0.7)', callback: v => v + '%' },
              grid: { display: false },
            }} : {}),
          }
        }
      });
    }
    // 台メモ自動保存
    const memoKey = `pachi_seat_memo_${hall}_${machineName}_${seatNumber}`;
    const memoTa = document.getElementById('seat-memo-ta');
    if (memoTa) {
      let _memoT = null;
      memoTa.addEventListener('input', () => {
        clearTimeout(_memoT);
        _memoT = setTimeout(() => localStorage.setItem(memoKey, memoTa.value), 600);
      });
    }
  } catch(e) {
    body.innerHTML = `<div style="padding:20px;color:var(--danger)">エラー: ${esc(e.message)}</div>`;
  }
}

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
let _loadHallPageGen = 0; // race condition guard
async function loadHallPage() {
  const hall = getSelectedHall();
  if (!hall) return;
  const gen = ++_loadHallPageGen;
  const isStale = () => gen !== _loadHallPageGen;
  _addRecentHall(hall);
  _loadHallMemo(hall);
  const _gradeBanner = document.getElementById('hall-grade-banner');
  if (_gradeBanner) _gradeBanner.style.display = 'none';
  try {
    // セッション統計を取得
    const [daitoData, hallStats] = await Promise.allSettled([
      api.getDaitoAnalysis(),
      apiFetch(`/api/hall/stats?hall_name=${encodeURIComponent(hall)}`),
    ]);
    if (isStale()) return; // 別のホールに切り替わっていたら中止

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
      // 他ホール → anaslo DBの機種ランキングを使用
      renderWeekdayChartFromSessions(hall);
      // anaslo机種ランキングを優先表示（なければセッション由来）
      try {
        const anasloRanking = await api.getMachineRanking(hall);
        if (isStale()) return;
        const rankEl = document.getElementById('hall-machine-ranking');
        if (anasloRanking && anasloRanking.length > 0 && anasloRanking[0].source === 'anaslo_db') {
          rankEl.innerHTML = renderAnasloMachineRanking(anasloRanking);
        } else if (stats && stats.machine_stats) {
          rankEl.innerHTML = renderMachineRankingFromSessions(stats.machine_stats);
        } else {
          rankEl.innerHTML = '<p class="hint">まだ記録がありません</p>';
        }
      } catch(e) {
        document.getElementById('hall-machine-ranking').innerHTML =
          stats && stats.machine_stats
            ? renderMachineRankingFromSessions(stats.machine_stats)
            : '<p class="hint">まだ記録がありません</p>';
      }
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
  renderPinnedSeatsCard();
  _renderSeatHistory();
  loadDowHeatmap(hall);
  loadTodayBriefing(hall);
  loadBBSurgeSeats(hall);
  loadEventDayPattern(hall);
  loadTodayDowMachines(hall);
  loadMachineSettingTendency(hall);
  loadSlumpSeats(hall);
}

async function loadSlumpSeats(hall) {
  const card = document.getElementById('slump-seats-card');
  const body = document.getElementById('slump-seats-body');
  if (!card) return;
  try {
    const rows = await apiFetch(`/api/hall/slump_seats?hall_name=${encodeURIComponent(hall)}&days=5&min_slump=0.5`);
    if (!rows || !rows.length) { card.style.display = 'none'; return; }
    card.style.display = 'block';
    const maxSZ = Math.max(...rows.map(r => r.slump_z), 1);
    body.innerHTML = `<p style="font-size:.72rem;color:var(--text3);margin-bottom:8px">直近5日のBB確率が平均より急落（低設定継続 or 変更待ち候補）</p>
      ${rows.slice(0, 8).map(r => {
        const z = r.slump_z;
        const slumpCol = z > 2.0 ? '#f43f5e' : z > 1.5 ? 'var(--danger)' : z > 1.0 ? 'var(--warning)' : 'var(--text3)';
        const barPct = Math.min(Math.round(z / maxSZ * 100), 100);
        const encHall = encodeURIComponent(hall);
        const encM = encodeURIComponent(r.machine_name);
        const pctChange = r.baseline_bb > 0 ? (-(r.baseline_bb - r.recent_bb) / r.baseline_bb * 100).toFixed(0) : null;
        return `<div style="padding:8px 10px;border-radius:8px;margin-bottom:5px;cursor:pointer;
            border:1px solid ${z > 1.0 ? slumpCol + '44' : 'var(--border)'};
            background:${z > 1.5 ? 'rgba(244,63,94,.05)' : 'var(--bg3)'}"
          onclick="showSeatDetailModal(decodeURIComponent('${encHall}'),decodeURIComponent('${encM}'),${r.seat_number})">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px">
            <div style="flex:1;min-width:0">
              <div style="font-weight:700;font-size:.82rem">${esc(r.machine_name)} <span style="color:var(--text3);font-weight:400;font-size:.76rem">${r.seat_number}番台</span></div>
              <div style="font-size:.65rem;color:var(--text3);margin-top:2px">
                過去平均 <span style="color:var(--text2)">${r.baseline_bb.toFixed(1)}回/日</span>
                → 直近5日 <span style="color:${slumpCol};font-weight:700">${r.recent_bb.toFixed(1)}回/日</span>
                ${pctChange ? `<span style="color:${slumpCol}"> (${pctChange}%)</span>` : ''}
              </div>
            </div>
            <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
              <button onclick="event.stopPropagation();togglePinSeat(decodeURIComponent('${encHall}'),decodeURIComponent('${encM}'),${r.seat_number})"
                style="background:none;border:none;cursor:pointer;font-size:.85rem;padding:2px;color:${isPinnedSeat(hall,r.machine_name,r.seat_number)?'var(--warning)':'var(--text3)'}">
                ${isPinnedSeat(hall, r.machine_name, r.seat_number) ? '★' : '☆'}
              </button>
              <div style="font-size:1.1rem;font-weight:900;color:${slumpCol}">-${z.toFixed(1)}σ</div>
            </div>
          </div>
          <div style="height:3px;background:var(--bg2);border-radius:2px">
            <div class="anim-bar" style="width:${barPct}%;height:100%;background:${slumpCol};border-radius:2px"></div>
          </div>
        </div>`;
      }).join('')}`;
  } catch(e) {
    if (card) card.style.display = 'none';
  }
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

function renderAnasloMachineRanking(machines) {
  const sign = v => v >= 0 ? `+${v}` : `${v}`;
  return machines.slice(0, 10).map((m, i) => {
    const avgDiff = m.avg_diff || 0;
    const diffColor = avgDiff >= 0 ? 'var(--success)' : 'var(--danger)';
    const wrColor = (m.win_rate || 0) >= 50 ? 'var(--success)' : 'var(--danger)';
    let trendHtml = '';
    if (m.bb_trend_14d !== null && m.bb_trend_14d !== undefined) {
      const t = m.bb_trend_14d;
      const tc = t > 1 ? 'var(--success)' : t < -1 ? 'var(--danger)' : 'var(--text3)';
      const ta = t > 1 ? '↑' : t < -1 ? '↓' : '→';
      trendHtml = `<span style="font-size:.6rem;color:${tc};margin-left:4px">BB${ta}${t > 0 ? '+' : ''}${t}%</span>`;
    }
    return `<div style="display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid var(--border)">
      <span style="font-size:.75rem;color:var(--text3);width:22px;flex-shrink:0">${i+1}</span>
      <span style="font-size:.82rem;flex:1;color:var(--text1)">${esc(m.machine)}${trendHtml}</span>
      <div style="text-align:right;flex-shrink:0">
        <div style="font-size:.8rem;font-weight:700;color:${diffColor}">${sign(avgDiff)}枚</div>
        <div style="font-size:.65rem;color:${wrColor}">勝率${m.win_rate || 0}%</div>
      </div>
    </div>`;
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
    switchHallTab('detail');
  }
});

document.getElementById('hall-custom-input').addEventListener('change', () => {
  const name = document.getElementById('hall-custom-input').value.trim();
  if (name) switchHallTab('detail');
});

function getSelectedHall() {
  const v = document.getElementById('hall-select').value;
  if (v === '__custom__') {
    return document.getElementById('hall-custom-input').value.trim() || '';
  }
  return v;
}

// ===== ホールメモ (LocalStorage) =====
function _loadHallMemo(hall) {
  const card = document.getElementById('hall-memo-card');
  const ta = document.getElementById('hall-memo-text');
  if (!card || !ta) return;
  card.style.display = 'block';
  ta.value = localStorage.getItem(`pachi_memo_${hall}`) || '';
  ta.dataset.hall = hall;
}

(function() {
  let _memoTimer = null;
  document.addEventListener('input', e => {
    if (e.target.id !== 'hall-memo-text') return;
    const hall = e.target.dataset.hall;
    if (!hall) return;
    clearTimeout(_memoTimer);
    _memoTimer = setTimeout(() => {
      localStorage.setItem(`pachi_memo_${hall}`, e.target.value);
      const saved = document.getElementById('hall-memo-saved');
      if (saved) { saved.style.opacity = '1'; setTimeout(() => { saved.style.opacity = '0'; }, 1500); }
    }, 600);
  });
})();

function _getRecentHalls() {
  try { return JSON.parse(localStorage.getItem('pachi_recent_halls') || '[]'); } catch { return []; }
}
function _addRecentHall(hall) {
  if (!hall) return;
  const halls = _getRecentHalls().filter(h => h !== hall);
  halls.unshift(hall);
  localStorage.setItem('pachi_recent_halls', JSON.stringify(halls.slice(0, 5)));
  _renderRecentHallsBar();
}
function _renderRecentHallsBar() {
  const bar = document.getElementById('recent-halls-bar');
  if (!bar) return;
  const halls = _getRecentHalls();
  if (!halls.length) { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  bar.innerHTML = halls.map(h => {
    const enc = encodeURIComponent(h);
    const label = esc(h.length > 10 ? h.slice(0, 9) + '…' : h);
    return `<button class="btn btn-ghost" style="font-size:.62rem;padding:2px 8px;white-space:nowrap;max-width:110px;overflow:hidden;text-overflow:ellipsis"
      title="${esc(h)}"
      onclick="(function(){var s=document.getElementById('hall-select');s.value=decodeURIComponent('${enc}');s.dispatchEvent(new Event('change'));})();return false">${label}</button>`;
  }).join('') + `<button class="btn btn-ghost" style="font-size:.58rem;padding:2px 6px;color:var(--text3)" onclick="_clearRecentHalls()" title="履歴削除">✕</button>`;
}
function _clearRecentHalls() {
  localStorage.removeItem('pachi_recent_halls');
  _renderRecentHallsBar();
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
  // ホールトレンドと機種ランキング（並列）
  loadHallTrend(hall);
  loadTopMachines(hall);
}

async function loadScrapeReport(hall, date) {
  const el = document.getElementById('scrape-report-table');
  el.innerHTML = '<p class="hint" style="text-align:center;padding:12px">読み込み中...</p>';
  try {
    const rows = await apiFetch(`/api/hall/report?hall_name=${encodeURIComponent(hall)}&report_date=${date}&limit=30`);
    if (!rows || rows.length === 0) {
      el.innerHTML = `<div class="empty-hint" style="padding:12px 0"><span>📋</span><span>${date} のレポートなし</span></div>`;
      return;
    }
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

let _hallTrendChart = null;
async function loadHallTrend(hall) {
  const card = document.getElementById('hall-trend-card');
  if (!card) return;
  try {
    const data = await apiFetch(`/api/hall/trend_summary?hall_name=${encodeURIComponent(hall)}&days=14`);
    if (!data || !data.dates || data.dates.length < 2) { card.style.display = 'none'; return; }
    card.style.display = 'block';
    const summaryEl = document.getElementById('hall-trend-summary');
    if (summaryEl) {
      const sign = data.trend >= 0 ? '+' : '';
      const trendColor = data.trend >= 0 ? 'var(--success)' : 'var(--danger)';
      const trendIcon = data.trend > 100 ? '↑↑' : data.trend > 0 ? '↑' : data.trend < -100 ? '↓↓' : '↓';
      const posCount = (data.values || []).filter(v => v > 0).length;
      const winRate = data.days_data ? Math.round(posCount / data.days_data * 100) : 0;
      summaryEl.innerHTML = `
        <span style="color:var(--text3)">平均</span> <strong style="color:var(--text1)">${data.avg >= 0 ? '+' : ''}${data.avg}枚</strong>
        <span style="margin:0 4px;color:var(--text3)">·</span>
        <span style="color:${trendColor};font-weight:700">${trendIcon}${sign}${data.trend}</span>
        <span style="margin:0 4px;color:var(--text3)">·</span>
        <span style="color:${winRate>=50?'var(--success)':'var(--danger)'}">${winRate}%勝</span>
        <span style="color:var(--text3);font-size:.6rem;margin-left:2px">(${data.days_data}日)</span>`;
    }
    const ctx = document.getElementById('hall-trend-chart')?.getContext('2d');
    if (!ctx) return;
    if (_hallTrendChart) { _hallTrendChart.destroy(); _hallTrendChart = null; }
    const vals = data.values;
    const posData = vals.map(v => v >= 0 ? v : 0);
    const negData = vals.map(v => v < 0 ? v : 0);
    _hallTrendChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: data.dates.map(d => d.slice(5)),
        datasets: [
          { data: posData, backgroundColor: 'rgba(52,211,153,.7)', borderWidth: 0 },
          { data: negData, backgroundColor: 'rgba(239,68,68,.7)', borderWidth: 0 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: {
          callbacks: { label: ctx => `${ctx.parsed.y >= 0 ? '+' : ''}${ctx.parsed.y}枚` }
        }},
        scales: {
          x: { ticks: { font: { size: 10 }, color: '#888' }, grid: { display: false }, stacked: true },
          y: { ticks: { font: { size: 10 }, color: '#888' }, grid: { color: 'rgba(255,255,255,.04)' }, stacked: true },
        },
      },
    });
  } catch(e) {
    const card = document.getElementById('hall-trend-card');
    if (card) card.style.display = 'none';
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
      const barColor = diff >= 100 ? 'var(--success)' : diff >= 0 ? 'var(--warning)' : 'var(--danger)';
      const diffStr = (diff > 0 ? '+' : '') + Math.round(diff).toLocaleString();
      const rankLabel = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : `${i+1}`;
      const encHm = encodeURIComponent(hall), encMm = encodeURIComponent(r.machine_name);
      return `<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;cursor:pointer"
        onclick="renderMachineTrendChart(decodeURIComponent('${encHm}'),decodeURIComponent('${encMm}'))">
        <span style="width:22px;text-align:right;font-size:${i<3?'.82':'.72'}rem;color:var(--text3)">${rankLabel}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--primary);text-decoration:underline dotted">${esc(r.machine_name)}</div>
          <div style="height:5px;background:var(--bg3);border-radius:3px;margin-top:2px;overflow:hidden">
            <div class="anim-bar" style="width:${pct}%;height:5px;background:${barColor};border-radius:3px;box-shadow:0 0 6px ${barColor}55"></div>
          </div>
        </div>
        <span style="font-size:.78rem;font-weight:700;color:${barColor};white-space:nowrap">${diffStr}</span>
        <span style="font-size:.62rem;color:var(--text3);white-space:nowrap">${r.report_count}日</span>
      </div>`;
    }).join('');
  } catch(e) {
    card.style.display = 'none';
  }
}

document.getElementById('scrape-btn').addEventListener('click', async () => {
  const hall = getSelectedHall();
  const btn = document.getElementById('scrape-btn');
  const days = document.getElementById('scrape-days-minrepo')?.value || 30;
  btn.disabled = true;
  try {
    await apiFetch(`/api/hall/scrape?hall_name=${encodeURIComponent(hall)}&days=${days}`, { method: 'POST' });
    showToast(`みんレポ取得開始（${days}日分）。しばらくお待ちください。`);
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
  container.innerHTML = '<p class="hint" style="text-align:center;padding:10px">読み込み中...</p>';
  try {
    let rows = await fetch(`/api/hall/seat_report?hall_name=${encodeURIComponent(hall)}&date=${date}&limit=200`).then(r => r.json());
    if (!rows.length) {
      container.innerHTML = `<div class="empty-hint"><span>🎰</span><span>${date} のアナスロデータなし</span></div>`;
      return;
    }
    // 末尾フィルター
    if (_anasloTailFilter) {
      rows = rows.filter(r => String(r.seat_number).endsWith(_anasloTailFilter));
    }
    if (!rows.length) {
      container.innerHTML = `<div class="empty-hint"><span>🎰</span><span>末尾${_anasloTailFilter}の台データなし</span></div>`;
      return;
    }
    const maxAbs = Math.max(...rows.map(r => Math.abs(r.diff_coins || 0)), 1);
    const html = `<div style="overflow-x:auto">
      <table style="width:100%;font-size:.76rem;border-collapse:collapse">
        <thead><tr style="background:var(--bg2);color:var(--text3);font-size:.62rem;letter-spacing:.04em">
          <th style="padding:5px 6px;text-align:center;font-weight:700">台番</th>
          <th style="padding:5px 6px;text-align:left;font-weight:700">機種</th>
          <th style="padding:5px 6px;text-align:right;font-weight:700">差枚</th>
          <th style="padding:5px 6px;text-align:right;font-weight:700">G数</th>
          <th style="padding:5px 6px;text-align:right;font-weight:700">BB/RB</th>
        </tr></thead>
        <tbody>${rows.map((r, i) => {
          const diff = r.diff_coins ?? 0;
          const diffCol = diff > 3000 ? 'var(--warning)' : diff > 0 ? 'var(--success)' : diff < -2000 ? 'var(--danger)' : 'var(--text3)';
          const barW = Math.round(Math.abs(diff) / maxAbs * 40);
          const barCol = diff >= 0 ? 'rgba(16,185,129,.5)' : 'rgba(244,63,94,.4)';
          const rowBg = diff > 3000 ? 'rgba(251,191,36,.05)' : diff > 0 ? '' : diff < -2000 ? 'rgba(244,63,94,.04)' : '';
          const rankIcon = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : '';
          let bbRb = '-';
          if (r.bb_prob != null || r.rb_prob != null) {
            const bbStr = r.bb_prob != null
              ? (r.games && r.bb_prob > 0 ? `1/${Math.round(r.games / r.bb_prob)}` : `${r.bb_prob.toFixed(1)}回`)
              : '-';
            const rbStr = r.rb_prob != null
              ? (r.games && r.rb_prob > 0 ? `1/${Math.round(r.games / r.rb_prob)}` : `${r.rb_prob.toFixed(1)}回`)
              : '-';
            bbRb = `BB${bbStr} RB${rbStr}`;
          }
          const encH2 = encodeURIComponent(hall), encM2 = encodeURIComponent(r.machine_name);
          return `<tr style="border-bottom:1px solid var(--border);background:${rowBg};cursor:pointer"
            onclick="showSeatDetailModal(decodeURIComponent('${encH2}'),decodeURIComponent('${encM2}'),${r.seat_number})">
            <td style="padding:5px 6px;text-align:center;font-weight:600;color:var(--text2)">${rankIcon ? `${rankIcon}<br><span style="font-size:.58rem;color:var(--text3)">${r.seat_number}</span>` : r.seat_number}</td>
            <td style="padding:5px 6px;max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.72rem">${esc(r.machine_name)}</td>
            <td style="padding:5px 6px;text-align:right">
              <div style="display:flex;align-items:center;gap:4px;justify-content:flex-end">
                <div style="width:${barW}px;height:4px;background:${barCol};border-radius:2px;min-width:2px"></div>
                <span style="font-weight:700;color:${diffCol}">${diff>0?'+':''}${diff.toLocaleString()}</span>
              </div>
            </td>
            <td style="padding:5px 6px;text-align:right;color:var(--text3)">${r.games?.toLocaleString() ?? '-'}</td>
            <td style="padding:5px 6px;text-align:right;color:var(--text3)">${bbRb}</td>
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
    // BB確率直接比較 (より精度高) と 差枚比較 (フォールバック) を並行取得
    const [bbRows, diffRows] = await Promise.all([
      apiFetch(`/api/hall/tail_bb_analysis?hall_name=${encodeURIComponent(hall)}&days=90`).catch(() => []),
      fetch(`/api/hall/tail_analysis?hall_name=${encodeURIComponent(hall)}&days=30`).then(r=>r.json()).catch(() => []),
    ]);

    if (bbRows && bbRows.length > 0) {
      // BB確率z-scoreでバーチャート表示（より精度高い）
      const sign = v => v >= 0 ? `+${v}` : `${v}`;
      const maxAbsZ = Math.max(...bbRows.map(r => Math.abs(r.z_score)), 0.5);
      const topTail = bbRows[0];
      const html = `<p style="font-size:.72rem;color:var(--text3);margin-bottom:8px">
          末尾別BB確率（過去90日 / z-scoreで相対比較）
          ${topTail && topTail.z_score > 0.5 ? `<strong style="color:var(--success)"> 末尾${topTail.tail}が高め</strong>` : ''}
        </p>
        <div style="display:flex;flex-direction:column;gap:5px">
        ${bbRows.map(r => {
          const z = r.z_score;
          const zCol = z > 0.5 ? 'var(--success)' : z < -0.5 ? 'var(--danger)' : 'var(--text3)';
          const barPct = Math.min(Math.round(Math.abs(z) / maxAbsZ * 100), 100);
          const barCol = z > 0 ? 'var(--success)' : 'var(--danger)';
          return `<div style="display:flex;align-items:center;gap:6px">
            <div style="width:24px;text-align:center;font-size:1rem;font-weight:900;color:${z > 0.5 ? 'var(--success)' : z < -0.5 ? 'var(--danger)' : 'var(--text2)'};flex-shrink:0">${r.tail}</div>
            <div style="flex:1;height:10px;background:var(--bg3);border-radius:5px;overflow:hidden">
              <div class="anim-bar" style="width:${barPct}%;height:100%;background:${barCol};border-radius:5px;opacity:.75"></div>
            </div>
            <div style="width:36px;text-align:right;font-size:.7rem;font-weight:700;color:${zCol};flex-shrink:0">${z>0?'+':''}${z}σ</div>
            <div style="width:52px;text-align:right;font-size:.62rem;color:var(--text3);flex-shrink:0">${r.avg_bb.toFixed(1)}回 ${r.win_rate}%勝</div>
          </div>`;
        }).join('')}
        </div>`;
      container.innerHTML = html;
    } else if (diffRows && diffRows.length > 0) {
      // フォールバック: 差枚ベース
      const html = `<p style="font-size:.75rem;color:var(--text3);margin-bottom:6px">過去30日・末尾別平均差枚</p>
        <table style="width:100%;font-size:.8rem;border-collapse:collapse">
          <thead><tr style="background:var(--bg2)"><th>末尾</th><th>平均差枚</th><th>勝率</th><th>件数</th></tr></thead>
          <tbody>${diffRows.map(r => {
            const t = r.tail.replace('末尾', '');
            return `<tr><td>${esc(t)}</td><td>${r.avg_diff > 0 ? '+' : ''}${r.avg_diff}</td><td>${r.win_rate}%</td><td>${r.count}</td></tr>`;
          }).join('')}</tbody></table>`;
      container.innerHTML = html;
    } else {
      container.innerHTML = `<div class="empty-hint" style="padding:18px 12px">
        <span>🎰</span>
        <strong style="color:var(--text2)">末尾データなし</strong>
        <span style="font-size:.72rem">アナスロから台番付きデータを取得すると分析できます</span>
      </div>`;
    }

    // ゾーン分析を末尾テーブルの下に追加
    loadZoneAnalysis(hall);
  } catch(e) {
    container.innerHTML = `<p class="hint center">取得失敗</p>`;
  }
}

async function loadZoneAnalysis(hall) {
  let zoneEl = document.getElementById('zone-analysis-table');
  if (!zoneEl) {
    zoneEl = document.createElement('div');
    zoneEl.id = 'zone-analysis-table';
    zoneEl.style.cssText = 'margin-top:12px';
    const tailTable = document.getElementById('anaslo-tail-table');
    if (tailTable) tailTable.parentNode.appendChild(zoneEl);
  }
  try {
    const rows = await apiFetch(`/api/hall/zone_analysis?hall_name=${encodeURIComponent(hall)}&days=90&zone_size=10`);
    if (!rows || rows.length < 2) { zoneEl.style.display = 'none'; return; }
    const top3 = rows.slice(0, 3);
    const maxAbsZ = Math.max(...rows.map(r => Math.abs(r.z_score)), 1);
    const zoneItems = rows.map(r => {
      const z = r.z_score;
      const zCol = z > 0.5 ? 'var(--success)' : z < -0.5 ? 'var(--danger)' : 'var(--text3)';
      const barPct = Math.min(Math.round(Math.abs(z) / maxAbsZ * 100), 100);
      const barCol = z > 0 ? 'var(--success)' : 'var(--danger)';
      const isTop = z === Math.max(...rows.map(r2 => r2.z_score));
      return `<div style="display:flex;align-items:center;gap:6px;padding:3px 0;${isTop ? 'background:rgba(16,185,129,.04);border-radius:4px;padding:4px 4px;' : ''}">
        <div style="width:70px;font-size:.68rem;color:var(--text2);flex-shrink:0;font-weight:${isTop?'700':'400'}">${esc(r.label)}</div>
        <div style="flex:1;height:8px;background:var(--bg2);border-radius:4px;position:relative">
          <div class="anim-bar" style="width:${barPct}%;height:100%;background:${barCol};border-radius:4px;opacity:.7"></div>
        </div>
        <div style="width:38px;text-align:right;font-size:.68rem;font-weight:700;color:${zCol};flex-shrink:0">${z>0?'+':''}${(+z).toFixed(1)}σ</div>
        <div style="width:28px;text-align:right;font-size:.6rem;color:var(--text3);flex-shrink:0">${r.avg_bb.toFixed(1)}</div>
      </div>`;
    }).join('');
    zoneEl.innerHTML = `<p style="font-size:.72rem;color:var(--text3);margin:10px 0 6px">台番ゾーン別BB確率（10台単位 / 過去90日）
      <span style="font-size:.6rem;color:var(--text3);margin-left:6px">右端: BB確率/日</span></p>
      ${zoneItems}`;
    zoneEl.style.display = '';
  } catch(e) {
    if (zoneEl) zoneEl.style.display = 'none';
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

let _anasloTailFilter = '';

document.getElementById('anaslo-date-select').addEventListener('change', (e) => {
  const hall = getSelectedHall();
  loadAnasloSeatReport(hall, e.target.value);
});

// 末尾フィルターボタン
document.addEventListener('click', e => {
  const btn = e.target.closest('.tail-filter-btn');
  if (!btn) return;
  document.querySelectorAll('.tail-filter-btn').forEach(b => b.classList.remove('active-tail'));
  btn.classList.add('active-tail');
  _anasloTailFilter = btn.dataset.tail || '';
  const hall = getSelectedHall();
  const date = document.getElementById('anaslo-date-select')?.value;
  if (hall && date) loadAnasloSeatReport(hall, date);
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
    const catOrder = ['スマスロ系', '北斗系', 'バジリスク系', 'カバネリ系', 'ハナハナ系', 'その他'];
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
// ---------------------------------------------------------------------------
// ピン留め機能（LocalStorage）
// ---------------------------------------------------------------------------
function _pinKey() { return 'pachi_pinned_seats_v1'; }

function getPinnedSeats() {
  try { return JSON.parse(localStorage.getItem(_pinKey()) || '[]'); } catch { return []; }
}

function isPinnedSeat(hall, machine, seat) {
  return getPinnedSeats().some(p => p.hall === hall && p.machine === machine && p.seat === seat);
}

function togglePinSeat(hall, machine, seat) {
  const pins = getPinnedSeats();
  const idx = pins.findIndex(p => p.hall === hall && p.machine === machine && p.seat === seat);
  if (idx >= 0) {
    pins.splice(idx, 1);
    showToast(`${machine} ${seat}番台のピンを解除しました`);
  } else {
    pins.unshift({ hall, machine, seat, pinned_at: new Date().toISOString() });
    showToast(`${machine} ${seat}番台をピン留めしました`);
  }
  localStorage.setItem(_pinKey(), JSON.stringify(pins.slice(0, 20)));
  // ボタンを即座に更新
  const btn = document.getElementById('pin-seat-btn');
  if (btn) btn.textContent = isPinnedSeat(hall, machine, seat) ? '★ ピン中' : '☆ ピン';
  // ホールページのピン一覧を更新
  renderPinnedSeatsCard();
}

async function quickJumpToSeat() {
  const seatNum = parseInt(document.getElementById('quick-seat-num')?.value);
  const machineName = document.getElementById('quick-machine-name')?.value?.trim();
  const hall = getSelectedHall();
  if (!hall || !seatNum) { showToast('台番を入力してください', 'error'); return; }

  if (machineName) {
    showSeatDetailModal(hall, machineName, seatNum);
    return;
  }
  // 機種名なし → APIで台番から機種名を検索
  try {
    const rows = await apiFetch(`/api/hall/seat_by_number?hall_name=${encodeURIComponent(hall)}&seat_number=${seatNum}`);
    if (!rows || !rows.length) {
      showToast(`${seatNum}番台のデータが見つかりません`, 'error');
      return;
    }
    if (rows.length === 1) {
      showSeatDetailModal(hall, rows[0].machine_name, seatNum);
    } else {
      // 複数機種ヒット → 選択ダイアログ
      const overlay = document.getElementById('modal-overlay');
      const title = document.getElementById('modal-title');
      const body = document.getElementById('modal-body');
      title.textContent = `${seatNum}番台の機種を選択`;
      body.innerHTML = rows.map(r => {
        const encH = encodeURIComponent(hall), encM = encodeURIComponent(r.machine_name);
        return `<div style="padding:10px 0;border-bottom:1px solid var(--border);cursor:pointer"
          onclick="closeModal();showSeatDetailModal(decodeURIComponent('${encH}'),decodeURIComponent('${encM}'),${seatNum})">
          <div style="font-size:.9rem;font-weight:700">${esc(r.machine_name)}</div>
          <div style="font-size:.68rem;color:var(--text3)">${r.record_count}日分のデータ</div>
        </div>`;
      }).join('');
      overlay.style.display = 'flex';
    }
  } catch(e) {
    showToast('検索エラー: ' + e.message, 'error');
  }
}

function renderPinnedSeatsCard() {
  let card = document.getElementById('pinned-seats-card');
  if (!card) return;
  const pins = getPinnedSeats();
  if (pins.length === 0) { card.style.display = 'none'; return; }
  const hall = getSelectedHall();
  const rows = pins.filter(p => !hall || p.hall === hall).map(p => {
    const memo = localStorage.getItem(`pachi_seat_memo_${p.hall}_${p.machine}_${p.seat}`) || '';
    const memoSnip = memo ? `<div style="font-size:.58rem;color:var(--text3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:150px;margin-top:2px">📝 ${esc(memo.slice(0, 30))}</div>` : '';
    return `<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border)">
       <div style="flex:1;min-width:0;margin-right:6px">
         <div style="font-size:.82rem;font-weight:700">${esc(p.machine)} <span style="color:var(--primary-h)">${p.seat}番台</span></div>
         <div style="font-size:.62rem;color:var(--text3)">${esc(p.hall)}</div>
         ${memoSnip}
       </div>
       <div style="display:flex;gap:6px;flex-shrink:0">
         <button onclick="showSeatDetailModal(${JSON.stringify(p.hall)},${JSON.stringify(p.machine)},${p.seat})"
           style="background:var(--bg2);border:none;border-radius:5px;padding:4px 8px;font-size:.7rem;cursor:pointer;color:var(--text2)">詳細</button>
         <button onclick="togglePinSeat(${JSON.stringify(p.hall)},${JSON.stringify(p.machine)},${p.seat})"
           style="background:none;border:1px solid var(--border);border-radius:5px;padding:4px 8px;font-size:.7rem;cursor:pointer;color:var(--text3)">解除</button>
       </div>
     </div>`;
  }).join('');
  if (!rows) { card.style.display = 'none'; return; }
  document.getElementById('pinned-seats-body').innerHTML = rows;
  card.style.display = 'block';
}

// ---------------------------------------------------------------------------
// Opportunity hunting page
// ---------------------------------------------------------------------------
let opportunityState = { profiles: [], candidates: [], recent_results: [], summary: null };
let opportunityQuickResult = null;
let opportunityCrawlerRunning = false;

function opportunityCrawlerDate(value) {
  if (!value) return '未実行';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('ja-JP');
}

function renderOpportunityCrawler(status, candidates) {
  const counts = status?.candidate_counts || {};
  const pending = (Number(counts.pending) || 0) + (Number(counts.conflict) || 0);
  const lastRun = status?.last_run;
  document.getElementById('opp-crawler-status').textContent = opportunityCrawlerRunning
    ? '確認中…'
    : `${pending}件 未確認・最終 ${opportunityCrawlerDate(lastRun?.finished_at || lastRun?.started_at)}`;
  document.getElementById('opp-crawler-supabase').textContent = status?.supabase_configured
    ? 'Supabase同期：設定済み'
    : 'Supabase同期：未設定（この端末には保存されます）';
  const list = document.getElementById('opp-crawler-candidates');
  const reviewable = (candidates || []).filter(item => item.status === 'pending' || item.status === 'conflict');
  if (!reviewable.length) {
    list.innerHTML = '<p class="hint center">未確認の更新候補はありません</p>';
    return;
  }
  list.innerHTML = reviewable.map(item => {
    const extracted = item.extracted || {};
    const points = extracted.curve_points || [];
    const preview = points.slice(0, 4).map(point => `${Number(point.value).toLocaleString()}G: ${opportunityMoney(point.ev_yen, true)}`).join(' / ');
    const conflicts = (item.conflicts || []).map(conflict => esc(conflict.message || conflict)).join('・');
    return `<article class="opp-crawler-item">
      <div class="opp-crawler-item-head"><strong>${esc(item.machine_name || item.catalog_key)}</strong><span class="opp-judgment ${item.status === 'conflict' ? 'opp-judgment-caution' : ''}">${item.status === 'conflict' ? '要注意' : '更新候補'}</span></div>
      <p>${esc(extracted.condition_label || '')}${preview ? `・${esc(preview)}` : ''}</p>
      ${conflicts ? `<p class="opp-crawler-conflict">差異：${conflicts}</p>` : ''}
      <div class="opp-crawler-item-actions">
        <a class="btn btn-secondary btn-sm" href="${esc(item.source_url)}" target="_blank" rel="noopener noreferrer">出典を開く</a>
        <button class="btn btn-primary btn-sm" type="button" data-opp-crawler-approve="${item.id}">この数値を承認</button>
        <button class="btn btn-secondary btn-sm" type="button" data-opp-crawler-reject="${item.id}">不採用</button>
      </div>
    </article>`;
  }).join('');
}

async function loadOpportunityCrawler() {
  try {
    const [status, candidates] = await Promise.all([
      api.getOpportunityCrawlerStatus(),
      api.getOpportunityCrawlerCandidates(),
    ]);
    renderOpportunityCrawler(status, candidates);
  } catch (error) {
    document.getElementById('opp-crawler-status').textContent = '確認失敗';
    document.getElementById('opp-crawler-candidates').innerHTML = `<p class="hint center">${esc(error.message)}</p>`;
  }
}

function currentMonthValue() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
}

function nullableNumber(id) {
  const raw = document.getElementById(id)?.value;
  return raw === '' || raw == null ? null : Number(raw);
}

function opportunityMoney(value, signed = false) {
  if (value == null) return '--';
  const sign = signed && value > 0 ? '+' : '';
  return `${sign}${Number(value).toLocaleString()}円`;
}

function minutesUntilClosing(timeValue) {
  const match = /^(\d{2}):(\d{2})$/.exec(timeValue || '');
  if (!match) return 0;
  const now = new Date();
  const close = new Date(now);
  close.setHours(Number(match[1]), Number(match[2]), 0, 0);
  return Math.max(0, Math.ceil((close.getTime() - now.getTime()) / 60000));
}

function syncQuickConditions(profile) {
  if (!profile) return;
  if (['equivalent', '56', 'other'].includes(profile.exchange_type)) {
    document.getElementById('opp-quick-exchange').value = profile.exchange_type;
  }
  if (['cash', 'medals'].includes(profile.funding_mode)) {
    document.getElementById('opp-quick-funding').value = profile.funding_mode;
  }
  if (profile.reset_status === 'reset_confirmed') {
    document.getElementById('opp-quick-reset').value = 'reset_confirmed';
  } else if (profile.reset_status === 'normal') {
    document.getElementById('opp-quick-reset').value = 'normal';
  }
}

function renderOpportunityExtraFields(profile) {
  const container = document.getElementById('opp-quick-extra-fields');
  if (!container) return;
  const fields = Array.isArray(profile?.input_fields) ? profile.input_fields : [];
  container.innerHTML = fields.map(field => {
    const required = field.required ? ' required' : '';
    const help = field.help ? `<small class="hint">${esc(field.help)}</small>` : '';
    if (field.type === 'select') {
      return `<div><label class="form-label">${esc(field.label)}</label><select class="form-select" data-opp-profile-input="${esc(field.id)}"${required}>${(field.options || []).map(option => `<option value="${esc(option.value)}">${esc(option.label)}</option>`).join('')}</select>${help}</div>`;
    }
    return `<div><label class="form-label">${esc(field.label)}</label><input class="form-input" data-opp-profile-input="${esc(field.id)}" type="number" inputmode="numeric" min="${Number(field.min ?? 0)}"${field.max == null ? '' : ` max="${Number(field.max)}"`} placeholder="${esc(field.placeholder || '')}"${required}>${help}</div>`;
  }).join('');
}

function collectOpportunityExtraInputs() {
  return Object.fromEntries([...document.querySelectorAll('[data-opp-profile-input]')].map(input => [
    input.dataset.oppProfileInput,
    input.value === 'true' ? true : input.value,
  ]));
}

function updateOpportunityQuickProfileSelect(syncConditions = true) {
  const machine = document.getElementById('opp-quick-machine').value;
  const select = document.getElementById('opp-quick-profile');
  const previous = Number(select.value);
  const profiles = opportunityState.profiles.filter(profile => profile.machine_name === machine);
  select.innerHTML = profiles.length
    ? profiles.map(profile => `<option value="${profile.id}">${esc(profile.condition_label || profileTitle(profile))}</option>`).join('')
    : '<option value="">登録済みルールなし</option>';
  select.disabled = profiles.length === 0;
  if (profiles.some(profile => profile.id === previous)) select.value = String(previous);
  const profile = profiles.find(item => item.id === Number(select.value)) || profiles[0];
  if (syncConditions) syncQuickConditions(profile);
  renderOpportunityExtraFields(profile);
  document.getElementById('opp-quick-current-label').textContent = profile
    ? `${profile.metric_name}（${profile.unit_label}）`
    : '現在値';
  document.getElementById('opp-quick-result').innerHTML = '';
  opportunityQuickResult = null;
}

function autoSelectOpportunityQuickProfile() {
  const machine = document.getElementById('opp-quick-machine').value;
  const exchange = document.getElementById('opp-quick-exchange').value;
  const funding = document.getElementById('opp-quick-funding').value;
  const reset = document.getElementById('opp-quick-reset').value;
  const select = document.getElementById('opp-quick-profile');
  const matches = opportunityState.profiles.filter(profile =>
    profile.machine_name === machine &&
    profile.exchange_type === exchange &&
    ['any', funding].includes(profile.funding_mode) &&
    ['any', reset].includes(profile.reset_status)
  );
  if (matches.length && !matches.some(profile => profile.id === Number(select.value))) {
    select.value = String(matches[0].id);
  }
  const profile = opportunityState.profiles.find(item => item.id === Number(select.value));
  renderOpportunityExtraFields(profile);
  document.getElementById('opp-quick-current-label').textContent = profile
    ? `${profile.metric_name}（${profile.unit_label}）`
    : '現在値';
  document.getElementById('opp-quick-result').innerHTML = matches.length
    ? ''
    : '<div class="opp-quick-inline-warning">この条件に一致する検証済みルールはありません</div>';
  opportunityQuickResult = null;
}

function populateOpportunityQuickMachines() {
  const select = document.getElementById('opp-quick-machine');
  const machineNames = [...new Set(opportunityState.profiles.map(profile => profile.machine_name))].sort();
  const saved = localStorage.getItem('pachi_quick_machine');
  const current = select.value;
  select.innerHTML = machineNames.length
    ? machineNames.map(machine => `<option value="${esc(machine)}">${esc(machine)}</option>`).join('')
    : '<option value="">ルール登録済み機種なし</option>';
  select.value = machineNames.includes(current) ? current : (machineNames.includes(saved) ? saved : (machineNames[0] || ''));
  updateOpportunityQuickProfileSelect(true);
}

function opportunityGuideJudgment(profile, point) {
  const expected = point.ev_yen == null ? NaN : Number(point.ev_yen);
  const worstCaseRaw = point.worst_case_yen == null ? profile.worst_case_investment_yen : point.worst_case_yen;
  const worstCase = worstCaseRaw == null ? NaN : Number(worstCaseRaw);
  const verifiedDate = profile.verified_on ? new Date(`${profile.verified_on}T00:00:00`) : null;
  const ageDays = verifiedDate && !Number.isNaN(verifiedDate.getTime())
    ? Math.floor((Date.now() - verifiedDate.getTime()) / 86400000)
    : null;
  if (!['official', 'verified'].includes(profile.confidence) || ageDays == null || ageDays > 180) {
    return { code: 'verify', label: '要確認', reason: '情報の信頼度・確認日を再確認' };
  }
  if (!Number.isFinite(expected)) return { code: 'verify', label: '要確認', reason: '期待値が未登録' };
  if (!Number.isFinite(worstCase)) return { code: 'verify', label: '要確認', reason: '必要資金が未登録' };
  if (Number(point.value) < Number(profile.start_threshold) || expected < 0) {
    return { code: 'wait', label: '見送り', reason: '狙い目ライン未満' };
  }
  const summary = opportunityState.summary || {};
  if (!summary.configured) return { code: 'verify', label: '資金設定', reason: '先に月間資金を設定' };
  if (worstCase > Number(summary.risk_capacity_yen || 0)) {
    return { code: 'funds', label: '資金不足', reason: '必要資金が現在の許容枠を超過' };
  }
  if (expected < 1000) return { code: 'caution', label: '条件付き', reason: '期待値1,000円未満' };
  return { code: 'target', label: '打つ候補', reason: '10秒判定で最終確認' };
}

function opportunityGuideRows(profiles, mode = 'targets') {
  const rows = [];
  profiles.forEach(profile => {
    const curves = Array.isArray(profile.curve_points)
      ? [...profile.curve_points].sort((a, b) => Number(a.value) - Number(b.value))
      : [];
    let points;
    if (mode === 'all' && curves.length) {
      points = curves;
    } else {
      const exact = curves.find(point => Number(point.value) === Number(profile.start_threshold));
      const next = curves.find(point => Number(point.value) >= Number(profile.start_threshold));
      points = [exact || next || {
        value: profile.start_threshold,
        ev_yen: profile.expected_value_yen,
        worst_case_yen: profile.worst_case_investment_yen,
      }];
    }
    points.forEach(point => rows.push({
      profile,
      point,
      judgment: opportunityGuideJudgment(profile, point),
    }));
  });
  const priority = { target: 0, caution: 1, verify: 2, funds: 3, wait: 4 };
  return rows.sort((a, b) => {
    if (mode === 'targets') {
      return (priority[a.judgment.code] - priority[b.judgment.code]) ||
        (Number(b.point.ev_yen || -Infinity) - Number(a.point.ev_yen || -Infinity));
    }
    return a.profile.machine_name.localeCompare(b.profile.machine_name, 'ja') ||
      a.profile.id - b.profile.id || Number(a.point.value) - Number(b.point.value);
  });
}

function renderOpportunityGuide() {
  const body = document.getElementById('opp-guide-body');
  if (!body) return;
  const mode = document.getElementById('opp-guide-mode').value;
  const search = document.getElementById('opp-guide-search').value.trim().toLowerCase();
  const allRows = opportunityGuideRows(opportunityState.profiles || [], mode);
  const rows = search
    ? allRows.filter(({ profile }) => `${profile.machine_name} ${profile.condition_label || ''}`.toLowerCase().includes(search))
    : allRows;
  const targetCount = allRows.filter(row => row.judgment.code === 'target').length;
  const finiteExpected = allRows
    .map(row => row.point.ev_yen == null ? NaN : Number(row.point.ev_yen))
    .filter(Number.isFinite);
  const machineCount = new Set((opportunityState.profiles || []).map(profile => profile.machine_name)).size;
  document.getElementById('opp-guide-count').textContent = `${rows.length}件`;
  document.getElementById('opp-guide-ready-count').textContent = `${targetCount}条件`;
  document.getElementById('opp-guide-max-ev').textContent = finiteExpected.length ? opportunityMoney(Math.max(...finiteExpected), true) : '--';
  document.getElementById('opp-guide-machine-count').textContent = `${machineCount}機種`;
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6" class="hint center">一致する期待値データがありません</td></tr>';
    return;
  }
  body.innerHTML = rows.map(({ profile, point, judgment }) => {
    const expected = point.ev_yen == null ? NaN : Number(point.ev_yen);
    const worstCase = point.worst_case_yen == null ? profile.worst_case_investment_yen : point.worst_case_yen;
    const expectedText = Number.isFinite(expected) ? opportunityMoney(expected, true) : '--';
    const currentText = `${Number(point.value).toLocaleString()}${esc(profile.unit_label || '')}`;
    return `<tr class="opp-guide-row opp-guide-row-${judgment.code}">
      <td data-label="判定"><span class="opp-guide-signal opp-guide-signal-${judgment.code}">${esc(judgment.label)}</span></td>
      <td data-label="機種・条件"><strong class="opp-guide-machine">${esc(profile.machine_name)}</strong><small>${esc(profile.condition_label || '条件未設定')}</small></td>
      <td data-label="現在値"><strong class="opp-guide-current">${currentText}</strong><small>開始 ${Number(profile.start_threshold).toLocaleString()}${esc(profile.unit_label || '')}</small></td>
      <td data-label="期待値"><strong class="${expected >= 0 ? 'opp-money-up' : 'opp-money-down'}">${expectedText}</strong><small>${esc(judgment.reason)}</small></td>
      <td data-label="必要資金"><strong>${opportunityMoney(worstCase)}</strong></td>
      <td data-label="操作"><button class="btn btn-secondary btn-sm" type="button" data-opp-guide-profile="${profile.id}" data-opp-guide-value="${Number(point.value)}">10秒判定</button></td>
    </tr>`;
  }).join('');
}

function renderOpportunityQuickResult(result) {
  const el = document.getElementById('opp-quick-result');
  const labels = {
    target: '着席候補', wait: 'まだ浅い', verify: '要確認', unknown: '判定不能',
    insufficient_funds: '資金不足', condition_mismatch: '条件不一致', closing_risk: '閉店リスク',
  };
  const expected = result.expected_value_yen == null ? '--' : opportunityMoney(result.expected_value_yen, true);
  const warnings = (result.warnings || []).map(warning => `<li>${esc(warning)}</li>`).join('');
  const saveButton = result.actionable
    ? '<button class="btn btn-secondary btn-full" type="button" data-opp-quick-save>候補台として保存</button>'
    : '';
  const adjustments = [
    result.section_adjustment?.active
      ? (result.section_adjustment.rule && !result.section_adjustment.border_reduction
        ? `機種別条件：${esc(result.section_adjustment.reason || '専用条件一致')}`
        : `有利区間差枚：切断目安まで約${Number(result.section_adjustment.remaining_to_cut_coins).toLocaleString()}枚／ボーダー-${Number(result.section_adjustment.border_reduction)}${esc(result.unit_label)}`)
      : '',
    result.replay_adjustment?.active
      ? `再プレイ残り${Number(result.replay_adjustment.remaining_replay_medals || 0).toLocaleString()}枚／現金ギャップ-${opportunityMoney(result.cash_gap_yen)}`
      : '',
  ].filter(Boolean);
  el.innerHTML = `
    <div class="opp-quick-result-head">
      <div><small>${esc(result.machine_name)}・${esc(result.condition_label)}</small><strong>${esc(labels[result.judgment] || result.judgment)}</strong></div>
      <span class="opp-quick-signal opp-quick-signal-${esc(result.judgment)}">${result.actionable ? '候補' : '停止'}</span>
    </div>
    <p class="opp-quick-reason">${esc(result.reason)}</p>
    <div class="opp-quick-metrics">
      <div><small>現在</small><strong>${Number(result.current_value).toLocaleString()}${esc(result.unit_label)}</strong></div>
      <div><small>補正後開始</small><strong>${Number(result.adjusted_start_threshold ?? result.start_threshold).toLocaleString()}${esc(result.unit_label)}</strong></div>
      <div><small>期待値</small><strong>${expected}</strong></div>
      <div><small>必要資金</small><strong>${opportunityMoney(result.worst_case_investment_yen)}</strong></div>
      <div><small>閉店まで</small><strong>${result.minutes_until_close}分</strong></div>
      <div><small>許容資金</small><strong>${opportunityMoney(result.risk_capacity_yen)}</strong></div>
      <div><small>平均 / 閉店安全側</small><strong>${result.estimated_play_minutes || '--'}分 / ${result.safe_play_minutes || '--'}分</strong></div>
      <div><small>期待時給</small><strong>${opportunityMoney(result.ev_per_hour_yen, true)}</strong></div>
    </div>
    ${adjustments.length ? `<div class="opp-adjustment-summary">${adjustments.map(item => `<span>${item}</span>`).join('')}</div>` : ''}
    ${warnings ? `<ul class="opp-quick-warnings">${warnings}</ul>` : ''}
    <div class="opp-quick-stop"><b>やめどき</b>${esc(result.stop_rule || '未登録')}</div>
    ${result.discrepancy_note ? `<div class="opp-discrepancy">数値差の扱い：${esc(result.discrepancy_note)}</div>` : ''}
    ${saveButton}`;
}

function profileTitle(profile) {
  return `${profile.metric_name} ${Number(profile.start_threshold).toLocaleString()}${profile.unit_label}〜`;
}

function safeExternalUrl(raw) {
  try {
    const url = new URL(raw);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
  } catch (_) {
    return '';
  }
}

function updateOpportunityProfileSelect() {
  const machine = document.getElementById('opp-machine').value.trim();
  const select = document.getElementById('opp-profile');
  const profiles = opportunityState.profiles.filter(profile => profile.machine_name === machine);
  select.innerHTML = profiles.length
    ? `<option value="">ルールを選択</option>${profiles.map(profile =>
        `<option value="${profile.id}">${esc(profile.condition_label || '条件未設定')}・${esc(profileTitle(profile))}</option>`
      ).join('')}`
    : '<option value="">登録済みルールなし</option>';
  select.disabled = profiles.length === 0;
  document.getElementById('opp-rule-machine').value = machine;
  updateOpportunityCurrentLabel();
}

function updateOpportunityCurrentLabel() {
  const select = document.getElementById('opp-profile');
  const profile = opportunityState.profiles.find(item => item.id === Number(select.value));
  document.getElementById('opp-current-label').textContent = profile
    ? `${profile.metric_name}（${profile.unit_label}）`
    : '現在値';
}

function renderOpportunitySummary(summary) {
  const status = document.getElementById('opp-budget-status');
  status.textContent = summary.configured ? '設定済み' : '未設定';
  status.className = summary.configured ? 'optional-note' : 'required-note';
  document.getElementById('opp-current-bankroll').textContent = summary.configured ? opportunityMoney(summary.current_bankroll) : '--';
  document.getElementById('opp-remaining-loss').textContent = summary.configured ? opportunityMoney(summary.remaining_loss_yen) : '--';
  const net = document.getElementById('opp-net-profit');
  net.textContent = opportunityMoney(summary.net_profit_yen, true);
  net.className = summary.net_profit_yen >= 0 ? 'opp-money-up' : 'opp-money-down';
  document.getElementById('opp-plays').textContent = `${summary.plays}回`;
  if (summary.configured) {
    document.getElementById('opp-bankroll').value = summary.starting_bankroll;
    document.getElementById('opp-loss-limit').value = summary.loss_limit_yen;
  }
}

function renderOpportunityProfiles(profiles) {
  const el = document.getElementById('opp-profile-list');
  if (!profiles.length) {
    el.innerHTML = '<p class="hint center">狙い目ルールはまだありません</p>';
    return;
  }
  const labels = { official: '公式', verified: '確認済み', reference: '参考', unverified: '未確認' };
  el.innerHTML = `<div style="font-size:.65rem;color:var(--text3);margin-bottom:5px">登録済みルール（${profiles.length}条件）</div>` + profiles.map(profile => {
    const curves = Array.isArray(profile.curve_points) ? profile.curve_points : [];
    const sources = (Array.isArray(profile.source_urls) ? profile.source_urls : [profile.source_url])
      .map(safeExternalUrl).filter(Boolean);
    const curveRows = curves.map(point => `
      <div class="opp-curve-row">
        <span>${Number(point.value).toLocaleString()}${esc(profile.unit_label)}</span>
        <strong class="${point.ev_yen >= 0 ? 'opp-money-up' : 'opp-money-down'}">${opportunityMoney(point.ev_yen, true)}</strong>
        <span>${point.worst_case_yen == null ? '--' : opportunityMoney(point.worst_case_yen)}</span>
      </div>`).join('');
    const sourceLinks = sources.map((url, index) =>
      `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">出典${index + 1}</a>`
    ).join('');
    return `<div class="opp-profile-row">
      <div class="opp-profile-body">
        <div class="opp-profile-head">
          <div>
            <div class="opp-profile-name">${esc(profile.machine_name)}</div>
            <div class="opp-condition">${esc(profile.condition_label || '条件未設定')}</div>
          </div>
          <button class="btn btn-ghost btn-sm" type="button" data-opp-delete-profile="${profile.id}">無効化</button>
        </div>
        <div class="opp-profile-meta">${esc(profileTitle(profile))}・${esc(labels[profile.confidence] || profile.confidence)}・確認 ${esc(profile.verified_on || '未設定')}・必要資金目安 ${opportunityMoney(profile.worst_case_investment_yen)}</div>
        ${curves.length ? `<details class="opp-curve"><summary>期待値表を見る（${curves.length}点）</summary><div class="opp-curve-head"><span>現在値</span><span>期待値</span><span>必要資金目安</span></div>${curveRows}</details>` : ''}
        ${profile.discrepancy_note ? `<div class="opp-discrepancy">数値差の扱い：${esc(profile.discrepancy_note)}</div>` : ''}
        ${sourceLinks ? `<div class="opp-source-links">${sourceLinks}</div>` : ''}
      </div>
    </div>`;
  }).join('');
}

function opportunityJudgmentLabel(judgment) {
  return {
    target: '狙い候補', wait: '待ち', verify: '要検証', unknown: '判定不能',
    insufficient_funds: '資金不足',
  }[judgment] || judgment;
}

function renderOpportunityCandidates(candidates) {
  const el = document.getElementById('opp-candidate-list');
  document.getElementById('opp-open-count').textContent = `${candidates.length}台`;
  if (!candidates.length) {
    el.innerHTML = '<p class="hint center">候補台はまだありません</p>';
    return;
  }
  const today = new Date().toISOString().slice(0, 10);
  el.innerHTML = candidates.map(candidate => {
    const expected = candidate.expected_value_yen == null ? '未算出' : opportunityMoney(candidate.expected_value_yen);
    const location = [candidate.hall_name, candidate.seat_number ? `${candidate.seat_number}番台` : ''].filter(Boolean).join('・') || '店舗・台番未登録';
    const current = `${Number(candidate.current_value).toLocaleString()}${esc(candidate.unit_label || '')}`;
    const source = candidate.source_name ? `・根拠 ${esc(candidate.source_name)}` : '';
    return `<div class="opp-candidate" data-candidate-id="${candidate.id}">
      <div class="opp-candidate-top">
        <span class="opp-rank">${candidate.rank}</span>
        <div class="opp-candidate-main">
          <div class="opp-candidate-name">${esc(candidate.machine_name)}</div>
          <div class="opp-candidate-meta">${esc(candidate.condition_label || '条件未設定')}・${esc(location)}${source}</div>
        </div>
        <span class="opp-judgment opp-judgment-${esc(candidate.judgment)}">${esc(opportunityJudgmentLabel(candidate.judgment))}</span>
      </div>
      <div class="opp-candidate-data">
        <span>現在 <strong>${current}</strong></span>
        <span>開始 <strong>${candidate.start_threshold == null ? '--' : Number(candidate.start_threshold).toLocaleString() + esc(candidate.unit_label || '')}</strong></span>
        <span>期待値 <strong>${expected}</strong></span>
        <span>時間効率 <strong>${candidate.ev_per_hour_yen == null ? '--' : opportunityMoney(candidate.ev_per_hour_yen) + '/h'}</strong></span>
        <span>必要資金目安 <strong>${opportunityMoney(candidate.worst_case_investment_yen)}</strong></span>
      </div>
      <div class="opp-reason">${esc(candidate.reason)}${candidate.stop_rule ? `・やめどき：${esc(candidate.stop_rule)}` : ''}</div>
      <div class="opp-actions">
        <button class="btn btn-secondary btn-sm" type="button" data-opp-result="${candidate.id}">実戦結果を入力</button>
        <button class="btn btn-ghost btn-sm" type="button" data-opp-skip="${candidate.id}">見送り</button>
      </div>
      <form class="opp-result-form" data-opp-result-form="${candidate.id}">
        <div class="form-row-2col">
          <div><label class="form-label">投資（円）</label><input name="investment_yen" class="form-input" type="number" min="0" step="1000" required></div>
          <div><label class="form-label">回収（円）</label><input name="returns_yen" class="form-input" type="number" min="0" step="1000" required></div>
        </div>
        <div class="form-row-2col">
          <div><label class="form-label">実戦日</label><input name="played_on" class="form-input" type="date" value="${today}" required></div>
          <div><label class="form-label">時間（分）</label><input name="played_minutes" class="form-input" type="number" min="0" max="1440"></div>
        </div>
        <div class="form-row"><label class="form-label">メモ</label><input name="notes" class="form-input" placeholder="当選契機・終了状況など"></div>
        <button class="btn btn-primary btn-full" type="submit">結果を保存</button>
      </form>
    </div>`;
  }).join('');
}

function renderOpportunityResults(results) {
  const el = document.getElementById('opp-result-list');
  if (!results.length) {
    el.innerHTML = '<p class="hint center">実戦結果はまだありません</p>';
    return;
  }
  el.innerHTML = results.map(result => {
    const netClass = result.net_profit_yen >= 0 ? 'opp-money-up' : 'opp-money-down';
    return `<div class="opp-result-row">
      <div class="opp-result-body">
        <div class="opp-result-name">${esc(result.machine_name)}${result.seat_number ? `・${result.seat_number}番台` : ''}</div>
        <div class="opp-result-meta">${esc(result.played_on)}・投資 ${opportunityMoney(result.investment_yen)}・回収 ${opportunityMoney(result.returns_yen)}・${result.played_minutes || 0}分</div>
      </div>
      <strong class="${netClass}">${opportunityMoney(result.net_profit_yen, true)}</strong>
    </div>`;
  }).join('');
}

async function loadOpportunityPage() {
  const monthEl = document.getElementById('opp-month');
  if (!monthEl.value) monthEl.value = currentMonthValue();
  const list = document.getElementById('opp-candidate-list');
  if (!opportunityState.summary) list.innerHTML = loadingHtml(3);
  try {
    opportunityState = await api.getOpportunityDashboard(monthEl.value);
    document.getElementById('opp-machine-datalist').innerHTML = state.machines.map(machine => `<option value="${esc(machine)}">`).join('');
    renderOpportunitySummary(opportunityState.summary);
    populateOpportunityQuickMachines();
    renderOpportunityGuide();
    renderOpportunityProfiles(opportunityState.profiles);
    renderOpportunityCandidates(opportunityState.candidates);
    renderOpportunityResults(opportunityState.recent_results || []);
    updateOpportunityProfileSelect();
    loadOpportunityCrawler();

    const alert = document.getElementById('opp-alert');
    if (!opportunityState.summary.configured) {
      alert.textContent = '先に運用資金と月間損失上限を設定してください。資金未設定では狙い候補を出しません。';
      alert.style.display = 'block';
    } else if (!opportunityState.profiles.length) {
      alert.textContent = '狙い目ルールが未登録です。出典と確認日を含むルールを登録してください。';
      alert.style.display = 'block';
    } else {
      alert.style.display = 'none';
    }
  } catch (error) {
    list.innerHTML = `<p class="hint center">読み込み失敗: ${esc(error.message)}</p>`;
  }
}

document.getElementById('opp-month').addEventListener('change', loadOpportunityPage);
document.getElementById('opp-crawler-run').addEventListener('click', async event => {
  if (opportunityCrawlerRunning) return;
  opportunityCrawlerRunning = true;
  event.currentTarget.disabled = true;
  document.getElementById('opp-crawler-status').textContent = '確認中…';
  try {
    await api.runOpportunityCrawler();
    showToast('公開ソースの確認を開始しました。少し待ってから更新候補を確認してください');
    setTimeout(loadOpportunityCrawler, 4000);
  } catch (error) { showToast(error.message, 'error'); }
  finally {
    opportunityCrawlerRunning = false;
    event.currentTarget.disabled = false;
  }
});
document.getElementById('opp-guide-search').addEventListener('input', renderOpportunityGuide);
document.getElementById('opp-guide-mode').addEventListener('change', renderOpportunityGuide);
document.getElementById('opp-quick-machine').addEventListener('change', event => {
  localStorage.setItem('pachi_quick_machine', event.target.value);
  updateOpportunityQuickProfileSelect(true);
});
document.getElementById('opp-quick-profile').addEventListener('change', () => updateOpportunityQuickProfileSelect(true));
document.querySelector('.opp-quick-templates').addEventListener('click', event => {
  const button = event.target.closest('[data-opp-template]');
  if (!button) return;
  const profile = opportunityState.profiles.find(item => item.id === Number(document.getElementById('opp-quick-profile').value));
  if (button.dataset.oppTemplate === 'zero') document.getElementById('opp-quick-current').value = 0;
  if (button.dataset.oppTemplate === 'heaven') {
    document.getElementById('opp-quick-current').value = Number(profile?.heaven_exit_games ?? 32);
    showToast('天国抜け32Gは機種の画面と照合してください');
  }
  if (button.dataset.oppTemplate === 'ceiling') {
    if (profile?.ceiling_threshold == null) return showToast('天井値が未登録です', 'error');
    document.getElementById('opp-quick-current').value = Number(profile.ceiling_threshold);
  }
});
document.getElementById('opp-quick-ocr').addEventListener('change', async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    const { recognizeNumberFromFile } = await import('/mobile/ocr.mjs?v=2.5.0');
    const result = await recognizeNumberFromFile(file);
    document.getElementById('opp-quick-current').value = result.value;
    showToast(`OCR候補 ${result.value}G。表示と照合してください`);
  } catch (error) { showToast(error.message, 'error'); }
  finally { event.target.value = ''; }
});
['opp-quick-exchange', 'opp-quick-funding', 'opp-quick-reset'].forEach(id => {
  document.getElementById(id).addEventListener('change', autoSelectOpportunityQuickProfile);
});
const savedQuickClose = localStorage.getItem('pachi_quick_close');
if (savedQuickClose) document.getElementById('opp-quick-close').value = savedQuickClose;
document.getElementById('opp-quick-close').addEventListener('change', event => {
  localStorage.setItem('pachi_quick_close', event.target.value);
});
document.getElementById('opp-machine').addEventListener('change', updateOpportunityProfileSelect);
document.getElementById('opp-machine').addEventListener('input', () => {
  if (state.machines.includes(document.getElementById('opp-machine').value.trim())) updateOpportunityProfileSelect();
});
document.getElementById('opp-profile').addEventListener('change', updateOpportunityCurrentLabel);

document.getElementById('opp-budget-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await api.updateOpportunityBudget({
      month: document.getElementById('opp-month').value,
      starting_bankroll: Number(document.getElementById('opp-bankroll').value),
      loss_limit_yen: Number(document.getElementById('opp-loss-limit').value),
    });
    showToast('月間資金を保存しました');
    await loadOpportunityPage();
  } catch (error) { showToast(error.message, 'error'); }
});

document.getElementById('opp-quick-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.submitter;
  if (submit) submit.disabled = true;
  try {
    const request = {
      month: document.getElementById('opp-month').value || currentMonthValue(),
      machine_name: document.getElementById('opp-quick-machine').value,
      profile_id: Number(document.getElementById('opp-quick-profile').value),
      current_value: Number(document.getElementById('opp-quick-current').value),
      exchange_type: document.getElementById('opp-quick-exchange').value,
      funding_mode: document.getElementById('opp-quick-funding').value,
      reset_status: document.getElementById('opp-quick-reset').value,
      minutes_until_close: minutesUntilClosing(document.getElementById('opp-quick-close').value),
      section_difference_coins: nullableNumber('opp-section-diff'),
      replay_limit_medals: Number(document.getElementById('opp-replay-limit').value || 0),
      replay_used_medals: Number(document.getElementById('opp-replay-used').value || 0),
      exchange_rate: Number(document.getElementById('opp-exchange-rate').value || 5.6),
      extra_inputs: collectOpportunityExtraInputs(),
    };
    const response = await api.quickAssessOpportunity(request);
    opportunityQuickResult = { ...response, ...request };
    renderOpportunityQuickResult(opportunityQuickResult);
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    if (submit) submit.disabled = false;
  }
});

document.getElementById('opp-profile-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await api.createOpportunityProfile({
      machine_name: document.getElementById('opp-rule-machine').value.trim(),
      condition_label: document.getElementById('opp-condition').value.trim(),
      exchange_type: document.getElementById('opp-rule-exchange').value,
      funding_mode: document.getElementById('opp-rule-funding').value,
      reset_status: document.getElementById('opp-rule-reset').value,
      metric_name: document.getElementById('opp-metric').value.trim(),
      unit_label: document.getElementById('opp-unit').value.trim(),
      start_threshold: Number(document.getElementById('opp-start').value),
      ceiling_threshold: nullableNumber('opp-ceiling'),
      expected_value_yen: nullableNumber('opp-expected'),
      estimated_play_minutes: nullableNumber('opp-estimated-minutes'),
      worst_case_investment_yen: nullableNumber('opp-worst'),
      stop_rule: document.getElementById('opp-stop-rule').value.trim(),
      source_name: document.getElementById('opp-source-name').value.trim(),
      source_url: document.getElementById('opp-source-url').value.trim(),
      verified_on: document.getElementById('opp-verified-on').value || null,
      confidence: document.getElementById('opp-confidence').value,
      notes: '',
    });
    showToast('狙い目ルールを保存しました');
    event.target.reset();
    document.getElementById('opp-metric').value = '現在ゲーム数';
    document.getElementById('opp-unit').value = 'G';
    document.getElementById('opp-condition').value = '通常・等価';
    document.getElementById('opp-confidence').value = 'verified';
    document.getElementById('opp-rule-exchange').value = 'equivalent';
    document.getElementById('opp-rule-funding').value = 'any';
    document.getElementById('opp-rule-reset').value = 'normal';
    await loadOpportunityPage();
  } catch (error) { showToast(error.message, 'error'); }
});

document.getElementById('opp-candidate-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await api.createOpportunityCandidate({
      machine_name: document.getElementById('opp-machine').value.trim(),
      hall_name: document.getElementById('opp-hall').value.trim(),
      seat_number: nullableNumber('opp-seat'),
      current_value: Number(document.getElementById('opp-current-value').value),
      profile_id: Number(document.getElementById('opp-profile').value),
      notes: '',
    });
    document.getElementById('opp-current-value').value = '';
    document.getElementById('opp-seat').value = '';
    showToast('候補台を追加しました');
    await loadOpportunityPage();
  } catch (error) { showToast(error.message, 'error'); }
});

document.getElementById('page-opportunity').addEventListener('click', async event => {
  const crawlerApprove = event.target.closest('[data-opp-crawler-approve]');
  if (crawlerApprove) {
    if (!window.confirm('出典の数値を期待値表へ反映しますか？')) return;
    try {
      await api.approveOpportunityCrawlerCandidate(Number(crawlerApprove.dataset.oppCrawlerApprove));
      showToast('確認済みの期待値データを反映しました');
      await loadOpportunityPage();
    } catch (error) { showToast(error.message, 'error'); }
    return;
  }
  const crawlerReject = event.target.closest('[data-opp-crawler-reject]');
  if (crawlerReject) {
    try {
      await api.rejectOpportunityCrawlerCandidate(Number(crawlerReject.dataset.oppCrawlerReject));
      showToast('更新候補を不採用にしました');
      await loadOpportunityCrawler();
    } catch (error) { showToast(error.message, 'error'); }
    return;
  }
  const guideButton = event.target.closest('[data-opp-guide-profile]');
  if (guideButton) {
    const profile = opportunityState.profiles.find(item => item.id === Number(guideButton.dataset.oppGuideProfile));
    if (profile) {
      document.getElementById('opp-quick-machine').value = profile.machine_name;
      localStorage.setItem('pachi_quick_machine', profile.machine_name);
      updateOpportunityQuickProfileSelect(false);
      document.getElementById('opp-quick-profile').value = String(profile.id);
      syncQuickConditions(profile);
      document.getElementById('opp-quick-current').value = guideButton.dataset.oppGuideValue;
      document.getElementById('opp-quick-result').innerHTML = '';
      opportunityQuickResult = null;
      document.querySelector('.opp-quick-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setTimeout(() => document.getElementById('opp-quick-current').focus(), 350);
    }
    return;
  }
  const quickSaveButton = event.target.closest('[data-opp-quick-save]');
  if (quickSaveButton && opportunityQuickResult?.actionable) {
    try {
      await api.createOpportunityCandidate({
        machine_name: opportunityQuickResult.machine_name,
        current_value: opportunityQuickResult.current_value,
        profile_id: opportunityQuickResult.profile_id,
        notes: `10秒判定・閉店まで${opportunityQuickResult.minutes_until_close}分`,
        section_difference_coins: opportunityQuickResult.section_difference_coins,
        replay_limit_medals: opportunityQuickResult.replay_limit_medals,
        replay_used_medals: opportunityQuickResult.replay_used_medals,
        exchange_rate: opportunityQuickResult.exchange_rate,
        exchange_type: opportunityQuickResult.exchange_type,
        funding_mode: opportunityQuickResult.funding_mode,
        extra_inputs: opportunityQuickResult.extra_inputs || {},
      });
      showToast('候補台として保存しました');
      opportunityQuickResult = null;
      document.getElementById('opp-quick-result').innerHTML = '';
      await loadOpportunityPage();
    } catch (error) { showToast(error.message, 'error'); }
    return;
  }
  const resultButton = event.target.closest('[data-opp-result]');
  if (resultButton) {
    document.querySelector(`[data-opp-result-form="${resultButton.dataset.oppResult}"]`)?.classList.toggle('open');
    return;
  }
  const skipButton = event.target.closest('[data-opp-skip]');
  if (skipButton) {
    try {
      await api.updateOpportunityCandidate(Number(skipButton.dataset.oppSkip), { status: 'skipped' });
      showToast('候補台を見送りにしました');
      await loadOpportunityPage();
    } catch (error) { showToast(error.message, 'error'); }
    return;
  }
  const deleteButton = event.target.closest('[data-opp-delete-profile]');
  if (deleteButton) {
    try {
      await api.deleteOpportunityProfile(Number(deleteButton.dataset.oppDeleteProfile));
      showToast('ルールを無効化しました');
      await loadOpportunityPage();
    } catch (error) { showToast(error.message, 'error'); }
  }
});

document.getElementById('page-opportunity').addEventListener('submit', async event => {
  const form = event.target.closest('[data-opp-result-form]');
  if (!form) return;
  event.preventDefault();
  const formData = new FormData(form);
  try {
    await api.createOpportunityResult(Number(form.dataset.oppResultForm), {
      played_on: formData.get('played_on'),
      investment_yen: Number(formData.get('investment_yen')),
      returns_yen: Number(formData.get('returns_yen')),
      played_minutes: Number(formData.get('played_minutes') || 0),
      notes: String(formData.get('notes') || ''),
    });
    showToast('実戦結果を保存しました');
    await loadOpportunityPage();
  } catch (error) { showToast(error.message, 'error'); }
});

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
  await Promise.all([checkConnection(), loadDesktopVersion()]);
  await loadMachineSelect();
  await populateSessionFilters();
  _renderRecentHallsBar();
  loadTodayHotCard();
  loadWeeklyHighlight();

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
  updateEstimateReadiness();

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

  // 月フィルターのデフォルトを今月に設定
  const monthFilterEl = document.getElementById('ses-month-filter');
  if (monthFilterEl && !monthFilterEl.value) {
    const now = new Date();
    monthFilterEl.value = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}`;
  }

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
      `/api/hall/today_machine_ranking?hall_name=${encodeURIComponent(hall)}&days=120`
    );
    if (!rows || rows.length === 0) { card.style.display = 'none'; return; }

    card.style.display = 'block';
    const dow = rows[0]?.weekday_ja || '';
    title.textContent = `${dow}曜日に強い機種（過去120日同曜日）`;

    const maxAbs = Math.max(...rows.map(r => Math.abs(r.avg_diff)), 1);
    const sign = v => v >= 0 ? `+${v}` : `${v}`;

    body.innerHTML = rows.map((r, i) => {
      const col = r.avg_diff >= 0 ? 'var(--success)' : 'var(--danger)';
      const pct = Math.round(Math.abs(r.avg_diff) / maxAbs * 100);
      const encH = encodeURIComponent(hall);
      const encM = encodeURIComponent(r.machine_name);
      return `<div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border);cursor:pointer"
        onclick="loadMachineSeatRanking(decodeURIComponent('${encH}'),decodeURIComponent('${encM}'))">
        <span style="font-size:.68rem;color:var(--text3);width:16px;text-align:center;flex-shrink:0">${i+1}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:.88rem;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(r.machine_name)}</div>
          <div style="height:3px;background:var(--bg3);border-radius:2px;margin-top:4px">
            <div class="anim-bar" style="width:${pct}%;height:100%;background:${col};border-radius:2px"></div>
          </div>
          <div style="font-size:.62rem;color:var(--text3);margin-top:2px">${r.count}日 ${r.unit_cnt}台 勝${r.win_rate}% → タップで台別</div>
        </div>
        <div style="text-align:right;flex-shrink:0">
          <div style="font-weight:900;color:${col};font-size:.92rem">${sign(r.avg_diff)}枚</div>
          <div style="font-size:.62rem;color:var(--text3)">${r.last_date || ''}</div>
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
    const [rows, highRates] = await Promise.all([
      apiFetch(`/api/hall/machine_setting_tendency?hall_name=${encodeURIComponent(hall)}&days=60`),
      apiFetch(`/api/hall/machine_high_rate?hall_name=${encodeURIComponent(hall)}&days=90`).catch(() => []),
    ]);
    const highRateMap = Object.fromEntries((highRates || []).map(r => [r.machine_name, r]));
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
      const encHallT = encodeURIComponent(hall);
      const encMachT = encodeURIComponent(r.machine_name);
      // トレンド表示
      let trendBadge = '';
      if (r.trend_delta !== null && r.trend_delta !== undefined) {
        const td = r.trend_delta;
        const tCol = td > 50 ? 'var(--success)' : td < -50 ? 'var(--danger)' : 'var(--text3)';
        const tArrow = td > 50 ? '↑' : td < -50 ? '↓' : '→';
        trendBadge = `<span style="font-size:.62rem;color:${tCol};margin-left:4px">${tArrow}${td > 0 ? '+' : ''}${td}枚/日(2週)</span>`;
      }
      // 高設定率バッジ
      const hr = highRateMap[r.machine_name];
      const hrBadge = hr && hr.high_rate > 0
        ? `<span style="font-size:.62rem;padding:1px 5px;border-radius:3px;background:${hr.high_rate>=20?'rgba(16,185,129,.15)':'rgba(255,255,255,.04)'};color:${hr.high_rate>=20?'var(--success)':hr.high_rate>=10?'var(--warning)':'var(--text3)'}">高設定率${hr.high_rate}%</span>`
        : '';
      return `<div style="padding:7px 0;border-bottom:1px solid var(--border);cursor:pointer"
        onclick="loadMachineSeatRankingInline(decodeURIComponent('${encHallT}'),decodeURIComponent('${encMachT}'),this)">
        <div style="display:flex;justify-content:space-between;align-items:flex-start">
          <div style="font-size:.85rem;font-weight:700;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.machine_name)}</div>
          <div style="text-align:right;flex-shrink:0;margin-left:8px">
            <span style="font-size:1rem;font-weight:900;color:${col}">設定${estS.toFixed(1)}</span>
            <span style="font-size:.65rem;color:var(--text3);margin-left:4px">高設定${highPct}%</span>
          </div>
        </div>
        <div style="display:flex;gap:2px;margin:4px 0">${distBar}</div>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">${bbNote}<span style="font-size:.65rem;color:var(--text3)">${r.unit_cnt}台 ${r.records}件</span>${trendBadge}${hrBadge}<span style="font-size:.63rem;color:var(--text3)">タップ→台別</span></div>
        <div class="inline-seat-ranking" style="display:none;margin-top:8px"></div>
      </div>`;
    }).join('');
  } catch(e) {
    if (card) card.style.display = 'none';
  }
}

function selectHall(hallName) {
  const sel = document.getElementById('hall-select');
  if (!sel) return;
  // 既存optionに存在するか確認
  const opt = [...sel.options].find(o => o.value === hallName);
  if (opt) {
    sel.value = hallName;
  } else {
    // カスタム入力に設定
    sel.value = '__custom__';
    const customInput = document.getElementById('hall-custom-input');
    if (customInput) { customInput.value = hallName; customInput.style.display = ''; }
  }
  loadHallPage();
}

async function loadDowHeatmap(hall) {
  const card = document.getElementById('dow-heatmap-card');
  const body = document.getElementById('dow-heatmap-body');
  if (!card) return;
  try {
    const d = await apiFetch(`/api/hall/machine_dow_heatmap?hall_name=${encodeURIComponent(hall)}&top_n=12`);
    if (!d || !d.machines?.length) { card.style.display = 'none'; return; }

    const labels = d.dow_labels || ['月','火','水','木','金','土','日'];
    const todayDow = new Date().getDay(); // 0=日
    const jpTodayDow = todayDow === 0 ? 6 : todayDow - 1; // 月=0

    const zColor = z => {
      if (z == null) return 'var(--bg2)';
      if (z >= 1.0) return 'rgba(16,185,129,.5)';
      if (z >= 0.5) return 'rgba(16,185,129,.25)';
      if (z <= -1.0) return 'rgba(239,68,68,.45)';
      if (z <= -0.5) return 'rgba(239,68,68,.22)';
      return 'var(--bg2)';
    };
    const zText = z => z == null ? '–' : (z >= 0 ? `+${z.toFixed(1)}` : `${z.toFixed(1)}`);

    const header = `<tr><th style="font-size:.6rem;padding:4px 6px;text-align:left;color:var(--text3);white-space:nowrap;position:sticky;left:0;background:var(--bg)">機種</th>` +
      labels.map((l, i) =>
        `<th style="font-size:.65rem;padding:4px 5px;text-align:center;min-width:36px;${i === jpTodayDow ? 'color:var(--primary-h);font-weight:800;border-bottom:2px solid var(--primary)' : 'color:var(--text3)'}">${l}</th>`
      ).join('') + '</tr>';

    const rows = d.machines.map(m => {
      const cells = m.cells.map((c, i) => {
        const tipText = c.bb != null ? `BB:1/${Math.round(1/c.bb*100)}` : '';
        const highlight = i === jpTodayDow ? 'font-weight:800;outline:2px solid var(--primary-h);outline-offset:-1px;border-radius:2px' : '';
        return `<td title="${tipText}" style="text-align:center;padding:4px 2px;background:${zColor(c.z)};font-size:.63rem;line-height:1.1;${highlight}">
          <div>${zText(c.z)}</div>${tipText ? `<div style="font-size:.5rem;color:rgba(255,255,255,.5);margin-top:1px">${tipText}</div>` : ''}
        </td>`;
      }).join('');
      return `<tr><td style="font-size:.65rem;padding:4px 6px;white-space:nowrap;color:var(--text1);position:sticky;left:0;background:var(--bg)">${esc(m.machine)}</td>${cells}</tr>`;
    }).join('');

    body.innerHTML = `<div style="overflow-x:auto;-webkit-overflow-scrolling:touch"><table style="border-collapse:collapse;min-width:100%"><thead>${header}</thead><tbody>${rows}</tbody></table></div>
      <div style="font-size:.6rem;color:var(--text3);margin-top:4px">σ値: 各機種のBB確率を曜日間で標準化 / 今日の曜日(枠線)を強調</div>`;
    card.style.display = 'block';
  } catch(e) {
    card.style.display = 'none';
  }
}

async function loadTodayBriefing(hall) {
  const card = document.getElementById('today-briefing-card');
  const body = document.getElementById('today-briefing-body');
  if (!card) return;
  try {
    const d = await apiFetch(`/api/hall/today_briefing?hall_name=${encodeURIComponent(hall)}`);
    if (!d || d.error) { card.style.display = 'none'; return; }

    const sign = v => v >= 0 ? `+${v}` : `${v}`;
    const eventBanner = d.is_event_candidate
      ? `<div style="background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.3);border-radius:8px;padding:7px 12px;margin-bottom:8px;font-size:.8rem">
           <strong style="color:var(--success)">本日はイベント日候補 (+${d.event_z}σ)</strong>
           <span style="color:var(--text2);margin-left:8px;font-size:.72rem">高設定比率が統計的に上昇しやすい日</span>
         </div>`
      : '';

    const dowColor = d.dow_rank === 1 ? 'var(--success)' : d.dow_rank <= 2 ? 'var(--warning)' : 'var(--text2)';
    const dowRow = d.dow_avg_diff !== null
      ? `<div style="font-size:.78rem;margin-bottom:6px">
           <span style="color:var(--text3)">${d.weekday}曜日の傾向:</span>
           <strong style="color:${dowColor};margin-left:6px">${sign(d.dow_avg_diff)}枚平均 / ${d.dow_total}曜日中${d.dow_rank}位</strong>
         </div>` : '';

    const _seatBtn = (machine, seat, content) => {
      const encH = encodeURIComponent(hall), encM = encodeURIComponent(machine);
      return `<span style="cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px"
        onclick="showSeatDetailModal(decodeURIComponent('${encH}'),decodeURIComponent('${encM}'),${seat})">${content}</span>`;
    };
    const surgeRows = d.bb_surge_seats?.map(s =>
      `<div style="display:flex;justify-content:space-between;font-size:.75rem;padding:3px 0">
         <span style="color:var(--text1)">${_seatBtn(s.machine, s.seat, `${esc(s.machine)} <strong>${s.seat}番台</strong>`)}</span>
         <span style="color:var(--success);font-weight:700">BB急上昇 +${s.surge_z}σ</span>
       </div>`
    ).join('') || '<span style="font-size:.72rem;color:var(--text3)">なし</span>';

    const topSeatRows = d.top_seats?.map((s, i) =>
      `<div style="display:flex;justify-content:space-between;font-size:.75rem;padding:3px 0;border-bottom:1px solid var(--border)">
         <span><strong style="color:var(--text3);margin-right:4px">${i+1}.</strong>${_seatBtn(s.machine, s.seat, `${esc(s.machine)} ${s.seat}番台`)}</span>
         <span style="color:${s.avg_diff>=0?'var(--success)':'var(--danger)'};font-weight:700">${sign(s.avg_diff)}枚</span>
       </div>`
    ).join('') || '';

    const streakRows = d.streak_seats?.length
      ? d.streak_seats.map(s =>
          `<div style="display:flex;justify-content:space-between;font-size:.75rem;padding:3px 0">
             <span style="color:var(--text1)">${_seatBtn(s.machine, s.seat, `${esc(s.machine)} <strong>${s.seat}番台</strong>`)}</span>
             <span style="color:var(--success);font-weight:700">🔥 ${s.streak}連勝 / 平均${sign(s.avg_diff)}枚</span>
           </div>`
        ).join('')
      : '';

    const hrRows = d.high_rate_machines?.map(m =>
      `<div style="display:flex;justify-content:space-between;font-size:.75rem;padding:2px 0">
         <span>${esc(m.machine)}</span>
         <span style="color:${m.high_rate>=25?'var(--success)':m.high_rate>=15?'var(--warning)':'var(--text3)'}">高設定率${m.high_rate}%</span>
       </div>`
    ).join('') || '';

    body.innerHTML = `${eventBanner}${dowRow}
      <div style="margin-bottom:8px">
        <div style="font-size:.65rem;color:var(--text3);font-weight:600;margin-bottom:4px">BB急上昇台 (設定入替シグナル)</div>
        ${surgeRows}
      </div>
      ${streakRows ? `<div style="margin-bottom:8px">
        <div style="font-size:.65rem;color:var(--text3);font-weight:600;margin-bottom:4px">連続好調台 (モメンタム)</div>
        ${streakRows}
      </div>` : ''}
      <div style="margin-bottom:8px">
        <div style="font-size:.65rem;color:var(--text3);font-weight:600;margin-bottom:4px">狙い台TOP${d.top_seats?.length || 0}</div>
        ${topSeatRows}
      </div>
      ${hrRows ? `<div>
        <div style="font-size:.65rem;color:var(--text3);font-weight:600;margin-bottom:4px">高設定率機種</div>
        ${hrRows}
      </div>` : ''}`;
    card.style.display = 'block';
    _renderHallGradeBanner(d);
  } catch(e) {
    card.style.display = 'none';
  }
}

function _renderHallGradeBanner(d) {
  const el = document.getElementById('hall-grade-banner');
  if (!el) return;
  // スコア計算 (0-100)
  let score = 50;
  if (d.is_event_candidate) score += 20;
  if (d.dow_rank === 1) score += 15;
  else if (d.dow_rank === 2) score += 8;
  else if (d.dow_rank >= 4) score -= 10;
  const surgeCount = d.bb_surge_seats?.length || 0;
  score += Math.min(surgeCount * 8, 20);
  const streakCount = d.streak_seats?.length || 0;
  score += Math.min(streakCount * 5, 15);
  score = Math.max(0, Math.min(100, score));

  const grade = score >= 80 ? 'S' : score >= 65 ? 'A' : score >= 50 ? 'B' : score >= 35 ? 'C' : 'D';
  const gradeColors = { S: '#10b981', A: '#7c7ff5', B: '#f59e0b', C: '#6b7280', D: '#ef4444' };
  const gradeLabels = { S: '最高', A: '良好', B: '普通', C: 'やや低め', D: '不良' };
  const col = gradeColors[grade];
  const label = gradeLabels[grade];
  const reasons = [];
  if (d.is_event_candidate) reasons.push('イベント候補');
  if (d.dow_rank === 1) reasons.push(`${d.weekday}曜は特日傾向`);
  if (surgeCount > 0) reasons.push(`BB急上昇${surgeCount}台`);
  if (streakCount > 0) reasons.push(`連続好調${streakCount}台`);
  const reasonStr = reasons.length ? reasons.slice(0, 3).join(' · ') : '通常日';

  el.style.display = 'block';
  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;padding:12px 14px;
      background:linear-gradient(135deg,${col}18,${col}08);
      border:1.5px solid ${col}44;border-radius:12px">
      <div style="text-align:center;flex-shrink:0">
        <div style="font-size:2rem;font-weight:900;color:${col};line-height:1">${grade}</div>
        <div style="font-size:.58rem;color:${col};font-weight:700;opacity:.8">${label}</div>
      </div>
      <div style="flex:1;min-width:0">
        <div style="font-size:.75rem;font-weight:700;color:var(--text1);margin-bottom:2px">本日の推奨度 <span style="font-size:.7rem;font-weight:400;color:var(--text3)">(${score}/100)</span></div>
        <div style="font-size:.68rem;color:var(--text2)">${reasonStr}</div>
      </div>
      <div style="flex-shrink:0">
        <div style="width:36px;height:36px;border-radius:50%;border:3px solid ${col}44;background:${col}18;display:flex;align-items:center;justify-content:center">
          <div style="width:${Math.round(score * 0.34)}px;height:${Math.round(score * 0.34)}px;border-radius:50%;background:${col};max-width:28px;max-height:28px"></div>
        </div>
      </div>
    </div>`;
}

async function loadEventDayPattern(hall) {
  const card = document.getElementById('event-day-card');
  const body = document.getElementById('event-day-body');
  if (!card) return;
  try {
    const d = await apiFetch(`/api/hall/event_day_pattern?hall_name=${encodeURIComponent(hall)}&days=180`);
    if (!d || !d.top_patterns || d.top_patterns.length === 0) {
      card.style.display = 'none';
      return;
    }
    // 今日の日付情報
    const now = new Date();
    const todayTail = now.getDate() % 10;
    const todayDow = ['日','月','火','水','木','金','土'][now.getDay()];
    const isFive = [5,15,25].includes(now.getDate());

    // 今日がイベント日候補かどうか
    const todayHits = d.top_patterns.filter(p => {
      if (p.type === `末尾${todayTail}の日`) return true;
      if (p.type === `${todayDow}曜日`) return true;
      if (p.type === '5・15・25日' && isFive) return true;
      return false;
    });

    const todayBanner = todayHits.length > 0
      ? `<div style="background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.3);border-radius:8px;padding:8px 12px;margin-bottom:8px;font-size:.8rem">
           <span style="color:var(--success);font-weight:600">本日 (${now.getMonth()+1}/${now.getDate()} ${todayDow}曜日) はイベント日候補です</span><br>
           <span style="color:var(--text2);font-size:.72rem">該当パターン: ${todayHits.map(p => p.type).join('・')}</span>
         </div>`
      : '';

    const rows = d.top_patterns.map(p => {
      const isToday = todayHits.includes(p);
      const zColor = p.z >= 1.5 ? 'var(--success)' : p.z >= 0.8 ? 'var(--warning)' : 'var(--text2)';
      return `<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:.78rem">
        <span style="color:${isToday?'var(--success)':'var(--text1)'};font-weight:${isToday?'600':'400'}">${p.type}${isToday?' ◀今日':''}</span>
        <span style="display:flex;gap:8px;align-items:center">
          <span style="color:var(--text3);font-size:.7rem">${p.count}日分</span>
          <span style="color:var(--text3);font-size:.7rem">BB${p.bb_mean.toFixed(3)}%</span>
          <span style="color:${zColor};font-weight:600">+${p.z.toFixed(2)}σ</span>
        </span>
      </div>`;
    }).join('');

    // 次のイベント候補日を計算（最大30日先まで）
    let nextEventHtml = '';
    if (todayHits.length === 0 && d.top_patterns.length > 0) {
      const upcomingDates = [];
      for (let delta = 1; delta <= 30; delta++) {
        const d2 = new Date(now.getTime() + delta * 86400000);
        const t2 = d2.getDate() % 10;
        const dow2 = ['日','月','火','水','木','金','土'][d2.getDay()];
        const isFive2 = [5,15,25].includes(d2.getDate());
        const hits2 = d.top_patterns.filter(p => {
          if (p.type === `末尾${t2}の日`) return true;
          if (p.type === `${dow2}曜日`) return true;
          if (p.type === '5・15・25日' && isFive2) return true;
          return false;
        });
        if (hits2.length > 0) {
          const bestZ = Math.max(...hits2.map(h => h.z));
          upcomingDates.push({ delta, date: d2, hits: hits2, bestZ });
          if (upcomingDates.length >= 2) break;
        }
      }
      if (upcomingDates.length > 0) {
        const next = upcomingDates[0];
        const afterNext = upcomingDates[1];
        const mm = next.date.getMonth() + 1, dd = next.date.getDate();
        const dow3 = ['日','月','火','水','木','金','土'][next.date.getDay()];
        const afterStr = afterNext
          ? `<span style="color:var(--text3);font-size:.65rem;margin-left:8px">次→ ${afterNext.date.getMonth()+1}/${afterNext.date.getDate()}(${['日','月','火','水','木','金','土'][afterNext.date.getDay()]})+${afterNext.delta}日</span>`
          : '';
        nextEventHtml = `<div style="background:rgba(124,127,245,.08);border:1px solid rgba(124,127,245,.2);border-radius:8px;padding:7px 12px;margin-bottom:8px;display:flex;align-items:center;gap:8px">
          <div style="font-size:1.4rem;font-weight:900;color:var(--primary-h);line-height:1">+${next.delta}</div>
          <div>
            <div style="font-size:.75rem;font-weight:600;color:var(--text1)">次候補: ${mm}/${dd}(${dow3}) · ${next.hits[0].type}</div>
            <div style="display:flex;align-items:center;gap:4px">${afterStr}</div>
          </div>
        </div>`;
      }
    }

    body.innerHTML = todayBanner + nextEventHtml + rows +
      `<div style="font-size:.65rem;color:var(--text3);margin-top:6px">過去${d.total_days}日・全台平均BB ${d.global_mean_bb?.toFixed(1)}回/日 | σはBB回数の機種内偏差</div>`;
    card.style.display = 'block';
  } catch (e) {
    card.style.display = 'none';
  }
}

async function loadBBSurgeSeats(hall) {
  const card = document.getElementById('bb-surge-card');
  const body = document.getElementById('bb-surge-body');
  if (!card) return;
  try {
    const rows = await apiFetch(`/api/hall/bb_surge_seats?hall_name=${encodeURIComponent(hall)}&days=3&min_surge=0.5`);
    if (!rows || !rows.length) { card.style.display = 'none'; return; }
    card.style.display = 'block';
    const maxZ = Math.max(...rows.map(r => r.surge_z), 1);
    const html = `<p style="font-size:.72rem;color:var(--text3);margin-bottom:8px">直近3日のBB確率が過去60日平均より急上昇（設定変更シグナル）</p>
      ${rows.slice(0, 8).map(r => {
        const z = r.surge_z;
        const surgeCol = z > 2.0 ? '#fbbf24' : z > 1.5 ? 'var(--warning)' : z > 1.0 ? 'var(--success)' : 'var(--primary-h)';
        const glowStr = z > 1.5 ? `0 0 12px ${z > 2.0 ? 'rgba(251,191,36,.4)' : 'rgba(16,185,129,.3)'}` : 'none';
        const barPct = Math.min(Math.round(z / maxZ * 100), 100);
        const encHall = encodeURIComponent(hall);
        const encM = encodeURIComponent(r.machine_name);
        const pctChange = r.baseline_bb > 0 ? ((r.recent_bb - r.baseline_bb) / r.baseline_bb * 100).toFixed(0) : null;
        return `<div style="padding:8px 10px;border-radius:8px;margin-bottom:5px;cursor:pointer;
            border:1px solid ${z > 1.5 ? surgeCol + '44' : 'var(--border)'};
            background:${z > 1.5 ? `rgba(16,185,129,.06)` : 'var(--bg3)'};
            box-shadow:${glowStr}"
          onclick="showSeatDetailModal(decodeURIComponent('${encHall}'),decodeURIComponent('${encM}'),${r.seat_number})">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px">
            <div style="flex:1;min-width:0">
              <div style="font-weight:700;font-size:.82rem">${esc(r.machine_name)} <span style="color:var(--text3);font-weight:400;font-size:.76rem">${r.seat_number}番台</span></div>
              <div style="font-size:.65rem;color:var(--text3);margin-top:2px">
                過去平均 <span style="color:var(--text2)">${r.baseline_bb.toFixed(1)}回/日</span>
                → 直近3日 <span style="color:${surgeCol};font-weight:700">${r.recent_bb.toFixed(1)}回/日</span>
                ${pctChange ? `<span style="color:${surgeCol}"> (+${pctChange}%)</span>` : ''}
              </div>
            </div>
            <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
              <button onclick="event.stopPropagation();togglePinSeat(decodeURIComponent('${encHall}'),decodeURIComponent('${encM}'),${r.seat_number})"
                style="background:none;border:none;cursor:pointer;font-size:.85rem;padding:2px;color:${isPinnedSeat(hall,r.machine_name,r.seat_number)?'var(--warning)':'var(--text3)'}">
                ${isPinnedSeat(hall, r.machine_name, r.seat_number) ? '★' : '☆'}
              </button>
              <div style="font-size:1.1rem;font-weight:900;color:${surgeCol};text-shadow:${glowStr}">+${z.toFixed(1)}σ</div>
            </div>
          </div>
          <div style="height:3px;background:var(--bg2);border-radius:2px">
            <div class="anim-bar" style="width:${barPct}%;height:100%;background:${surgeCol};border-radius:2px"></div>
          </div>
        </div>`;
      }).join('')}`;
    body.innerHTML = html;
  } catch(e) {
    card.style.display = 'none';
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
    title.innerHTML = `今日(${data.today_weekday}曜日)の狙い台 <button onclick="event.stopPropagation();_copyTodayTargets()" title="テキストコピー" style="background:none;border:none;cursor:pointer;font-size:.7rem;color:var(--text3);padding:0 4px;vertical-align:middle">📋</button>`;
    let html = '';
    if (data.seats.length) {
      html += `<div style="font-size:.68rem;color:var(--text3);margin-bottom:8px;text-transform:uppercase;letter-spacing:.08em">複合スコア順（曜日傾向・BB確率・安定性・トレンド統合）</div>`;
      data.seats.forEach((s, i) => {
        const medals = ['1位', '2位', '3位'];
        const medalCols = ['var(--warning)', 'var(--text2)', '#cd7f32'];
        const col = s.avg_diff >= 0 ? 'var(--success)' : 'var(--danger)';
        const sign = v => v >= 0 ? `+${v}` : `${v}`;
        const stab = (s.stability || 0);
        const stabW = Math.round(stab * 100);
        const stabCol = stab >= 0.7 ? 'var(--success)' : stab >= 0.4 ? 'var(--warning)' : 'var(--danger)';
        const dowBadge = s.avg_same_dow !== undefined && s.avg_same_dow !== s.avg_diff
          ? `<span style="font-size:.68rem;color:var(--primary-h);background:rgba(124,127,245,.12);padding:1px 6px;border-radius:4px">${data.today_weekday}曜 ${sign(s.avg_same_dow)}枚</span>`
          : '';
        const trendBadge = s.avg_7d !== null && s.avg_7d !== undefined
          ? `<span style="font-size:.68rem;color:${s.avg_7d >= s.avg_diff ? 'var(--success)' : 'var(--text3)'};background:var(--bg3);padding:1px 6px;border-radius:4px">直近7日 ${sign(s.avg_7d)}枚</span>`
          : '';
        // BB z-score バッジ（機種内での相対的な高設定確率）
        const bbZ = s.bb_z;
        const bbZBadge = (bbZ !== undefined && bbZ !== null && Math.abs(bbZ) >= 0.3)
          ? `<span style="font-size:.68rem;font-weight:700;color:${bbZ > 0.5 ? 'var(--success)' : bbZ < -0.5 ? 'var(--danger)' : 'var(--text3)'};background:${bbZ > 0.5 ? 'rgba(16,185,129,.1)' : 'var(--bg3)'};padding:1px 6px;border-radius:4px">BB ${bbZ > 0 ? '+' : ''}${bbZ}σ</span>`
          : '';
        const encHall2 = encodeURIComponent(getSelectedHall());
        const encMach2 = encodeURIComponent(s.machine_name);
        const copyStr = `${medals[i]} ${s.machine_name} ${s.seat_number}番 平均${s.avg_diff >= 0 ? '+' : ''}${s.avg_diff}枚 勝率${s.win_rate}%`;
        html += `<div data-seat-copy="${esc(copyStr)}" style="padding:10px 0;border-bottom:1px solid var(--border);cursor:pointer"
          onclick="showSeatDetailModal(decodeURIComponent('${encHall2}'),decodeURIComponent('${encMach2}'),${s.seat_number})">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px">
            <span style="font-size:.65rem;font-weight:900;color:${medalCols[i]};background:rgba(255,255,255,.04);padding:2px 7px;border-radius:4px;flex-shrink:0">${medals[i]}</span>
            <div style="font-weight:800;font-size:.92rem;flex:1">${esc(s.machine_name)} <span style="color:var(--text3);font-weight:400">${s.seat_number}番</span></div>
            <div style="font-weight:900;color:${col};font-size:1.05rem">${sign(s.avg_diff)}枚</div>
          </div>
          <div style="display:flex;gap:5px;flex-wrap:wrap;margin-bottom:6px">
            ${dowBadge}${trendBadge}${bbZBadge}
            <span style="font-size:.68rem;color:var(--text3);background:var(--bg3);padding:1px 6px;border-radius:4px">${s.days}日 / 勝率${s.win_rate}%</span>
            <span style="font-size:.68rem;color:var(--text3)">タップで詳細</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            <span style="font-size:.62rem;color:var(--text3);flex-shrink:0">安定性</span>
            <div style="flex:1;height:4px;background:var(--bg3);border-radius:2px">
              <div class="anim-bar" style="width:${stabW}%;height:100%;background:${stabCol};border-radius:2px"></div>
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

window._copyTodayTargets = function() {
  const body = document.getElementById('today-targets-body');
  if (!body) return;
  const lines = [];
  body.querySelectorAll('[data-seat-copy]').forEach(el => lines.push(el.dataset.seatCopy));
  if (!lines.length) { showToast('コピーできるデータなし', 'error'); return; }
  navigator.clipboard.writeText(lines.join('\n')).then(() => showToast('狙い台をコピーしました'));
};

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

    // 統計サマリー
    const statsEl = document.getElementById('machine-trend-stats');
    if (statsEl) {
      const validDiffs = diffs.filter(v => v !== null);
      const avgDiff = validDiffs.length ? Math.round(validDiffs.reduce((a,b)=>a+b,0)/validDiffs.length) : null;
      const maxDiff = validDiffs.length ? Math.max(...validDiffs) : null;
      const minDiff = validDiffs.length ? Math.min(...validDiffs) : null;
      const winRate = validDiffs.length ? Math.round(validDiffs.filter(v=>v>0).length/validDiffs.length*100) : null;
      const avgSign = avgDiff >= 0 ? '+' : '';
      const avgCol = avgDiff >= 0 ? 'var(--success)' : 'var(--danger)';
      statsEl.innerHTML = [
        avgDiff !== null ? `<span>平均差枚 <strong style="color:${avgCol}">${avgSign}${avgDiff}</strong></span>` : '',
        maxDiff !== null ? `<span>最高 <strong style="color:var(--success)">+${Math.max(maxDiff,0)}</strong></span>` : '',
        minDiff !== null ? `<span>最低 <strong style="color:var(--danger)">${minDiff}</strong></span>` : '',
        winRate !== null ? `<span>プラス率 <strong style="color:${winRate>=50?'var(--success)':'var(--danger)'}">${winRate}%</strong></span>` : '',
        `<span style="color:var(--text3)">${validDiffs.length}日分</span>`,
      ].filter(Boolean).join('');
    }

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
      const bbBadge = r.bb_z != null
        ? `<span style="font-size:.63rem;padding:1px 5px;border-radius:3px;background:${r.bb_z>=0.5?'rgba(16,185,129,.15)':r.bb_z<=-0.5?'rgba(244,63,94,.1)':'var(--bg3)'};color:${r.bb_z>=0.5?'var(--success)':r.bb_z<=-0.5?'var(--danger)':'var(--text3)'}">BB ${r.bb_z>=0?'+':''}${r.bb_z}σ</span>` : '';
      return `<div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border)">
        <span style="font-size:.7rem;color:var(--text3);width:18px;text-align:center;flex-shrink:0">${i+1}</span>
        <div style="flex:1">
          <div style="font-size:.88rem;font-weight:700;display:flex;flex-wrap:wrap;gap:4px;align-items:center">${r.seat_number}番台 ${dowTxt} ${bbBadge}</div>
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

// ---------------------------------------------------------------------------
// 機種傾向カード内インライン台ランキング
// ---------------------------------------------------------------------------
async function loadMachineSeatRankingInline(hall, machineName, rowEl) {
  const slot = rowEl.querySelector('.inline-seat-ranking');
  if (!slot) return;

  if (slot.style.display === 'block') {
    slot.style.display = 'none';
    return;
  }

  slot.style.display = 'block';
  slot.innerHTML = '<span style="font-size:.75rem;color:var(--text3)">台ランキング読み込み中...</span>';

  try {
    const rows = await apiFetch(
      `/api/hall/machine_seat_ranking?hall_name=${encodeURIComponent(hall)}&machine_name=${encodeURIComponent(machineName)}&days=30`
    );
    if (!rows || rows.length === 0) {
      slot.innerHTML = '<span style="font-size:.75rem;color:var(--text3)">台番データなし</span>';
      return;
    }
    const sign = v => v >= 0 ? `+${v}` : `${v}`;
    const items = rows.slice(0, 5).map((r, i) => {
      const col = r.avg_diff >= 0 ? 'var(--success)' : 'var(--danger)';
      const encH = encodeURIComponent(hall);
      const encM = encodeURIComponent(machineName);
      const bbBadge2 = r.bb_z != null
        ? `<span style="font-size:.6rem;color:${r.bb_z>=0.5?'var(--success)':r.bb_z<=-0.5?'var(--danger)':'var(--text3)'}">${r.bb_z>=0?'+':''}${r.bb_z}σ</span>` : '';
      return `<div onclick="showSeatDetailModal(decodeURIComponent('${encH}'),decodeURIComponent('${encM}'),${r.seat_number})"
        style="display:flex;justify-content:space-between;align-items:center;
               padding:5px 8px;border-radius:6px;background:var(--bg2);margin-bottom:3px;cursor:pointer">
        <span style="font-size:.78rem;font-weight:700">#${i+1} ${r.seat_number}番台 ${bbBadge2}</span>
        <span style="font-size:.78rem;font-weight:900;color:${col}">${sign(r.avg_diff)}枚</span>
        <span style="font-size:.63rem;color:var(--text3)">${r.days}日 勝${r.win_rate}%</span>
      </div>`;
    }).join('');
    slot.innerHTML = items;
  } catch(e) {
    slot.innerHTML = '<span style="font-size:.75rem;color:var(--danger)">エラー</span>';
  }
}

init().then(() => {
  // 起動時は必ず目的選択ホームから始める。
  switchTab('home', { record: false, resetHistory: true });
});

// ---------------------------------------------------------------------------
// マップページ
// ---------------------------------------------------------------------------
let _hallMap = null;
let _targetHeatLayer = null;
let _desktopHeatData = null;

function desktopTomorrowValue() {
  const value = new Date();
  value.setDate(value.getDate() + 1);
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
}

function desktopSignedCoins(value) {
  const number = Math.round(Number(value || 0));
  return `${number >= 0 ? '+' : ''}${number.toLocaleString('ja-JP')}枚`;
}

function desktopTrendRows(rows, labelKey) {
  if (!rows?.length) return '<p class="desktop-heat-empty">まだ長期データがありません。</p>';
  const maxAbs = Math.max(1, ...rows.map(row => Math.abs(Number(row.avg_diff || 0))));
  return `<div class="desktop-mini-trend">${rows.map(row => {
    const value = Number(row.avg_diff || 0);
    const width = Math.max(3, Math.round(Math.abs(value) / maxAbs * 100));
    return `<div><span>${esc(row[labelKey])}</span><i><b class="${value >= 0 ? 'desktop-trend-up' : 'desktop-trend-down'}" style="width:${width}%"></b></i><em class="${value >= 0 ? 'money-up' : 'money-down'}">${desktopSignedCoins(value)}</em></div>`;
  }).join('')}</div>`;
}

function renderDesktopHeatDetail(hall) {
  const detail = document.getElementById('desktop-heat-detail');
  if (!detail) return;
  if (!hall) {
    detail.innerHTML = '<div class="desktop-heat-empty">地図の店舗を選ぶと、長期傾向を表示します。</div>';
    return;
  }
  detail.innerHTML = `
    <div class="desktop-heat-detail-head">
      <div><span>${esc(_desktopHeatData.visit_date)}の分析</span><h2>${esc(hall.hall_name)}</h2></div>
      <div class="desktop-heat-score" style="--heat-color:${esc(hall.color)}"><b>${hall.score}</b><small>点</small><em>${esc(hall.heat_level)}</em></div>
    </div>
    <div class="desktop-heat-stats">
      <div><small>指定日推定</small><strong class="${hall.projected_diff >= 0 ? 'money-up' : 'money-down'}">${hall.projected_diff == null ? '--' : desktopSignedCoins(hall.projected_diff)}</strong></div>
      <div><small>プラス日率</small><strong>${hall.positive_rate == null ? '--' : `${hall.positive_rate}%`}</strong></div>
      <div><small>信頼度</small><strong>${esc(hall.confidence)}</strong></div>
      <div><small>長期実績</small><strong>${hall.long_term?.sample_days || 0}日</strong></div>
    </div>
    <div class="desktop-heat-reasons">${(hall.reasons || []).map(reason => `<span>${esc(reason)}</span>`).join('')}</div>
    <section class="desktop-trend-block"><h3>月ごとの長期推移</h3>${desktopTrendRows(hall.monthly_trend, 'month')}</section>
    <section class="desktop-trend-block"><h3>曜日ごとの長期傾向</h3>${desktopTrendRows(hall.weekday_profile, 'weekday')}</section>
    <section class="desktop-heat-machines"><h3>この日の狙い機種</h3>${(hall.target_machines || []).slice(0, 3).map(machine => `<div><strong>${esc(machine.machine_name)}</strong><span class="${machine.avg_diff >= 0 ? 'money-up' : 'money-down'}">${desktopSignedCoins(machine.avg_diff)}</span><small>${machine.sample_days}日・${machine.score}点</small></div>`).join('') || '<p class="desktop-heat-empty">機種候補は材料不足です。</p>'}</section>`;
}

async function loadMapPage() {
  const hint = document.getElementById('map-hint');
  const dateInput = document.getElementById('desktop-heat-date');
  const longDaysInput = document.getElementById('desktop-heat-long-days');
  const form = document.getElementById('desktop-heat-form');
  const button = document.getElementById('desktop-heat-button');
  if (!dateInput.value) dateInput.value = desktopTomorrowValue();
  if (!form.dataset.bound) {
    form.dataset.bound = '1';
    form.addEventListener('submit', event => {
      event.preventDefault();
      loadMapPage();
    });
  }

  if (!_hallMap) {
    _hallMap = L.map('hall-map', { zoomControl: true }).setView([34.724, 135.631], 13);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© OpenStreetMap contributors',
      maxZoom: 18,
    }).addTo(_hallMap);
  }

  hint.textContent = '指定日の店舗熱量と長期傾向を計算中...';
  button.disabled = true;
  try {
    const url = `/api/map/target_heat?visit_date=${encodeURIComponent(dateInput.value)}&days=120&long_days=${encodeURIComponent(longDaysInput.value)}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`API ${response.status}`);
    _desktopHeatData = await response.json();
    const halls = _desktopHeatData.halls || [];
    if (_targetHeatLayer) _targetHeatLayer.remove();
    _targetHeatLayer = L.layerGroup().addTo(_hallMap);

    if (!halls.length) {
      hint.textContent = '表示できる店舗データがありません。データ収集後に再度確認してください。';
      renderDesktopHeatDetail(null);
      return;
    }

    hint.textContent = `${_desktopHeatData.visit_date}（${_desktopHeatData.weekday}）・${halls.length}店舗を表示中。丸が大きいほど当日の分析点が高いです。`;
    halls.forEach(hall => {
      const radius = hall.score ? 12 + hall.score * .22 : 9;
      const marker = L.circleMarker([hall.lat, hall.lng], {
        radius, color: hall.color, fillColor: hall.color, fillOpacity: .74, weight: 3, opacity: 1,
      }).addTo(_targetHeatLayer);
      marker.bindTooltip(esc(`${hall.rank ? `${hall.rank}位 ` : ''}${hall.hall_name} ${hall.score}点`), { direction: 'top' });
      marker.on('click', () => renderDesktopHeatDetail(hall));
    });

    const coords = halls.map(hall => [hall.lat, hall.lng]);
    if (coords.length === 1) _hallMap.setView(coords[0], 14);
    else _hallMap.fitBounds(coords, { padding: [30, 30], maxZoom: 14 });
    renderDesktopHeatDetail(halls[0]);
    setTimeout(() => _hallMap.invalidateSize(), 100);
  } catch(e) {
    hint.textContent = 'マップデータ取得失敗：' + e.message;
  } finally {
    button.disabled = false;
  }
  loadHallCompare();
}

let _compareDays = 30;

// 期間切り替えボタン
document.addEventListener('click', e => {
  const btn = e.target.closest('.compare-days-btn');
  if (!btn) return;
  _compareDays = parseInt(btn.dataset.days);
  document.querySelectorAll('.compare-days-btn').forEach(b => {
    b.classList.toggle('active-period', b === btn);
    b.style.borderColor = '';
    b.style.color = '';
  });
  loadHallCompare();
});

async function loadTodayPickCard() {
  const card = document.getElementById('today-pick-card');
  const body = document.getElementById('today-pick-body');
  if (!card || !body) return;
  try {
    const today = new Date().toISOString().slice(0, 10);
    const hotData = await apiFetch('/api/hall/hot_days?months=1').catch(() => null);
    const todayHot = hotData?.hot_days?.filter(h => h.date === today) || [];
    todayHot.sort((a, b) => b.z_score - a.z_score);
    if (todayHot.length === 0) { card.style.display = 'none'; return; }
    card.style.display = 'block';
    body.innerHTML = todayHot.slice(0, 5).map(h => {
      const icon = h.z_score >= 2.0 ? '🔥🔥' : h.z_score >= 1.5 ? '🔥' : '・';
      const zCol = h.z_score >= 2.0 ? 'var(--warning)' : h.z_score >= 1.5 ? 'var(--success)' : 'var(--text3)';
      const diffSign = h.avg_diff >= 0 ? '+' : '';
      return `<div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04)">
        <span style="font-size:.9rem;flex-shrink:0">${icon}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:.78rem;font-weight:700;color:var(--text1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(h.hall_name)}</div>
          <div style="font-size:.6rem;color:var(--text3)">直近平均 <span style="color:${h.avg_diff >= 0 ? 'var(--success)' : 'var(--danger)'}">${diffSign}${h.avg_diff}枚</span></div>
        </div>
        <div style="text-align:right;flex-shrink:0">
          <div style="font-size:.7rem;color:${zCol};font-weight:700">${h.z_score != null ? h.z_score.toFixed(1) + 'σ' : ''}</div>
          <div style="font-size:.58rem;color:var(--text3)">${h.label || ''}</div>
        </div>
        <button onclick="event.stopPropagation();switchToHall(decodeURIComponent('${encodeURIComponent(h.hall_name)}'))" class="btn btn-ghost" style="font-size:.62rem;padding:2px 7px;flex-shrink:0">→</button>
      </div>`;
    }).join('');
  } catch(e) {
    const c = document.getElementById('today-pick-card');
    if (c) c.style.display = 'none';
  }
}

function _getFavHalls() {
  try { return JSON.parse(localStorage.getItem('pachi_fav_halls') || '[]'); } catch(e) { return []; }
}
function _isFavHall(hall) { return _getFavHalls().includes(hall); }
function _toggleFavHall(hall) {
  let favs = _getFavHalls();
  if (favs.includes(hall)) { favs = favs.filter(h => h !== hall); }
  else { favs.unshift(hall); }
  localStorage.setItem('pachi_fav_halls', JSON.stringify(favs));
  loadHallCompare();
}

async function loadHallCompare() {
  const card = document.getElementById('hall-compare-card');
  const body = document.getElementById('hall-compare-body');
  if (!card) return;
  card.style.display = 'block';
  body.innerHTML = loadingHtml(2);
  const sub = document.getElementById('compare-subtitle');
  if (sub) sub.textContent = `過去${_compareDays}日`;
  loadTodayPickCard();
  try {
    const rows = await apiFetch(`/api/hall/compare?days=${_compareDays}`);
    if (!rows || rows.length === 0) {
      body.innerHTML = `<div class="empty-hint">
        <span>📊</span>
        <strong style="color:var(--text2)">比較データなし</strong>
        <span>管理タブ → 全店舗実行 でスクレイプしてください</span>
        <button onclick="switchHallTab('admin')" class="btn btn-ghost btn-sm" style="margin-top:4px">管理タブへ</button>
      </div>`;
      return;
    }
    // お気に入りホールを先頭に固定
    const favs = _getFavHalls();
    if (favs.length > 0) {
      rows.sort((a, b) => {
        const af = favs.indexOf(a.hall_name), bf = favs.indexOf(b.hall_name);
        if (af !== -1 && bf === -1) return -1;
        if (bf !== -1 && af === -1) return 1;
        if (af !== -1 && bf !== -1) return af - bf;
        return 0;
      });
    }
    const maxAbs = Math.max(...rows.map(r => Math.abs(r.avg_diff)), 1);
    const sign = v => v >= 0 ? `+${v}` : `${v}`;
    // データ鮮度インジケータ
    const latestDates = rows.map(r => r.latest_date).filter(Boolean).sort();
    const newestDate = latestDates[latestDates.length - 1];
    const oldestActive = latestDates[0];
    let freshnessHtml = '';
    if (newestDate) {
      const diffDays = Math.round((Date.now() - new Date(newestDate).getTime()) / 86400000);
      const col = diffDays === 0 ? 'var(--success)' : diffDays <= 1 ? 'var(--warning)' : 'var(--danger)';
      const label = diffDays === 0 ? '本日更新済み' : `${diffDays}日前が最新`;
      freshnessHtml = `<div style="font-size:.62rem;color:${col};text-align:right;margin-bottom:6px;padding:0 2px">● ${label} (${rows.length}店舗)</div>`;
    }
    body.innerHTML = freshnessHtml + rows.map((r, i) => {
      const col = r.avg_diff >= 0 ? 'var(--success)' : 'var(--danger)';
      const pct = Math.round(Math.abs(r.avg_diff) / maxAbs * 100);
      const encH = encodeURIComponent(r.hall_name);
      let trendHtml = '';
      if (r.bb_trend_7d !== null && r.bb_trend_7d !== undefined) {
        const t = r.bb_trend_7d;
        const tc = t > 1 ? 'var(--success)' : t < -1 ? 'var(--danger)' : 'var(--text3)';
        const ta = t > 1 ? '↑' : t < -1 ? '↓' : '→';
        trendHtml = `<span style="color:${tc};font-size:.58rem;margin-left:4px">BB${ta}${t > 0 ? '+' : ''}${t}%</span>`;
      }
      const zCol = r.bb_z > 0.5 ? 'var(--success)' : r.bb_z < -0.5 ? 'var(--danger)' : 'var(--text3)';
      const surgeHtml = r.surge_seat_count > 0
        ? `<span style="color:var(--warning);font-size:.58rem;margin-left:4px">🔺${r.surge_seat_count}台急上昇</span>`
        : '';
      const eventHtml = r.today_event_z != null && r.today_event_z >= 0.5
        ? `<span style="color:var(--primary-h);font-size:.58rem;margin-left:4px">📅イベント候補 z=${r.today_event_z.toFixed(1)}σ</span>`
        : '';
      // 今日のおすすめスコア: 順位上位 + イベント候補 + BB上昇
      const todayScore = (i < 5 ? (5 - i) : 0)
        + (r.today_event_z != null && r.today_event_z >= 0.5 ? Math.round(r.today_event_z * 2) : 0)
        + (r.bb_trend_7d != null && r.bb_trend_7d > 2 ? 2 : 0)
        + (r.surge_seat_count > 0 ? 1 : 0);
      const todayBadge = todayScore >= 6
        ? `<span style="background:linear-gradient(90deg,rgba(251,191,36,.25),rgba(16,185,129,.2));border:1px solid rgba(251,191,36,.4);color:#fbbf24;font-size:.58rem;padding:1px 6px;border-radius:4px;font-weight:700;margin-left:4px">★今日</span>`
        : '';
      const srcBadge = r.data_source === 'minrepo'
        ? `<span style="font-size:.52rem;color:var(--text3);margin-left:3px">みんレポ</span>`
        : '';
      const bbLine = r.avg_bb != null
        ? `BB ${r.bb_z > 0 ? '+' : ''}${r.bb_z}σ` : 'みんレポデータ';
      const rankStyle = i === 0 ? 'background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.2);border-radius:10px;padding:7px 8px;margin-bottom:4px' :
                        i <= 2 ? 'border-bottom:1px solid var(--border);padding:7px 0' :
                                 'border-bottom:1px solid var(--border);padding:7px 0';
      const rankNumStyle = i === 0 ? 'font-size:.78rem;font-weight:900;color:#fbbf24;width:18px;text-align:center;flex-shrink:0' :
                           'font-size:.68rem;color:var(--text3);width:18px;text-align:center;flex-shrink:0';
      const isPinned = _isFavHall(r.hall_name);
      const copyText = `${i+1}位 ${r.hall_name} ${r.avg_diff >= 0 ? '+' : ''}${r.avg_diff}枚 (${r.days_data}日 勝率${r.win_rate}%)`;
      return `<div class="compare-row" style="display:flex;align-items:center;gap:8px;cursor:pointer;${rankStyle}"
        data-copy-rank="${esc(copyText)}"
        onclick="switchToHall(decodeURIComponent('${encH}'))">
        <button onclick="event.stopPropagation();_toggleFavHall(decodeURIComponent('${encH}'))" title="お気に入り"
          style="background:none;border:none;cursor:pointer;font-size:.85rem;padding:0;flex-shrink:0;line-height:1;opacity:${isPinned ? '1' : '0.3'}">${isPinned ? '★' : '☆'}</button>
        <span style="${rankNumStyle}">${i+1}</span>
        <div style="flex:1;min-width:0">
          <div style="font-size:.82rem;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(r.hall_name)}${srcBadge}${todayBadge}${trendHtml}${surgeHtml}${eventHtml}</div>
          <div style="height:3px;background:var(--bg3);border-radius:2px;margin-top:3px">
            <div class="anim-bar" style="width:${pct}%;height:100%;background:${col};border-radius:2px"></div>
          </div>
          <div style="font-size:.58rem;color:var(--text3);margin-top:2px">${r.days_data}日 ${r.machine_count}機種 ${r.record_count}件 勝率${r.win_rate}% <span style="color:${zCol}">${bbLine}</span></div>
        </div>
        <div style="text-align:right;flex-shrink:0">
          <div style="font-weight:900;color:${col};font-size:.92rem">${sign(r.avg_diff)}枚</div>
          <div style="font-size:.6rem;color:var(--text3)">${r.latest_date || ''}</div>
        </div>
      </div>`;
    }).join('');

    // ルールベースのインサイト
    const insightLines = [];
    const topHall = rows[0];
    const eventCandidates = rows.filter(r => r.today_event_z != null && r.today_event_z >= 0.5);
    const surgeHalls = rows.filter(r => r.surge_seat_count > 0);
    const risingHalls = rows.filter(r => r.bb_trend_7d != null && r.bb_trend_7d > 2);
    if (eventCandidates.length > 0)
      insightLines.push(`📅 今日イベント候補: ${eventCandidates.slice(0,2).map(r=>r.hall_name).join(' / ')}`);
    if (surgeHalls.length > 0)
      insightLines.push(`🔺 BB急上昇台あり: ${surgeHalls.slice(0,2).map(r=>r.hall_name).join(' / ')}`);
    if (risingHalls.length > 0)
      insightLines.push(`↑ BB上昇トレンド: ${risingHalls.slice(0,2).map(r=>r.hall_name).join(' / ')}`);
    if (topHall && topHall.avg_diff > 0)
      insightLines.push(`🏆 直近${_compareDays}日トップ: ${topHall.hall_name} (${topHall.avg_diff >= 0 ? '+' : ''}${topHall.avg_diff}枚)`);
    if (insightLines.length > 0) {
      body.innerHTML += `<div style="margin-top:10px;padding:8px 10px;background:var(--bg2);border:1px solid var(--border);border-radius:8px">
        <div style="font-size:.6rem;color:var(--text3);margin-bottom:4px;font-weight:600">今日のポイント</div>
        ${insightLines.map(l => `<div style="font-size:.72rem;color:var(--text2);padding:1px 0">${l}</div>`).join('')}
      </div>`;
    }
  } catch(e) {
    body.innerHTML = `<div style="color:var(--danger);font-size:.8rem;padding:12px">エラー: ${e.message}</div>`;
  }
}

async function loadCrossHallCompare() {
  const machine = document.getElementById('cross-machine-input')?.value?.trim();
  const body = document.getElementById('cross-hall-body');
  if (!machine || !body) return;
  body.innerHTML = loadingHtml(2);
  try {
    const rows = await apiFetch(`/api/machine/cross_hall?machine_name=${encodeURIComponent(machine)}&days=30`);
    if (!rows || rows.length === 0) {
      body.innerHTML = `<div class="empty-hint"><span>🎰</span><span>「${esc(machine)}」のデータなし（30日以内）</span></div>`;
      return;
    }
    const sign = v => v >= 0 ? `+${v}` : `${v}`;
    body.innerHTML = `<div style="font-size:.62rem;color:var(--text3);margin-bottom:6px">「${esc(machine)}」— 店舗別平均差枚 (過去30日)</div>`
      + rows.map((r, i) => {
        const col = r.avg_diff >= 100 ? 'var(--success)' : r.avg_diff >= 0 ? 'var(--warning)' : 'var(--danger)';
        const icon = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : `${i+1}`;
        const enc = encodeURIComponent(r.hall_name);
        return `<div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;cursor:pointer;padding:3px 0" onclick="switchToHall(decodeURIComponent('${enc}'))">
          <span style="font-size:${i<3?'.82':'.7'}rem;width:20px;text-align:center;flex-shrink:0">${icon}</span>
          <div style="flex:1;min-width:0">
            <div style="font-size:.74rem;font-weight:${i<3?700:400};overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.hall_name)}</div>
            <div style="height:3px;background:var(--bg3);border-radius:2px;margin-top:2px">
              <div style="width:${r.bar_pct}%;height:100%;background:${col};border-radius:2px"></div>
            </div>
          </div>
          <div style="text-align:right;flex-shrink:0">
            <div style="font-size:.78rem;font-weight:700;color:${col}">${sign(r.avg_diff)}枚</div>
            <div style="font-size:.58rem;color:var(--text3)">${r.days_count}日</div>
          </div>
        </div>`;
      }).join('');
  } catch(e) {
    body.innerHTML = `<div style="color:var(--danger);font-size:.75rem">取得失敗</div>`;
  }
}
window.loadCrossHallCompare = loadCrossHallCompare;

// Enterキーで機種横断比較
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && document.activeElement?.id === 'cross-machine-input') {
    loadCrossHallCompare();
  }
});

// 比較ランキングをテキストでコピー
window.copyCompareRanking = function() {
  const body = document.getElementById('hall-compare-body');
  if (!body) return;
  const items = body.querySelectorAll('[data-copy-rank]');
  if (!items.length) { showToast('コピーできるデータなし', 'error'); return; }
  const lines = Array.from(items).map(el => el.dataset.copyRank).join('\n');
  navigator.clipboard.writeText(lines).then(() => showToast('ランキングをコピーしました'));
};

// 比較ランキングからホール詳細へ遷移
window.switchToHall = function(hallName) {
  const sel = document.getElementById('hall-select');
  let found = false;
  for (const opt of sel.options) {
    if (opt.value === hallName) { sel.value = hallName; found = true; break; }
  }
  if (!found) {
    sel.value = '__custom__';
    const ci = document.getElementById('hall-custom-input');
    ci.style.display = '';
    ci.value = hallName;
  }
  switchTab('hall');
  switchHallTab('detail'); // calls loadHallPage() internally
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

  // ホール選択を同期（全店舗オプションは残す）
  const hallSel = document.getElementById('hall-select');
  const aiHallSel = document.getElementById('ai-hall-select');
  if (hallSel && aiHallSel) {
    // 既存のホールオプションを追加（全店舗は固定で先頭）
    const existing = Array.from(hallSel.options).map(o => o.value).filter(Boolean);
    // 全店舗は必ず先頭
    aiHallSel.innerHTML = '<option value="全店舗">全店舗比較</option>';
    existing.forEach(v => {
      const opt = document.createElement('option');
      opt.value = v;
      opt.textContent = v;
      aiHallSel.appendChild(opt);
    });
    // 現在の hall-select の値を反映（全店舗がデフォルト）
    const cur = hallSel.value;
    if (cur && existing.includes(cur)) aiHallSel.value = cur;
    else aiHallSel.value = '全店舗';
  }
  _updateAiScopeUI();
}

function getAiHall() {
  const sel = document.getElementById('ai-hall-select');
  const v = sel ? sel.value : '全店舗';
  return v === '全店舗' ? '全店舗' : v;
}

function _updateAiScopeUI() {
  const hall = getAiHall();
  const isAll = hall === '全店舗';
  const scopeBadge = document.getElementById('ai-scope-badge');
  const reportTitle = document.getElementById('ai-report-title');
  const reportSubtitle = document.getElementById('ai-report-subtitle');
  const chatInput = document.getElementById('ai-chat-input');
  const quickAll = document.getElementById('ai-quick-all');
  const quickHall = document.getElementById('ai-quick-hall');

  if (isAll) {
    if (scopeBadge) scopeBadge.textContent = '全店舗のデータを横断分析します';
    if (reportTitle) reportTitle.textContent = '今日の攻略レポート（全店舗比較）';
    if (reportSubtitle) reportSubtitle.textContent = '全店舗比較・イベント候補・おすすめ店舗';
    if (chatInput) chatInput.placeholder = '例: 今日どの店に行くべき？';
    if (quickAll) quickAll.style.display = 'flex';
    if (quickHall) quickHall.style.display = 'none';
  } else {
    if (scopeBadge) scopeBadge.textContent = `${hall} のデータを分析します`;
    if (reportTitle) reportTitle.textContent = '今日の攻略レポート';
    if (reportSubtitle) reportSubtitle.textContent = '曜日傾向・台番・末尾データを総合分析';
    if (chatInput) chatInput.placeholder = '例: どの台番が一番強い？';
    if (quickAll) quickAll.style.display = 'none';
    if (quickHall) quickHall.style.display = 'flex';
  }
}

// ホール選択が変わったらUIを更新
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('ai-hall-select')?.addEventListener('change', _updateAiScopeUI);
});

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
      const txt = data.report || '';
      out.innerHTML = txt
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/^###?\s+(.+)$/gm, '<strong style="color:var(--text1);display:block;margin:10px 0 4px">$1</strong>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\n\n/g, '<br><br>')
        .replace(/^[-•]\s+(.+)$/gm, '• $1<br>');
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


// ============================================================
// 店傾向 AI分析
// ============================================================

document.getElementById('hall-ai-btn').addEventListener('click', async () => {
  const hall = getSelectedHall();
  if (!hall) { showToast('ホールを選択してください', 'error'); return; }
  const btn = document.getElementById('hall-ai-btn');
  const out = document.getElementById('hall-ai-output');
  btn.disabled = true;
  btn.textContent = '生成中...';
  out.style.display = 'block';
  out.textContent = '分析中...';
  try {
    const r = await apiFetch(`/api/ai/report?hall_name=${encodeURIComponent(hall)}`);
    out.textContent = r.report || 'データが不足しています。スクレイプ後に再試行してください。';
  } catch(e) {
    out.textContent = 'AI分析失敗: ' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = '生成';
  }
});

// ============================================================
// スクレイプ管理パネル
// ============================================================

async function loadScrapeManager() {
  loadCacheStats();
  loadArchiveCollector();
  // DBデータ統計サマリー
  try {
    const st = await apiFetch('/api/stats').catch(() => null);
    const statsEl = document.getElementById('db-stats-card');
    if (st && statsEl) {
      const items = [
        { icon: '🌐', label: 'みんレポ', value: `${st.minrepo_days||0}日`, sub: `${st.minrepo_halls||0}店舗` },
        { icon: '🎰', label: 'アナスロ', value: `${st.anaslo_days||0}日`, sub: st.latest_date ? `最新 ${st.latest_date}` : '未取得' },
        { icon: '📅', label: 'イベント', value: `${st.event_count||0}件`, sub: `${st.event_halls||0}店舗` },
      ];
      statsEl.innerHTML = items.map(i => `
        <div style="background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:10px;text-align:center">
          <div style="font-size:1.1rem;margin-bottom:2px">${i.icon}</div>
          <div style="font-size:.62rem;color:var(--text3);margin-bottom:2px">${i.label}</div>
          <div style="font-size:1rem;font-weight:800;color:var(--text1)">${i.value}</div>
          <div style="font-size:.6rem;color:var(--text3)">${i.sub}</div>
        </div>`).join('');
    }
  } catch(e) {}

  try {
    const [status, cookieSt] = await Promise.all([
      apiFetch('/api/scrape/status').catch(() => null),
      apiFetch('/api/scrape/cookie_status').catch(() => null),
    ]);
    // ホール一覧
    loadScrapeHalls();

    // Cookie状態バッジ（期限・CFブロック警告含む）
    const badge = document.getElementById('cookie-status-badge');
    if (badge && cookieSt) {
      const age = cookieSt.age_hours;
      const ageStr = age != null
        ? (age < 1 ? `${Math.round(age * 60)}分前` : `${age}時間前`)
        : '';
      const ageWarnColor = age == null ? '' : age >= 24 ? 'var(--danger)' : age >= 6 ? 'var(--warning)' : 'var(--text3)';
      const ageHtml = ageStr
        ? ` <span style="color:${ageWarnColor};font-size:.75rem">(${ageStr}に登録${age >= 6 ? ' — 期限切れの可能性' : ''})</span>`
        : '';

      if (cookieSt.curl_cffi_available) {
        // curl_cffi があれば Cookie 不要で自動突破
        const cfBlockedRecently = (cookieSt.cf_blocked_halls || []).length > 0;
        if (cfBlockedRecently) {
          badge.innerHTML = `<span style="color:var(--warning)">⚡ curl_cffi 自動突破モード（一部CF検知あり）</span>`;
        } else {
          badge.innerHTML = `<span style="color:var(--success)">⚡ curl_cffi 自動突破モード — Cookie不要</span>`;
        }
      } else if (cookieSt.has_cf_clearance) {
        const iconColor = age != null && age >= 24 ? 'var(--warning)' : 'var(--success)';
        badge.innerHTML = `<span style="color:${iconColor}">✓ cf_clearance登録済み</span>${ageHtml}`;
      } else if (cookieSt.has_cookie) {
        badge.innerHTML = `<span style="color:var(--warning)">⚠ Cookie登録済み（cf_clearanceなし）</span>${ageHtml}`;
      } else {
        badge.innerHTML = '<span style="color:var(--danger)">✗ Cookie未登録 — curl-cffi をインストールするとCookie不要になります</span>';
      }

      // CFブロック警告バナー
      const alertEl = document.getElementById('cookie-alert');
      if (alertEl) {
        const blocked = cookieSt.cf_blocked_halls || [];
        if (blocked.length > 0) {
          alertEl.style.display = 'block';
          alertEl.innerHTML = `🚫 <strong>Cloudflareブロック検知</strong>: ${blocked.slice(0,3).map(h => esc(h)).join('、')}${blocked.length > 3 ? `他${blocked.length-3}店` : ''} — <strong>Cookieを更新してください</strong>`;
        } else {
          alertEl.style.display = 'none';
        }
      }
    }

    // スケジューラー情報
    const schedEl = document.getElementById('scrape-scheduler-info');
    if (schedEl && status) {
      const running = status.scrape_running ? '<span style="color:var(--warning)"> (スクレイプ実行中)</span>' : '';
      const next = status.next_scheduled_run
        ? `次回自動実行: ${new Date(status.next_scheduled_run).toLocaleString('ja-JP')}` : '次回実行未定';
      const sched = status.scheduler_running
        ? `<span style="color:var(--success)">● スケジューラー稼働中</span> — ${next}${running}`
        : `<span style="color:var(--danger)">● スケジューラー停止中</span>`;
      schedEl.innerHTML = sched + `<span style="color:var(--text3);margin-left:8px">対象${status.total_halls || 0}店舗</span>`;
    }

    // 実行ログ
    const logEl = document.getElementById('scrape-log-table');
    if (logEl && status && status.recent_logs) {
      const allLogs = status.recent_logs;
      if (allLogs.length === 0) {
        logEl.innerHTML = '<span style="color:var(--text3)">実行ログなし</span>';
      } else {
        const renderLogs = (logs) => logs.map(l => {
          const stCol = l.status === 'done' ? 'var(--success)' : l.status === 'running' ? 'var(--warning)' : 'var(--danger)';
          const stIcon = l.status === 'done' ? '✓' : l.status === 'running' ? '⟳' : (l.status === 'cf_blocked' ? '⛔' : '✗');
          const time = l.started_at ? l.started_at.slice(5, 16).replace('T', ' ') : '';
          const rowsBadge = l.rows_saved > 0
            ? `<span style="background:rgba(52,211,153,.15);color:var(--success);font-size:.6rem;padding:1px 5px;border-radius:4px">${l.rows_saved}件</span>`
            : `<span style="color:var(--text3);font-size:.62rem">0件</span>`;
          const errTip = l.error_msg ? ` title="${esc(l.error_msg)}"` : '';
          const errDetail = l.error_msg && l.status !== 'done' && l.status !== 'running'
            ? `<div style="font-size:.58rem;color:var(--danger);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px;margin-top:1px">${esc(l.error_msg.slice(0, 60))}</div>`
            : '';
          return `<div${errTip} style="display:flex;gap:6px;align-items:center;padding:4px 0;border-bottom:1px solid var(--border);flex-wrap:wrap">
            <span style="color:${stCol};font-size:.78rem;width:14px;text-align:center;flex-shrink:0">${stIcon}</span>
            <div style="flex:1;min-width:0">
              <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.75rem">${esc(l.hall_name)}</div>
              ${errDetail}
            </div>
            ${rowsBadge}
            <span style="color:var(--text3);flex-shrink:0;font-size:.62rem">${time}</span>
          </div>`;
        }).join('');
        const shown = allLogs.slice(0, 15);
        const moreBtn = allLogs.length > 15
          ? `<button onclick="this.previousSibling.innerHTML += ${JSON.stringify(renderLogs(allLogs.slice(15)))};this.remove()" class="btn btn-ghost" style="font-size:.62rem;padding:3px 10px;margin-top:4px;width:100%">もっと見る (${allLogs.length - 15}件)</button>`
          : '';
        logEl.innerHTML = `<div>${renderLogs(shown)}</div>${moreBtn}`;
      }
    }

    // 実行中ならポーリング
    if (status && status.scrape_running) {
      setTimeout(() => loadScrapeManager(), 5000);
    }
  } catch(e) {
    console.error('scrape manager load error:', e);
  }
}

let _archivePollTimer = null;

function _archiveDateOffset(days) {
  const value = new Date();
  value.setDate(value.getDate() + days);
  return value.toISOString().slice(0, 10);
}

async function loadArchiveCollector() {
  const statusEl = document.getElementById('archive-status');
  if (!statusEl) return;
  try {
    const data = await apiFetch('/api/scrape/archive/status');
    const hallSelect = document.getElementById('archive-hall');
    const previousHall = hallSelect.value;
    hallSelect.innerHTML = (data.supported_halls || []).map(item =>
      `<option value="${esc(item.hall_name)}">${esc(item.hall_name)}</option>`
    ).join('');
    if (previousHall && [...hallSelect.options].some(option => option.value === previousHall)) hallSelect.value = previousHall;
    const fromInput = document.getElementById('archive-date-from');
    const toInput = document.getElementById('archive-date-to');
    if (!fromInput.value) fromInput.value = _archiveDateOffset(-365);
    if (!toInput.value) toInput.value = _archiveDateOffset(-1);

    const job = data.job;
    if (!job) {
      statusEl.innerHTML = 'まだ過去データ収集は実行されていません。店舗と期間を選んで開始してください。';
    } else {
      const labels = { queued: '開始待ち', collecting: '収集中', paused: '一時停止', completed: '完了', failed: '失敗' };
      const tone = job.status === 'completed' ? 'var(--success)' : job.status === 'paused' ? 'var(--warning)' : job.status === 'collecting' ? 'var(--primary-h)' : 'var(--text2)';
      statusEl.innerHTML = `<strong style="color:${tone}">${labels[job.status] || esc(job.status)}</strong>　${esc(job.hall_name)} ${esc(job.date_from)}〜${esc(job.date_to)}<br>` +
        `処理 ${job.processed}/${job.discovered}ページ（${job.progress_pct}%）・新規 機種${job.machine_rows}件 / 台${job.seat_rows}件・失敗${job.failed_count}件` +
        (job.error ? `<br><span style="color:var(--warning)">${esc(job.error)}</span>` : '');
    }
    const coverage = document.getElementById('archive-coverage');
    coverage.innerHTML = (data.coverage || []).map(item =>
      `${esc(item.hall_name)}：${item.days || 0}日${item.oldest ? `（${esc(item.oldest)}〜${esc(item.newest)}）` : ''}`
    ).join('<br>');
    const running = job && ['queued', 'collecting'].includes(job.status);
    document.getElementById('archive-start-btn').disabled = Boolean(running || job?.status === 'paused');
    document.getElementById('archive-pause-btn').disabled = !running;
    document.getElementById('archive-resume-btn').disabled = job?.status !== 'paused';
    clearTimeout(_archivePollTimer);
    if (running) _archivePollTimer = setTimeout(loadArchiveCollector, 3000);
  } catch (error) {
    statusEl.innerHTML = `<span style="color:var(--danger)">進捗取得に失敗：${esc(error.message)}</span>`;
  }
}

document.getElementById('archive-start-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('archive-start-btn');
  const body = {
    hall_name: document.getElementById('archive-hall').value,
    date_from: document.getElementById('archive-date-from').value,
    date_to: document.getElementById('archive-date-to').value,
    max_pages: Number(document.getElementById('archive-max-pages').value),
  };
  if (!body.hall_name || !body.date_from || !body.date_to) return showToast('店舗と期間を指定してください', 'error');
  btn.disabled = true;
  try {
    await apiFetch('/api/scrape/archive/jobs', { method: 'POST', body: JSON.stringify(body) });
    showToast('過去データ収集を開始しました');
    await loadArchiveCollector();
  } catch (error) {
    showToast(error.message, 'error');
    btn.disabled = false;
  }
});

document.getElementById('archive-pause-btn')?.addEventListener('click', async () => {
  try {
    const data = await apiFetch('/api/scrape/archive/pause', { method: 'POST' });
    showToast(data.message, data.ok ? 'success' : 'error');
    setTimeout(loadArchiveCollector, 500);
  } catch (error) { showToast(error.message, 'error'); }
});

document.getElementById('archive-resume-btn')?.addEventListener('click', async () => {
  try {
    const data = await apiFetch('/api/scrape/archive/resume', { method: 'POST' });
    showToast(data.message, data.ok ? 'success' : 'error');
    await loadArchiveCollector();
  } catch (error) { showToast(error.message, 'error'); }
});

async function loadCacheStats() {
  const el = document.getElementById('cache-stats-text');
  if (!el) return;
  try {
    const d = await apiFetch('/api/cache/stats').catch(() => null);
    if (!d) { el.textContent = 'キャッシュ統計取得失敗'; return; }
    el.textContent = `キャッシュ: ${d.count}件 / TTL${d.ttl_seconds}s / ${d.memory_kb_estimate}KB推定`;
  } catch(e) {
    el.textContent = 'キャッシュ: -';
  }
}

window.clearApiCache = async function() {
  try {
    const r = await fetch('/api/cache/clear', { method: 'POST' }).then(r => r.json()).catch(() => null);
    showToast(r ? `キャッシュをクリアしました (${r.cleared}件)` : 'クリア失敗', r ? 'success' : 'error');
    loadCacheStats();
  } catch(e) {
    showToast('クリア失敗: ' + e.message, 'error');
  }
};

// Cookie保存ボタン
document.getElementById('save-cookie-btn')?.addEventListener('click', async () => {
  const input = document.getElementById('cookie-input');
  const btn = document.getElementById('save-cookie-btn');
  const badge = document.getElementById('cookie-status-badge');
  if (!input || !input.value.trim()) { showToast('Cookieを入力してください', 'error'); return; }
  btn.disabled = true;
  btn.textContent = '保存中...';
  try {
    const res = await fetch('/api/scrape/cookie', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cookie_str: input.value.trim() }),
    }).then(r => r.json());
    if (res.ok) {
      showToast(res.message || 'Cookie保存完了');
      input.value = '';
      await loadScrapeManager();
    } else {
      showToast('保存失敗: ' + (res.detail || JSON.stringify(res)), 'error');
    }
  } catch(e) {
    showToast('エラー: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Cookie保存';
  }
});

// 手動スクレイプ実行ボタン
// ============================================================
// カレンダータブ
// ============================================================

let _calYear  = new Date().getFullYear();
let _calMonth = new Date().getMonth();
let _calSelectedDate = null;
let _calHallFilter = '';
let _calEventMap  = {};  // dateStr → [{event}]
let _calHeatMap   = {};  // dateStr → {avg_diff, hall_count, halls:[]}
let _calHotDays   = {};  // dateStr → [{hall_name, z_score, label}]  (自動検出)
let _calDrillDate = null;
let _calDrillHall = null;

const EVENT_TYPE_ORDER = ['特日7','特日ゾロ目','新台入替','感謝デー','高設定示唆','通常イベント','その他'];

function _ym() {
  return `${_calYear}-${String(_calMonth + 1).padStart(2, '0')}`;
}

function _heatClass(diff) {
  if (diff === undefined || diff === null) return '';
  if (diff >  200) return 'heat-hot';
  if (diff >   50) return 'heat-warm';
  if (diff <  -200) return 'heat-cold';
  if (diff <  -50) return 'heat-cool';
  return '';
}

async function loadCalendar() {
  document.getElementById('cal-month-label').textContent =
    `${_calYear}年${_calMonth + 1}月`;
  const hallFilter = document.getElementById('cal-hall-filter')?.value || '';
  _calHallFilter = hallFilter;
  const ym = _ym();
  const evUrl = `/api/events/calendar?month=${ym}` + (hallFilter ? `&hall_name=${encodeURIComponent(hallFilter)}` : '');
  const hallHotParam = hallFilter ? `&hall_name=${encodeURIComponent(hallFilter)}` : '';
  const [evData, hmData, hotData] = await Promise.all([
    fetch(evUrl).then(r => r.json()).catch(() => ({events:{}})),
    fetch(`/api/hall/month_heatmap?month=${ym}`).then(r => r.json()).catch(() => ({days:{}})),
    fetch(`/api/hall/hot_days?months=3${hallHotParam}`).then(r => r.json()).catch(() => ({hot_days:[]})),
  ]);
  _calEventMap = evData.events || {};
  _calHeatMap  = hmData.days  || {};
  _calHotDays  = {};
  for (const h of (hotData.hot_days || [])) {
    if (h.date.startsWith(ym)) {
      _calHotDays[h.date] = _calHotDays[h.date] || [];
      _calHotDays[h.date].push(h);
    }
  }
  _renderCalGrid();
  loadCalStrength();
  _renderCalMonthStats();
}

function _renderCalMonthStats() {
  const el = document.getElementById('cal-month-stats');
  if (!el) return;
  const hotEntries = Object.entries(_calHotDays);
  const hotDayCount = hotEntries.length;
  if (hotDayCount === 0) { el.style.display = 'none'; return; }
  const topDay = hotEntries.reduce((best, [date, halls]) => {
    const topZ = Math.max(...halls.map(h => h.z_score || 0));
    return topZ > (best.z || 0) ? { date, z: topZ, hall: halls[0].hall_name } : best;
  }, {});
  const heatCount = Object.values(_calHeatMap).filter(v => v > 0).length;
  el.style.display = 'block';
  el.innerHTML = `<div style="display:flex;gap:10px;flex-wrap:wrap;font-size:.7rem">
    <span style="background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.2);border-radius:5px;padding:2px 8px">
      🔥 ホット日 <strong style="color:#fbbf24">${hotDayCount}日</strong>
    </span>
    ${heatCount ? `<span style="background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.2);border-radius:5px;padding:2px 8px">
      📊 出玉+ <strong style="color:var(--success)">${heatCount}日</strong>
    </span>` : ''}
    ${topDay.date ? `<span style="background:rgba(124,127,245,.1);border:1px solid rgba(124,127,245,.2);border-radius:5px;padding:2px 8px">
      ⭐ 最強日 <strong style="color:var(--primary-h)">${topDay.date.slice(5)}</strong>
      <span style="color:var(--text3)">(${topDay.z?.toFixed(1)}σ)</span>
    </span>` : ''}
  </div>`;
}

function _renderCalGrid() {
  const grid = document.getElementById('cal-grid');
  if (!grid) return;
  const firstDay   = new Date(_calYear, _calMonth, 1).getDay();
  const daysInMonth = new Date(_calYear, _calMonth + 1, 0).getDate();
  const daysInPrev  = new Date(_calYear, _calMonth, 0).getDate();
  const today = new Date().toISOString().slice(0, 10);
  const totalCells = Math.ceil((firstDay + daysInMonth) / 7) * 7;
  let html = '';
  for (let i = 0; i < totalCells; i++) {
    let day, month, year, isOther = false;
    if (i < firstDay) {
      day = daysInPrev - firstDay + i + 1;
      month = _calMonth === 0 ? 12 : _calMonth;
      year  = _calMonth === 0 ? _calYear - 1 : _calYear;
      isOther = true;
    } else if (i >= firstDay + daysInMonth) {
      day = i - firstDay - daysInMonth + 1;
      month = _calMonth === 11 ? 1 : _calMonth + 2;
      year  = _calMonth === 11 ? _calYear + 1 : _calYear;
      isOther = true;
    } else {
      day = i - firstDay + 1; month = _calMonth + 1; year = _calYear;
    }
    const dateStr = `${year}-${String(month).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
    const evs  = _calEventMap[dateStr] || [];
    const heat = _calHeatMap[dateStr];
    const heatCls = isOther ? '' : _heatClass(heat?.avg_diff);
    const cls = ['cal-cell', isOther?'other-month':'', dateStr===today?'today':'',
      dateStr===_calSelectedDate?'selected':'', evs.length?'has-event':'', heatCls]
      .filter(Boolean).join(' ');
    const dots = [...new Set(evs.map(e => e.event_type))]
      .map(t => `<span class="cal-dot cal-dot-${t}" title="${t}"></span>`).join('');
    // 自動検出ホット日
    const hotEntries = !isOther ? (_calHotDays[dateStr] || []) : [];
    const topHot = hotEntries.sort((a,b) => b.z_score - a.z_score)[0];
    const hotBadge = topHot
      ? `<span class="cal-hot-badge" title="${topHot.hall_name} z=${topHot.z_score}">${topHot.label === '超熱' ? '🔥🔥' : topHot.label === '熱' ? '🔥' : '・'}</span>`
      : '';
    // ホール数バッジ
    const hallBadge = (!isOther && heat && heat.hall_count > 0)
      ? `<span class="cal-hall-badge">${heat.hall_count}店</span>` : '';
    html += `<div class="${cls}" onclick="selectCalDay('${dateStr}')" style="position:relative">
      ${hallBadge}
      <span class="cal-day-num">${day}</span>
      ${hotBadge}
      <div class="cal-dots">${dots}</div>
    </div>`;
  }
  grid.innerHTML = html;
}

// ── L1: 日付選択 → L2ドリルダウン（ホールバー）を開く ──
async function selectCalDay(dateStr) {
  _calSelectedDate = dateStr;
  _calDrillDate = dateStr;
  _calDrillHall = null;
  _renderCalGrid();

  // イベントパネル更新
  const detail = document.getElementById('cal-day-detail');
  document.getElementById('cal-detail-date').textContent =
    dateStr.replace(/-/g, '/') + ' のイベント';
  detail.style.display = 'block';
  _renderDayEvents(dateStr);

  // ドリルダウンL2を開く
  const drill = document.getElementById('cal-drill');
  drill.style.display = 'block';
  document.getElementById('cal-drill-back').style.display = 'none';
  document.getElementById('cal-drill-crumb').textContent =
    dateStr.replace(/-/g, '/') + ' — 店舗別出玉';
  document.getElementById('cal-drill-machines').style.display = 'none';
  const hallsEl = document.getElementById('cal-drill-halls');
  hallsEl.style.display = 'block';
  hallsEl.innerHTML = '<div style="font-size:.72rem;color:var(--text3);padding:8px 0">読み込み中...</div>';

  const heat = _calHeatMap[dateStr];
  if (heat && heat.halls && heat.halls.length) {
    _renderHallBars(heat.halls, dateStr);
  } else {
    // APIから直接取得
    try {
      const d = await fetch(`/api/events/day?date_str=${dateStr}`).then(r => r.json());
      if (d.results && d.results.length) {
        // hall_day_machineは台ごとなのでホール別に集計
        const byHall = {};
        for (const r of d.results) {
          if (!byHall[r.hall_name]) byHall[r.hall_name] = {diffs: [], mc: 0};
          byHall[r.hall_name].diffs.push(r.avg_diff_coins);
          byHall[r.hall_name].mc++;
        }
        const halls = Object.entries(byHall).map(([name, v]) => ({
          hall_name: name,
          avg_diff: Math.round(v.diffs.reduce((a,b)=>a+b,0)/v.diffs.length),
          machine_count: v.mc,
        })).sort((a,b) => b.avg_diff - a.avg_diff);
        _renderHallBars(halls, dateStr);
      } else {
        hallsEl.innerHTML = `<div class="empty-hint" style="padding:8px 0">
        <span>📊</span>
        <span>${dateStr.replace(/-/g,'/')} のみんレポデータなし</span>
        <button onclick="switchHallTab('admin')" class="btn btn-ghost btn-sm" style="margin-top:4px">管理タブでスクレイプ</button>
      </div>`;
      }
    } catch(e) {
      hallsEl.innerHTML = '<div style="font-size:.72rem;color:var(--danger)">取得エラー</div>';
    }
  }
}

function _renderHallBars(halls, dateStr) {
  const el = document.getElementById('cal-drill-halls');
  if (!halls.length) {
    el.innerHTML = '<div class="empty-hint">この日のホールデータがありません</div>';
    return;
  }
  const maxAbs = Math.max(...halls.map(h => Math.abs(h.avg_diff)), 1);
  const items = halls.map((h, i) => {
    const pct = Math.round(Math.abs(h.avg_diff) / maxAbs * 100);
    const pos = h.avg_diff >= 0;
    const col  = pos ? 'var(--success)' : 'var(--danger)';
    const sign = pos ? '+' : '';
    const mc   = h.machine_count ? `<span class="drill-bar-sub">${h.machine_count}機種</span>` : '';
    const rankBg = i === 0 ? 'rgba(251,191,36,.15)' : i === 1 ? 'rgba(180,180,180,.08)' : i === 2 ? 'rgba(200,120,80,.08)' : '';
    return `<div class="drill-bar-item" data-hall="${esc(h.hall_name)}" data-date="${dateStr}" style="background:${rankBg}">
      <span class="drill-bar-rank">${i+1}</span>
      <div class="drill-bar-name" title="${esc(h.hall_name)}">${esc(h.hall_name)}</div>
      <div class="drill-bar-track">
        <div class="drill-bar-fill ${pos?'pos':'neg'}" style="width:${pct}%"></div>
      </div>
      ${mc}
      <div class="drill-bar-val" style="color:${col}">${sign}${h.avg_diff}</div>
    </div>`;
  }).join('');
  el.innerHTML = items;
  // イベント委譲でクリック処理
  el.querySelectorAll('.drill-bar-item').forEach(item => {
    item.addEventListener('click', () => calDrillHall(item.dataset.date, item.dataset.hall));
  });
}

// ── L2: ホールクリック → L3（台別バー）──
async function calDrillHall(dateStr, hallName) {
  _calDrillHall = hallName;
  document.getElementById('cal-drill-back').style.display = 'inline';
  document.getElementById('cal-drill-crumb').textContent =
    `${dateStr.replace(/-/g,'/')} › ${hallName}`;
  document.getElementById('cal-drill-halls').style.display = 'none';
  const machEl = document.getElementById('cal-drill-machines');
  machEl.style.display = 'block';
  machEl.innerHTML = '<div style="font-size:.72rem;color:var(--text3);padding:8px 0">読み込み中...</div>';
  try {
    const rows = await fetch(
      `/api/hall/day_machines?date_str=${dateStr}&hall_name=${encodeURIComponent(hallName)}`
    ).then(r => r.json());
    if (!rows.length) {
      machEl.innerHTML = '<div style="font-size:.72rem;color:var(--text3);padding:8px 0">台別データなし</div>';
      return;
    }
    const maxAbs = Math.max(...rows.map(r => Math.abs(r.avg_diff)), 1);
    machEl.innerHTML = rows.map(r => {
      const pct = Math.round(Math.abs(r.avg_diff) / maxAbs * 100);
      const pos = r.avg_diff >= 0;
      const col = pos ? 'var(--success)' : 'var(--danger)';
      const sign = pos ? '+' : '';
      const sub = [
        r.unit_count ? `${r.unit_count}台` : '',
        r.avg_games  ? `${Math.round(r.avg_games)}G` : '',
        r.win_rate_pct ? `勝率${Math.round(r.win_rate_pct)}%` : '',
      ].filter(Boolean).join(' ');
      const encM = encodeURIComponent(r.machine_name);
      const encH = encodeURIComponent(hallName);
      return `<div class="drill-bar-item"
        onclick="switchToHall(decodeURIComponent('${encH}'));setTimeout(()=>renderMachineTrendChart(decodeURIComponent('${encH}'),decodeURIComponent('${encM}')),400)"
        style="cursor:pointer" title="${esc(r.machine_name)} — クリックでトレンド表示">
        <div class="drill-bar-name" style="flex:0 0 110px;font-size:.72rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.machine_name)}</div>
        <div class="drill-bar-track">
          <div class="drill-bar-fill ${pos?'pos':'neg'}" style="width:${pct}%"></div>
        </div>
        <div class="drill-bar-sub" style="min-width:56px;text-align:right;font-size:.58rem;color:var(--text3)">${sub}</div>
        <div class="drill-bar-val" style="color:${col}">${sign}${r.avg_diff}</div>
      </div>`;
    }).join('');
  } catch(e) {
    machEl.innerHTML = `<div style="font-size:.72rem;color:var(--danger)">取得エラー: ${e.message}</div>`;
  }
}

// ── ドリル戻る ──
function calDrillBack() {
  _calDrillHall = null;
  document.getElementById('cal-drill-back').style.display = 'none';
  document.getElementById('cal-drill-crumb').textContent =
    (_calDrillDate||'').replace(/-/g,'/') + ' — 店舗別出玉';
  document.getElementById('cal-drill-machines').style.display = 'none';
  document.getElementById('cal-drill-halls').style.display = 'block';
}

function _renderDayEvents(dateStr) {
  const evs = _calEventMap[dateStr] || [];
  const evEl = document.getElementById('cal-detail-events');
  const hotHalls = _calHotDays[dateStr] || [];

  if (evs.length === 0 && hotHalls.length === 0) {
    evEl.innerHTML = `<div class="empty-hint" style="padding:6px 0;gap:4px">
      <span>📅</span><span style="font-size:.7rem">イベント記録なし</span>
    </div>`;
  } else {
    let html = '';
    // ホット情報（z-score由来）
    if (hotHalls.length) {
      html += hotHalls.map(h => {
        const icon = h.z_score >= 2.0 ? '🔥🔥' : h.z_score >= 1.5 ? '🔥' : '・';
        return `<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;padding:5px 8px;background:rgba(251,146,60,.08);border:1px solid rgba(251,146,60,.2);border-radius:7px">
          <span style="font-size:.85rem">${icon}</span>
          <div style="flex:1">
            <div style="font-size:.72rem;font-weight:600;color:var(--text1)">${esc(h.hall_name)}</div>
            <div style="font-size:.62rem;color:var(--text3)">${esc(h.label)} (z=${h.z_score.toFixed(1)})</div>
          </div>
        </div>`;
      }).join('');
    }
    // イベント記録
    html += evs.map(e => {
      const src = e.source ? `<span style="font-size:.6rem;color:var(--text3)">[${e.source}]</span>` : '';
      const del = `<button onclick="deleteEvent(${e.id},'${dateStr}')" style="background:none;border:none;cursor:pointer;opacity:.4;margin-left:4px;font-size:.75rem;color:var(--text3);padding:0;line-height:1">✕</button>`;
      return `<div style="display:flex;align-items:flex-start;gap:6px;margin-bottom:5px;padding:5px 8px;background:var(--bg3);border-radius:7px">
        <div style="flex:1">
          <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">
            <span class="ev-badge ev-badge-${e.event_type}" style="margin:0">${e.event_type}</span>
            <span style="font-size:.7rem;color:var(--text2)">${esc(e.event_title || '')}</span>
            ${src}
          </div>
          <div style="font-size:.63rem;color:var(--text3);margin-top:2px">${esc(e.hall_name)}</div>
        </div>
        ${del}
      </div>`;
    }).join('');
    evEl.innerHTML = html;
  }
  const resEl = document.getElementById('cal-detail-results');
  if (resEl) resEl.innerHTML = '';
}


async function deleteEvent(id, dateStr) {
  if (!confirm('このイベントを削除しますか？')) return;
  await fetch(`/api/events/${id}`, { method: 'DELETE' });
  await loadCalendar();
  if (_calSelectedDate === dateStr) selectCalDay(dateStr);
}

async function bulkAddEvents() {
  const raw = document.getElementById('bulk-event-input')?.value?.trim();
  if (!raw) return;
  const defaultHall = document.getElementById('bulk-hall-default')?.value || '';
  const resultEl = document.getElementById('bulk-result');
  if (resultEl) resultEl.textContent = '登録中...';

  const EVENT_KEYWORDS = {
    '新台': '新台入替', '入替': '新台入替',
    '777': '特日7', '777日': '特日7',
    'ゾロ目': '特日ゾロ目',
    '感謝': '感謝デー', '周年': '感謝デー',
    '高設定': '高設定示唆', '全台': '高設定示唆',
    '朝イチ': '朝イチ', 'モーニング': '朝イチ',
  };
  const lines = raw.split('\n').map(l => l.trim()).filter(Boolean);
  let ok = 0, skip = 0;

  for (const line of lines) {
    const parts = line.split(/\s+/);
    if (parts.length < 1) { skip++; continue; }

    const dateRaw = parts[0];
    const dm = dateRaw.match(/(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})|(\d{1,2})[\/月](\d{1,2})/);
    if (!dm) { skip++; continue; }
    let dateStr;
    if (dm[1]) {
      dateStr = `${dm[1]}-${dm[2].padStart(2,'0')}-${dm[3].padStart(2,'0')}`;
    } else {
      const y = new Date().getFullYear();
      dateStr = `${y}-${dm[4].padStart(2,'0')}-${dm[5].padStart(2,'0')}`;
    }

    const rest = parts.slice(1);
    let eventType = '通常イベント';
    let hallName = defaultHall;
    const titleParts = [];

    for (const p of rest) {
      const matched = Object.entries(EVENT_KEYWORDS).find(([k]) => p.includes(k));
      if (matched) {
        eventType = matched[1];
        titleParts.push(p);
      } else if (p.length >= 3 && (p.includes('店') || p.includes('ホール') || p.includes('パチ') || p.includes('スロ'))) {
        hallName = p;
      } else {
        titleParts.push(p);
      }
    }
    if (!hallName) { skip++; continue; }
    const title = titleParts.join(' ');

    try {
      await fetch(`/api/events/manual?hall_name=${encodeURIComponent(hallName)}&event_date=${dateStr}&event_type=${encodeURIComponent(eventType)}&event_title=${encodeURIComponent(title)}`, { method: 'POST' });
      ok++;
    } catch { skip++; }
  }

  if (resultEl) resultEl.textContent = `${ok}件登録${skip > 0 ? ` (${skip}件スキップ)` : ''}`;
  if (ok > 0) {
    document.getElementById('bulk-event-input').value = '';
    await loadCalendar();
  }
}

async function addManualEvent() {
  const dateStr = _calSelectedDate;
  if (!dateStr) { showToast('カレンダーの日付をクリックして選択してください', 'error'); return; }
  const hall = document.getElementById('manual-hall')?.value;
  const type = document.getElementById('manual-type')?.value;
  const title = document.getElementById('manual-title')?.value || '';
  if (!hall) { showToast('店舗を選択してください', 'error'); return; }
  try {
    const res = await fetch(`/api/events/manual?hall_name=${encodeURIComponent(hall)}&event_date=${dateStr}&event_type=${encodeURIComponent(type)}&event_title=${encodeURIComponent(title)}`, { method: 'POST' });
    if (!res.ok) throw new Error(res.statusText);
    document.getElementById('manual-title').value = '';
    showToast(`${dateStr} に${type}を登録しました`);
    await loadCalendar();
    selectCalDay(dateStr);
  } catch(e) {
    showToast('登録失敗: ' + e.message, 'error');
  }
}

async function loadCalStrength() {
  const el = document.getElementById('cal-strength-body');
  if (!el) return;
  el.innerHTML = '<div style="font-size:.72rem;color:var(--text3);padding:4px 0">分析中...</div>';
  try {
    const hallParam = _calHallFilter ? `?hall_name=${encodeURIComponent(_calHallFilter)}` : '';
    const data = await fetch(`/api/events/strength${hallParam}`).then(r => r.json());
    if (!data.length || data[0]?.error) {
      el.innerHTML = `<div class="empty-hint" style="padding:10px 0">
        <span>📊</span>
        <span>イベント記録が蓄積されると<br>ガセ・強イベ判断が表示されます</span>
      </div>`;
      return;
    }
    const maxAbs = Math.max(...data.map(d => Math.abs(d.diff_vs_normal)), 1);
    el.innerHTML = data.map(d => {
      const barW = Math.round(Math.abs(d.diff_vs_normal) / maxAbs * 100);
      const barCol = d.diff_vs_normal >= 50 ? 'var(--success)' :
                     d.diff_vs_normal >= 0  ? 'var(--warning)' : 'var(--danger)';
      const sign = d.diff_vs_normal >= 0 ? '+' : '';
      const strength = d.diff_vs_normal >= 100 ? '🔥強' : d.diff_vs_normal >= 30 ? '✓そこそこ' : d.diff_vs_normal >= 0 ? '△微妙' : '❌ガセ';
      const strengthCol = d.diff_vs_normal >= 100 ? 'var(--success)' : d.diff_vs_normal >= 0 ? 'var(--warning)' : 'var(--danger)';
      return `<div style="margin-bottom:10px;padding:8px;background:var(--bg3);border-radius:8px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
          <span class="ev-badge ev-badge-${d.event_type}" style="margin:0">${d.event_type}</span>
          <span style="font-size:.68rem;font-weight:700;color:${strengthCol}">${strength}</span>
          <span style="color:var(--text3);font-size:.6rem">${d.event_days}日のデータ</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <div style="flex:1;height:8px;background:rgba(255,255,255,.05);border-radius:99px;overflow:hidden">
            <div style="width:${barW}%;height:100%;background:${barCol};border-radius:99px;box-shadow:0 0 6px ${barCol}44"></div>
          </div>
          <span style="font-size:.78rem;font-weight:800;color:${barCol};min-width:54px;text-align:right">${sign}${d.diff_vs_normal}枚</span>
        </div>
        <div style="font-size:.6rem;color:var(--text3);display:flex;gap:10px">
          <span>イベ日 <strong style="color:var(--text2)">${d.avg_diff_event}枚</strong></span>
          <span>通常日 <strong style="color:var(--text2)">${d.avg_diff_normal}枚</strong></span>
          <span>勝率 <strong style="color:${d.win_rate_event>=50?'var(--success)':'var(--danger)'}">${d.win_rate_event}%</strong></span>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = '';
  }
}

let _calInited = false;
async function initCalendar() {
  // ホールリストをセレクトに設定（毎回最新を取得）
  try {
    const halls = await fetch('/api/scrape/halls').then(r => r.json());
    const names = halls.map(h => h.hall_name || h);
    const selIds = ['cal-hall-filter', 'manual-hall', 'bulk-hall-default'];
    selIds.forEach(id => {
      const sel = document.getElementById(id);
      if (!sel) return;
      // 既存オプション（空値の最初1件）以外を削除してリフレッシュ
      const firstOpt = sel.options[0];
      sel.innerHTML = '';
      if (firstOpt) sel.add(firstOpt);
      names.forEach(n => sel.add(new Option(n, n)));
    });
    _calInited = true;
  } catch(e) {}
  loadCalendar();
}

// カレンダーコントロールイベント
document.getElementById('cal-prev')?.addEventListener('click', () => {
  _calMonth--;
  if (_calMonth < 0) { _calMonth = 11; _calYear--; }
  loadCalendar();
});
document.getElementById('cal-next')?.addEventListener('click', () => {
  _calMonth++;
  if (_calMonth > 11) { _calMonth = 0; _calYear++; }
  loadCalendar();
});
document.getElementById('cal-today-btn')?.addEventListener('click', () => {
  const now = new Date();
  _calYear = now.getFullYear();
  _calMonth = now.getMonth();
  loadCalendar().then(() => {
    const today = new Date().toISOString().slice(0, 10);
    selectCalDay(today);
  });
});
document.getElementById('cal-hall-filter')?.addEventListener('change', () => loadCalendar());
let _evPollTimer = null;

function _renderEvProgress(d) {
  const card = document.getElementById('ev-progress-card');
  if (!card) return;
  card.style.display = 'block';

  const total = d.total || 0;
  const done = d.done || 0;
  const failed = d.failed || 0;
  const pct = total > 0 ? Math.round((done + failed) / total * 100) : 0;

  document.getElementById('ev-bp-count').textContent = `${done + failed}/${total}`;
  document.getElementById('ev-bp-bar').style.width = pct + '%';

  const titleEl = document.getElementById('ev-bp-title');
  if (!d.running && total > 0) {
    titleEl.textContent = 'イベント取得完了';
    titleEl.style.color = 'var(--success)';
  } else {
    titleEl.textContent = 'イベント取得中';
    titleEl.style.color = 'var(--text1)';
  }

  let meta = [];
  if (d.running && d.eta_min > 0) meta.push(`残り約${d.eta_min}分`);
  if (done > 0) meta.push(`完了 ${done}店舗`);
  if (failed > 0) meta.push(`<span style="color:var(--danger)">失敗 ${failed}店舗</span>`);
  document.getElementById('ev-bp-meta').innerHTML = meta.join(' · ');

  const hallsEl = document.getElementById('ev-bp-halls');
  if (d.halls && d.halls.length) {
    const statusLabel = { done: '完了', running: '取得中', waiting: '待機中', failed: '失敗' };
    const statusColor = { done: 'var(--success)', running: 'var(--accent)', waiting: 'var(--text3)', failed: 'var(--danger)' };
    hallsEl.innerHTML = d.halls.map(h => {
      const dot = h.status === 'running'
        ? '<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--accent);animation:bpulse 1.2s ease-in-out infinite;vertical-align:middle;margin-right:4px"></span>'
        : '';
      const found = h.found ? `${h.found}件` : h.status === 'waiting' ? '—' : '0件';
      const sources = h.by_source && Object.keys(h.by_source).length
        ? Object.entries(h.by_source).map(([s, n]) => `${s}:${n}`).join(' ')
        : '';
      return `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)">
        <div style="flex:1;font-size:.72rem;color:${statusColor[h.status]||'var(--text2)'};overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${dot}${h.name}</div>
        <div style="font-size:.63rem;color:var(--text3);text-align:right">${sources || found}</div>
        <div style="font-size:.65rem;padding:2px 7px;border-radius:99px;background:${h.status==='done'?'rgba(0,180,100,.15)':h.status==='failed'?'rgba(220,50,50,.12)':h.status==='running'?'rgba(70,130,220,.12)':'var(--bg3)'};color:${statusColor[h.status]||'var(--text3)'}">${statusLabel[h.status]||h.status}</div>
      </div>`;
    }).join('');
  }
}

function _startEvPoll() {
  if (_evPollTimer) clearInterval(_evPollTimer);
  const btn = document.getElementById('cal-scrape-btn');
  _evPollTimer = setInterval(async () => {
    try {
      const d = await fetch('/api/events/scrape_status').then(r => r.json());
      _renderEvProgress(d);
      if (!d.running) {
        clearInterval(_evPollTimer);
        _evPollTimer = null;
        if (btn) { btn.disabled = false; btn.textContent = 'イベント取得'; }
        loadCalendar();
      }
    } catch(e) {}
  }, 3000);
}

document.getElementById('cal-scrape-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('cal-scrape-btn');
  const hall = document.getElementById('cal-hall-filter')?.value;
  btn.disabled = true;
  btn.textContent = '取得中...';
  const url = '/api/events/scrape' + (hall ? `?hall_name=${encodeURIComponent(hall)}` : '');
  try {
    const r = await fetch(url, { method: 'POST' }).then(r => r.json());
    if (r.ok) {
      showToast(r.message || 'イベント取得を開始しました');
      _startEvPoll();
    } else {
      showToast(r.message || 'エラー');
      btn.disabled = false;
      btn.textContent = 'イベント取得';
    }
  } catch(e) {
    showToast('エラー: ' + e.message);
    btn.disabled = false;
    btn.textContent = 'イベント取得';
  }
});

let _bulkPollTimer = null;

function _renderBulkProgress(d) {
  const card = document.getElementById('bulk-progress-card');
  if (!card) return;
  card.style.display = 'block';

  const total = d.total || 0;
  const done = d.done || 0;
  const failed = d.failed || 0;
  const pct = total > 0 ? Math.round((done + failed) / total * 100) : 0;

  document.getElementById('bp-count').textContent = `${done + failed}/${total}`;
  document.getElementById('bp-bar').style.width = pct + '%';
  document.getElementById('bp-mode').textContent = `${d.mode || ''} / ${d.days || 0}日分`;

  const titleEl = document.getElementById('bp-title');
  if (!d.running && total > 0) {
    titleEl.textContent = '全店舗スクレイプ完了';
    titleEl.style.color = 'var(--success)';
  } else {
    titleEl.textContent = '全店舗スクレイプ実行中';
    titleEl.style.color = 'var(--text1)';
  }

  let meta = [];
  if (d.running && d.eta_min > 0) meta.push(`残り約${d.eta_min}分`);
  if (done > 0) meta.push(`完了 ${done}店舗`);
  if (failed > 0) meta.push(`<span style="color:var(--danger)">失敗 ${failed}店舗</span>`);
  document.getElementById('bp-meta').innerHTML = meta.join('<span style="margin:0 4px">·</span>');

  const hallsEl = document.getElementById('bp-halls');
  if (d.halls && d.halls.length) {
    const statusLabel = { done: '完了', partial: '一部取得', running: '実行中', waiting: '待機中', failed: '失敗' };
    const statusColor = { done: 'var(--success)', partial: 'var(--warning)', running: 'var(--accent)', waiting: 'var(--text3)', failed: 'var(--danger)' };
    hallsEl.innerHTML = d.halls.map(h => {
      const isRunning = h.status === 'running';
      const dot = isRunning ? '<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--accent);animation:bpulse 1.2s ease-in-out infinite;vertical-align:middle;margin-right:4px"></span>' : '';
      const records = h.records ? `${h.records}件` : (h.status === 'waiting' ? '—' : '');
      const sourceLabels = { seat: '台番', machine: '差枚', snapshot: '設置' };
      const sourceText = Object.entries(h.sources || {}).map(([key, value]) => {
        const mark = value.status === 'done' ? '○' : value.status === 'failed' ? '×' : '―';
        return `${sourceLabels[key] || key}${mark}`;
      }).join(' ');
      return `<div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border)">
        <div style="flex:1;min-width:0"><div style="font-size:.72rem;color:${statusColor[h.status]||'var(--text2)'};overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${dot}${esc(h.name)}</div><div style="font-size:.57rem;color:var(--text3)">${sourceText}</div></div>
        <div style="font-size:.65rem;color:var(--text3);min-width:30px;text-align:right">${records}</div>
        <div style="font-size:.65rem;padding:2px 7px;border-radius:99px;background:${h.status==='done'?'rgba(0,180,100,.15)':h.status==='partial'?'rgba(245,158,11,.14)':h.status==='failed'?'rgba(220,50,50,.12)':h.status==='running'?'rgba(70,130,220,.12)':'var(--bg3)'};color:${statusColor[h.status]||'var(--text3)'}">${statusLabel[h.status]||h.status}</div>
      </div>`;
    }).join('');
  }
}

function _startBulkPoll() {
  if (_bulkPollTimer) clearInterval(_bulkPollTimer);
  const btn = document.getElementById('scrape-run-btn');
  _bulkPollTimer = setInterval(async () => {
    try {
      const d = await fetch('/api/scrape/bulk_status').then(r => r.json());
      _renderBulkProgress(d);
      if (!d.running) {
        clearInterval(_bulkPollTimer);
        _bulkPollTimer = null;
        if (btn) btn.disabled = false;
        loadScrapeManager();
      }
    } catch(e) { /* ignore poll errors */ }
  }, 3000);
}

document.getElementById('scrape-run-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('scrape-run-btn');
  const statusEl = document.getElementById('scrape-run-status');
  const days = document.getElementById('scrape-days-select')?.value || '7';
  const minrepoOnly = document.getElementById('minrepo-only-check')?.checked ? 'true' : 'false';
  btn.disabled = true;
  statusEl.textContent = '開始中...';
  try {
    const res = await fetch(`/api/scrape/run?days=${days}&minrepo_only=${minrepoOnly}`, { method: 'POST' }).then(r => r.json());
    if (res.ok) {
      showToast(res.message);
      statusEl.innerHTML = '';
      _startBulkPoll();
    } else {
      statusEl.innerHTML = `<span style="color:var(--danger)">${res.message || '実行失敗'}</span>`;
      btn.disabled = false;
    }
  } catch(e) {
    statusEl.innerHTML = `<span style="color:var(--danger)">エラー: ${e.message}</span>`;
    btn.disabled = false;
  }
});

// ホールタブ切替時にスクレイプ管理パネルも更新
const _origLoadHallPage = typeof loadHallPage === 'function' ? loadHallPage : null;
document.addEventListener('DOMContentLoaded', () => {
  loadScrapeManager();
  // クイックジャンプ: Enterキーで検索
  const quickSeatInput = document.getElementById('quick-seat-num');
  const quickMachineInput = document.getElementById('quick-machine-name');
  if (quickSeatInput) quickSeatInput.addEventListener('keydown', e => { if (e.key === 'Enter') quickJumpToSeat(); });
  if (quickMachineInput) quickMachineInput.addEventListener('keydown', e => { if (e.key === 'Enter') quickJumpToSeat(); });
});

async function loadScrapeHalls() {
  const el = document.getElementById('scrape-halls-list');
  if (!el) return;
  try {
    const halls = await apiFetch('/api/scrape/halls');
    if (!halls || halls.length === 0) { el.innerHTML = '<span style="color:var(--text3);font-size:.65rem">登録なし</span>'; return; }
    const today = new Date().toISOString().slice(0, 10);
    el.innerHTML = halls.map(h => {
      const enCol = h.enabled ? 'var(--success)' : 'var(--text3)';
      const enLabel = h.enabled ? '有効' : '無効';
      const enc = encodeURIComponent(h.hall_name);
      const lastDate = h.last_scraped_date || '';
      const diffDays = lastDate ? Math.round((Date.now() - new Date(lastDate).getTime()) / 86400000) : null;
      const dateCol = diffDays === null ? 'var(--text3)' : diffDays === 0 ? 'var(--success)' : diffDays <= 2 ? 'var(--text3)' : 'var(--danger)';
      const dateLabel = diffDays === null ? '未取得' : diffDays === 0 ? '今日' : `${diffDays}日前`;
      const recStr = h.db_record_count ? `${h.db_record_count}件` : '';
      const coverage = h.coverage || {};
      const badges = [
        ['台番', coverage.seat?.records],
        ['差枚', coverage.machine?.records],
        ['設置', coverage.snapshot?.records],
        ['予定', coverage.event?.records],
      ].map(([label, count]) => `<span style="font-size:.54rem;padding:1px 5px;border-radius:99px;background:${count ? 'rgba(34,197,94,.12)' : 'var(--bg3)'};color:${count ? 'var(--success)' : 'var(--text3)'}">${label}${count ? '○' : '―'}</span>`).join('');
      return `<div style="display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid var(--bg3)">
        <div style="flex:1;min-width:0">
          <div style="font-size:.72rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(h.hall_name)}</div>
          <div style="font-size:.58rem;color:${dateCol}">${esc(h.data_level || '未取得')} · ${dateLabel}${recStr ? ` · ${recStr}` : ''}</div>
          <div style="display:flex;gap:3px;flex-wrap:wrap;margin-top:3px">${badges}</div>
        </div>
        <button onclick="toggleScrapeHall('${enc}',${!h.enabled})"
          style="font-size:.58rem;padding:1px 6px;border-radius:3px;border:1px solid var(--border);background:transparent;color:${enCol};cursor:pointer;flex-shrink:0">${enLabel}</button>
        <button onclick="removeScrapeHall('${enc}')"
          style="font-size:.58rem;padding:1px 5px;border-radius:3px;border:1px solid var(--border);background:transparent;color:var(--danger);cursor:pointer;flex-shrink:0">削除</button>
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = '<span style="color:var(--danger);font-size:.65rem">読み込み失敗</span>';
  }
}

async function addScrapeHall() {
  const name = document.getElementById('add-hall-name')?.value?.trim();
  const pref = document.getElementById('add-hall-pref')?.value?.trim() || '大阪府';
  if (!name) { showToast('ホール名を入力してください', 'error'); return; }
  const res = await fetch(`/api/scrape/halls?hall_name=${encodeURIComponent(name)}&prefecture=${encodeURIComponent(pref)}`, { method: 'POST' }).then(r => r.json()).catch(() => null);
  if (res?.ok) {
    document.getElementById('add-hall-name').value = '';
    showToast(`${name} を追加しました`);
    loadScrapeHalls();
  } else {
    showToast('追加失敗', 'error');
  }
}

async function removeScrapeHall(encodedName) {
  const name = decodeURIComponent(encodedName);
  if (!confirm(`「${name}」をスクレイプ対象から削除しますか？`)) return;
  const res = await fetch(`/api/scrape/halls?hall_name=${encodedName}`, { method: 'DELETE' }).then(r => r.json()).catch(() => null);
  if (res?.ok) { showToast(`${name} を削除しました`); loadScrapeHalls(); }
  else { showToast('削除失敗', 'error'); }
}

async function toggleScrapeHall(encodedName, enable) {
  const name = decodeURIComponent(encodedName);
  const res = await fetch(`/api/scrape/halls?hall_name=${encodedName}&enabled=${enable}`, { method: 'PATCH' }).then(r => r.json()).catch(() => null);
  if (res?.ok) { loadScrapeHalls(); }
  else { showToast('更新失敗', 'error'); }
}
window.addScrapeHall = addScrapeHall;
window.removeScrapeHall = removeScrapeHall;
window.toggleScrapeHall = toggleScrapeHall;
