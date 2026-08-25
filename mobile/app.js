import {
  JUDGMENT_LABELS,
  applyPersonalCalibration,
  assessQuick,
  buildGuideRows,
  buildPerformanceSeries,
  buildValidationSummary,
  calculateSummary,
  minutesUntilClosing,
  money,
} from './core.mjs?v=2.9.0';
import { recognizeNumberFromFile } from './ocr.mjs?v=2.9.0';

const APP_VERSION = '2.9.0';
const VERSION_SEEN_KEY = 'pachi-version-seen';
const TARGET_REGION_KEY = 'pachi-target-region-v2';
const API_ORIGIN = window.location.hostname === 'yoshirou911.github.io'
  ? 'https://pachi-tool.fly.dev'
  : '';
const apiUrl = path => `${API_ORIGIN}${path}`;
function storedTargetRegion() {
  try { return localStorage.getItem(TARGET_REGION_KEY) || 'shijonawate'; } catch { return 'shijonawate'; }
}
function setTargetRegion(region) {
  const value = region || 'shijonawate';
  try { localStorage.setItem(TARGET_REGION_KEY, value); } catch { /* ignore */ }
  ['target-search-region', 'target-map-region', 'juggler-region'].forEach(id => {
    const element = document.getElementById(id);
    if (element) element.value = value;
  });
  return value;
}
let releaseInfo = {
  version: APP_VERSION,
  released_on: '2026-08-25',
  channel: '公開版',
  patch_notes: [{
    version: APP_VERSION,
    released_on: '2026-08-25',
    title: '四條畷の自動収集・検証・個人補正を統合',
    items: ['四條畷店の設置台数とフロアマップを日次収集', '取得失敗を設定画面で見える化', 'ジャグラー過去検証と期待値の個人補正を追加'],
  }],
};
const DB_NAME = 'pachi-tool-mobile';
const STORE_NAME = 'app-state';
const STATE_KEY = 'main';
const defaultState = {
  version: 3,
  budget: { starting_bankroll: 0, loss_limit_yen: 0 },
  candidates: [],
  plans: [],
  results: [],
  patrol_sessions: [],
  patrol_observations: [],
  hall_reset_records: [],
  replay_usage: {},
  sync: { key: '', enabled: false, last_synced_at: '', pending: false, pending_count: 0, last_error: '' },
  settings: { closing_time: '22:45', scan_hall: 'キコーナ四條畷店' },
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
let jugglerCatalog = [];
let jugglerTargetData = null;
let activeModule = 'home';
let targetMapData = null;
let targetHeatMap = null;
let targetHeatLayer = null;
let trendData = null;
let floorData = null;
let floorEditorSeats = [];
let targetHallOptions = [];
let scanSnapshot = null;
let occupancyPriorityData = null;
let hyenaStoreRankingData = null;
let syncTimer = null;
let syncWriting = false;
let syncHeartbeat = null;

const TIME_STRATEGIES = [
  {
    id: 'morning', start: 600, end: 690, label: '朝一', sample: '10:30',
    title: 'リセット・据え置き確認を優先', interval: '60〜90分', tone: 'blue',
    check: ['リセット確定・据え置きで条件が変わる台', '前日最終Gと当日Gの合算可否', '朝一作戦に保存した機種・島'],
    avoid: '通常天井狙いはまだ育っていない。根拠のない0G着席はしない。',
  },
  {
    id: 'early', start: 690, end: 900, label: '前半', sample: '13:00',
    title: '履歴が育ち始めた台を拾う', interval: '約60分', tone: 'cyan',
    check: ['通過ラインに近い現在G', '単発・スルー回数と液晶表示', '空き台になった直後の履歴'],
    avoid: 'ゲーム数だけで座らず、AT間・CZ間など数える区間を合わせる。',
  },
  {
    id: 'middle', start: 900, end: 1110, label: '中盤', sample: '16:30',
    title: '通常天井・スルー狙いの標準巡回', interval: '45〜60分', tone: 'green',
    check: ['期待値ボーダー100G手前の台', '当たり履歴が増えた機種の島', '持ちメダルで打てる候補'],
    avoid: '低期待値を長時間追わず、候補がなければ次の島・店舗へ移る。',
  },
  {
    id: 'evening', start: 1110, end: 1260, label: '夜', sample: '19:30',
    title: '拾いやすさと閉店リスクを同時判定', interval: '30〜45分', tone: 'orange',
    check: ['現在Gと期待値', '閉店までの残り時間', '想定消化時間・投資上限・持ちメダル'],
    avoid: '期待値があっても、取り切れない可能性が高い長時間ATは見送る。',
  },
  {
    id: 'closing', start: 1260, end: 1365, label: '閉店前', sample: '21:30',
    title: '短時間で終わる高期待値だけ', interval: '20〜30分', tone: 'red',
    check: ['閉店リスク判定が「打てる」か', '短時間完結のゾーン・天井', 'すぐ着席できる高期待値台'],
    avoid: '消化時間不明・長いAT・低い期待値は見送り。取り切りを最優先する。',
  },
];

const PLAY_MINUTES_BY_MACHINE = {
  'スマスロ北斗の拳': 70,
  'スマスロモンキーターン5': 80,
  'スマスロ東京喰種': 95,
  'スマスロ ゴッドイーター リザレクション': 85,
  'スマスロ かぐや様は告らせたい': 75,
  'スマスロ モンスターハンターライズ': 100,
  'スマスロ からくりサーカス': 105,
  'スマスロ 東京リベンジャーズ': 90,
  'スマスロ 化物語': 75,
  'スマスロ ミリオンゴッド-神々の軌跡-': 110,
};

const COMMON_PREVIOUS_FIELD = {
  id: 'previous_day_games', label: '前日最終G（据え置き時だけ加算）', type: 'number',
  min: 0, max: 5000, placeholder: '不明なら空欄', help: 'リセット確定時は加算しません。', add_to_primary: true,
};

function enrichProfile(profile) {
  const fields = Array.isArray(profile.input_fields) ? [...profile.input_fields] : [];
  const requirements = Array.isArray(profile.requirements) ? [...profile.requirements] : [];
  if (profile.reset_status === 'normal' && String(profile.unit_label).includes('G')) fields.unshift(COMMON_PREVIOUS_FIELD);
  if (profile.machine_name.includes('東京喰種')) {
    fields.push({ id: 'counter_source', label: '確認したカウンター', type: 'select', required: true, options: [
      { value: '', label: '選択してください' }, { value: 'cz', label: '液晶CZ間' }, { value: 'at', label: 'データカウンターAT間' },
    ] });
    requirements.push({ field: 'counter_source', operator: 'eq', value: profile.catalog_key.includes('-cz-') ? 'cz' : 'at', message: `${profile.metric_name}を確認してください` });
  }
  if (profile.machine_name.includes('かぐや様')) {
    fields.push({ id: 'last_bonus', label: '直前ボーナス', type: 'select', required: true, options: [
      { value: '', label: '選択してください' }, { value: 'big', label: 'BIG後' }, { value: 'reg', label: 'REG後' },
    ] });
    requirements.push({ field: 'last_bonus', operator: 'eq', value: profile.catalog_key.includes('-big-') ? 'big' : 'reg', message: '選んだBIG後・REG後条件と履歴が一致しません' });
  }
  if (profile.machine_name.includes('モンスターハンターライズ')) {
    fields.push({ id: 'quest_misses', label: 'クエストスルー回数', type: 'number', min: 0, max: 20, placeholder: '例：3', help: 'メニュー画面で確認。4スルー以上は強い追加根拠です。' });
  }
  if (profile.machine_name.includes('からくりサーカス')) {
    fields.push(
      { id: 'counter_source', label: '入力したゲーム数', type: 'select', required: true, options: [
        { value: '', label: '選択してください' }, { value: 'real', label: 'CZ間の実ゲーム数' }, { value: 'lcd', label: '液晶内部G' },
      ] },
      { id: 'cz_misses', label: 'CZスルー回数', type: 'number', min: 0, max: 20, placeholder: '例：2' },
      { id: 'at_gap', label: 'AT間ゲーム数', type: 'number', min: 0, max: 5000, placeholder: '例：900' },
      { id: 'fate_progress', label: '運命の一劇の状態', type: 'select', options: [
        { value: '', label: '権利なし/不明' }, { value: 'ready', label: '権利獲得済み' },
      ] },
    );
    requirements.push({ field: 'counter_source', operator: 'eq', value: 'real', message: '液晶内部GではなくCZ間の実ゲーム数を入力してください' });
  }
  if (profile.machine_name.includes('ゴッドイーター') || profile.machine_name.includes('かぐや様')) {
    fields.push({ id: 'section_reset_confirmed', label: '有利区間リセット確認', type: 'select', options: [
      { value: '', label: '未確認' }, { value: 'true', label: '画面・挙動で確認済み' },
    ], help: '差枚だけの推測では選ばないでください。' });
  }
  if (profile.machine_name.includes('バイオハザードRE:3')) {
    fields.push({ id: 'cz_misses', label: 'CZスルー回数', type: 'number', min: 0, max: 6, placeholder: '例：5', help: '5スルー以降は追加の狙い根拠です。' });
  }
  if (profile.machine_name.includes('東京リベンジャーズ')) {
    fields.push({ id: 'chance_misses', label: '東卍チャンススルー回数', type: 'number', min: 0, max: 4, placeholder: '例：3', help: '3スルー以上は追加根拠です。' });
  }
  return {
    ...profile,
    estimated_play_minutes: profile.estimated_play_minutes || PLAY_MINUTES_BY_MACHINE[profile.machine_name] || null,
    input_fields: fields,
    requirements,
  };
}

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
  state.version = 3;
  if (state.sync?.enabled && !syncWriting) {
    state.sync.pending = true;
    state.sync.pending_count = Math.min(999, Number(state.sync.pending_count || 0) + 1);
  }
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
  if (state.sync?.enabled && !syncWriting && navigator.onLine) {
    clearTimeout(syncTimer);
    syncTimer = setTimeout(() => pushMobileSync(true), 1200);
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
    patrol_sessions: Array.isArray(saved?.patrol_sessions) ? saved.patrol_sessions : [],
    patrol_observations: Array.isArray(saved?.patrol_observations) ? saved.patrol_observations : [],
    hall_reset_records: Array.isArray(saved?.hall_reset_records) ? saved.hall_reset_records : [],
    replay_usage: saved?.replay_usage && typeof saved.replay_usage === 'object' ? saved.replay_usage : {},
    sync: { ...defaultState.sync, ...(saved?.sync || {}) },
  };
}

async function loadCatalog() {
  const response = await fetch(`./catalog.json?v=${APP_VERSION}`);
  if (!response.ok) throw new Error(`期待値データを取得できません（${response.status}）`);
  const catalog = await response.json();
  profiles = (catalog.profiles || []).map((profile, index) => enrichProfile({ ...profile, id: index + 1 }));
}

function currentProfile() {
  return profiles.find(profile => profile.id === Number(byId('quick-profile').value));
}

async function pushMobileSync(silent = false) {
  const key = (byId('sync-key')?.value || state.sync.key || '').trim();
  if (key.length < 32) {
    if (!silent) showToast('32文字以上の同期コードを設定してください');
    return false;
  }
  if (!navigator.onLine) {
    state.sync = { ...state.sync, key, enabled: true, pending: true, last_error: 'offline' };
    syncWriting = true;
    await writeLocalState();
    syncWriting = false;
    if (!silent) showToast('オフラインのため端末に保留しました');
    renderSettings();
    return false;
  }
  try {
    const response = await fetch(apiUrl('/api/opportunity/sync'), {
      method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Sync-Key': key },
      body: JSON.stringify({ state }),
    });
    if (!response.ok) throw new Error(`同期API ${response.status}`);
    state.sync = { key, enabled: true, last_synced_at: new Date().toISOString(), pending: false, pending_count: 0, last_error: '' };
    syncWriting = true;
    await writeLocalState();
    syncWriting = false;
    renderSettings();
    if (!silent) showToast('サーバーへ同期しました');
    return true;
  } catch (error) {
    state.sync = { ...state.sync, key, enabled: true, pending: true, last_error: error.message };
    syncWriting = true;
    await writeLocalState();
    syncWriting = false;
    renderSettings();
    if (!silent) showToast(`同期失敗：${error.message}`);
    return false;
  }
}

async function pullMobileSync() {
  const key = (byId('sync-key').value || '').trim();
  if (key.length < 32) return showToast('同期コードを入力してください');
  try {
    const response = await fetch(apiUrl('/api/opportunity/sync'), { headers: { 'X-Sync-Key': key }, cache: 'no-store' });
    if (!response.ok) throw new Error(response.status === 404 ? '保存データなし' : `同期API ${response.status}`);
    const payload = await response.json();
    state = normalizeState(payload.state);
    state.sync = { key, enabled: true, last_synced_at: new Date().toISOString(), pending: false, pending_count: 0, last_error: '' };
    syncWriting = true;
    await writeLocalState();
    syncWriting = false;
    renderAll();
    showToast('サーバーから復元しました');
  } catch (error) {
    syncWriting = false;
    showToast(`復元失敗：${error.message}`);
  }
}

function renderProfileInputFields(profile) {
  const container = byId('quick-extra-fields');
  const fields = Array.isArray(profile?.input_fields) ? profile.input_fields : [];
  container.innerHTML = fields.map(field => {
    const required = field.required ? ' required' : '';
    const help = field.help ? `<small>${esc(field.help)}</small>` : '';
    if (field.type === 'select') {
      return `<label>${esc(field.label)}<select data-profile-input="${esc(field.id)}"${required}>${(field.options || []).map(option => `<option value="${esc(option.value)}">${esc(option.label)}</option>`).join('')}</select>${help}</label>`;
    }
    return `<label>${esc(field.label)}<input data-profile-input="${esc(field.id)}" type="number" inputmode="numeric" min="${Number(field.min ?? 0)}"${field.max == null ? '' : ` max="${Number(field.max)}"`} placeholder="${esc(field.placeholder || '')}"${required}>${help}</label>`;
  }).join('');
  updateEffectiveValue();
}

function collectProfileInputs() {
  return Object.fromEntries([...document.querySelectorAll('[data-profile-input]')].map(input => [input.dataset.profileInput, input.value]));
}

function effectiveCurrentValue() {
  const current = Number(byId('quick-current').value || 0);
  const profile = currentProfile();
  const inputs = collectProfileInputs();
  const previousField = (profile?.input_fields || []).find(field => field.id === 'previous_day_games' && field.add_to_primary);
  const previous = previousField && byId('quick-reset').value === 'normal' ? Number(inputs.previous_day_games || 0) : 0;
  return { current, previous, effective: current + previous, inputs };
}

function updateEffectiveValue() {
  const note = byId('quick-effective-value');
  const values = effectiveCurrentValue();
  note.hidden = values.previous <= 0;
  note.innerHTML = values.previous > 0
    ? `<b>宵越し合算</b><strong>${values.current.toLocaleString('ja-JP')}G + 前日${values.previous.toLocaleString('ja-JP')}G = ${values.effective.toLocaleString('ja-JP')}G</strong><span>据え置き前提。リセットの可能性がある場合は使用しません。</span>`
    : '';
}

function replayUsageKey() {
  return `${todayValue()}|${byId('quick-hall')?.value || '未選択'}`;
}

function loadReplayUsageInputs() {
  const saved = state.replay_usage?.[replayUsageKey()] || {};
  byId('quick-replay-limit').value = saved.limit_medals ?? 460;
  byId('quick-replay-used').value = saved.used_medals ?? 0;
  byId('quick-exchange-rate').value = saved.exchange_rate ?? (byId('quick-exchange').value === '56' ? 5.6 : 5.6);
  updateAdjustmentNote();
}

async function persistReplayUsageInputs() {
  state.replay_usage[replayUsageKey()] = {
    limit_medals: Math.max(0, Number(byId('quick-replay-limit').value || 0)),
    used_medals: Math.max(0, Number(byId('quick-replay-used').value || 0)),
    exchange_rate: Math.max(5.01, Number(byId('quick-exchange-rate').value || 5.6)),
    updated_at: new Date().toISOString(),
  };
  await writeLocalState();
}

function adjustmentInputs() {
  return {
    sectionDifferenceCoins: byId('quick-section-diff').value === '' ? null : Number(byId('quick-section-diff').value),
    replayLimitMedals: Number(byId('quick-replay-limit').value || 0),
    replayUsedMedals: Number(byId('quick-replay-used').value || 0),
    exchangeRate: Number(byId('quick-exchange-rate').value || 5.6),
  };
}

function updateAdjustmentNote() {
  const exchange = byId('quick-exchange').value;
  const funding = byId('quick-funding').value;
  const limit = Number(byId('quick-replay-limit').value || 0);
  const used = Number(byId('quick-replay-used').value || 0);
  const remaining = Math.max(0, limit - used);
  const baseText = exchange === 'equivalent'
    ? '等価交換では現金ギャップ補正は行いません。有利区間差枚は入力した場合だけ参考補正します。'
    : funding === 'medals'
      ? `再プレイ残り${remaining.toLocaleString('ja-JP')}枚。超過分は現金投資として期待値から交換ギャップを引きます。`
      : '現金投資条件です。現金用ルールを選び、再プレイ補正は行いません。';
  const tendency = resetTendencyStats(byId('quick-hall')?.value || '', byId('quick-machine')?.value || '', (new Date().getDay() + 6) % 7);
  const tendencyText = tendency.suggestion === 'reset_confirmed'
    ? ` 同曜日実績${tendency.samples}件はリセット寄りですが、確定表示がない限り手動確認してください。`
    : tendency.suggestion === 'normal'
      ? ` 同曜日実績${tendency.samples}件は据え置き寄りです。`
      : '';
  byId('quick-adjustment-note').textContent = baseText + tendencyText;
}

function syncConditions(profile) {
  if (!profile) return;
  if (['equivalent', '56', 'other'].includes(profile.exchange_type)) byId('quick-exchange').value = profile.exchange_type;
  if (['cash', 'medals'].includes(profile.funding_mode)) byId('quick-funding').value = profile.funding_mode;
  if (['normal', 'reset_confirmed'].includes(profile.reset_status)) byId('quick-reset').value = profile.reset_status;
  byId('quick-current-label').textContent = `${profile.metric_name}（${profile.unit_label}）`;
  byId('quick-current-unit').textContent = profile.unit_label || 'G';
  renderProfileInputFields(profile);
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
  const adjustmentRows = [];
  const source = safeUrl(profile.source_url);
  if (result.section_adjustment?.active) {
    const dedicated = result.section_adjustment.rule && !result.section_adjustment.border_reduction;
    adjustmentRows.push(`<div><span>機種別ツラヌキ/天井条件</span><strong>${dedicated ? '専用条件一致' : `-${Number(result.section_adjustment.border_reduction).toLocaleString('ja-JP')}${esc(profile.unit_label)}`}</strong><small>${esc(result.section_adjustment.reason || (result.section_adjustment.verified ? '機種別確認済み' : '参考値のため単独では着席許可にしません'))}</small></div>`);
  }
  if (result.replay_adjustment?.active) {
    adjustmentRows.push(`<div><span>再プレイ・現金ギャップ</span><strong>-${money(result.cash_gap_yen)}</strong><small>再プレイ残り${Number(result.replay_adjustment.remaining_replay_medals || 0).toLocaleString('ja-JP')}枚／補正後期待値${money(result.expected_value_yen, true)}</small></div>`);
  }
  byId('quick-result').innerHTML = `
    <div class="decision-card ${esc(result.judgment)}">
      <div class="decision-head"><div><span class="page-step">判定結果</span><h2>${esc(label)}</h2></div><span class="signal signal-${esc(result.judgment)}">${result.actionable ? '打てる' : '停止'}</span></div>
      <p class="decision-reason">${esc(result.reason)}</p>
      <div class="decision-highlight">
        <div><small>期待値</small><strong class="${Number(result.expected_value_yen) >= 0 ? 'money-up' : ''}">${money(result.expected_value_yen, true)}</strong></div>
        <div><small>必要資金</small><strong>${money(result.worst_case_investment_yen)}</strong></div>
      </div>
      ${adjustmentRows.length ? `<div class="decision-adjustments">${adjustmentRows.join('')}</div>` : ''}
      <div class="result-metrics">
        <div><small>現在</small><strong>${Number(currentValue).toLocaleString('ja-JP')}${esc(profile.unit_label)}</strong></div>
        <div><small>補正後の狙い始め</small><strong>${Number(result.adjusted_start_threshold ?? profile.start_threshold).toLocaleString('ja-JP')}${esc(profile.unit_label)}〜</strong></div>
        <div><small>閉店まで</small><strong>${result.minutes_until_close}分</strong></div>
        <div><small>使える資金</small><strong>${money(calculateSummary(state).risk_capacity_yen)}</strong></div>
        <div><small>平均 / 閉店安全側</small><strong>${result.estimated_play_minutes ? `${result.estimated_play_minutes}分 / ${result.safe_play_minutes || '--'}分` : '--'}</strong></div>
        <div><small>期待時給</small><strong>${money(result.ev_per_hour_yen, true)}</strong></div>
      </div>
      <div class="input-rule"><b>この判定で見る数字</b><strong>${esc(profile.metric_name)}（${esc(profile.unit_label)}）</strong>${profile.notes ? `<span>${esc(profile.notes)}</span>` : ''}</div>
      <div class="decision-source"><span>データ信頼度：${esc({ official: '公式', verified: '複数情報で確認', reference: '参考', unverified: '未確認' }[profile.confidence] || profile.confidence || '未確認')}</span>${source ? `<a href="${esc(source)}" target="_blank" rel="noopener">出典を開く</a>` : ''}</div>
      ${result.duration_breakdown && Object.keys(result.duration_breakdown).length ? `<div class="stop-rule"><b>消化時間の内訳</b>通常 ${result.duration_breakdown.normal || 0}分・CZ/前兆 ${result.duration_breakdown.cz_forecast || 0}分・AT/ボーナス ${result.duration_breakdown.at_bonus || 0}分・引き戻し ${result.duration_breakdown.pullback || 0}分・速度変化 ${result.duration_breakdown.variable_speed || 0}分<br><small>閉店判定は長引き余裕込みの安全側時間を使用</small></div>` : ''}
      ${warnings ? `<ul class="warning-list">${warnings}</ul>` : ''}
      <div class="stop-rule"><b>やめどき</b>${esc(profile.stop_rule || '未登録')}</div>
      ${result.actionable ? `<div class="seat-final-check"><b>座る前の最終確認</b><small>4項目すべてを現物で確認すると保存できます</small>
        <label><input type="checkbox" data-seat-confirm><span>入力した「${esc(profile.metric_name)}」と台の表示が一致</span></label>
        <label><input type="checkbox" data-seat-confirm><span>スルー回数・示唆・専用項目を台メニューで確認</span></label>
        <label><input type="checkbox" data-seat-confirm><span>リセットを推測だけで確定扱いしていない</span></label>
        <label><input type="checkbox" data-seat-confirm><span>必要資金と閉店安全側時間に余裕がある</span></label>
      </div>` : ''}
      <div class="decision-actions">
        ${result.actionable ? '<button id="save-candidate-button" class="primary-button" type="button" disabled>4項目を確認して保存</button>' : ''}
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
    <p class="card-meta">${candidate.hall_name ? `${esc(candidate.hall_name)}・` : ''}${candidate.seat_number ? `${esc(candidate.seat_number)}番台・` : ''}現在 ${Number(candidate.current_value).toLocaleString('ja-JP')}${esc(candidate.unit_label)}・期待値 ${money(candidate.expected_value_yen, true)}・必要資金 ${money(candidate.worst_case_investment_yen)}</p>
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
    container.innerHTML = `<div class="target-empty"><strong>${esc(targetSearchData.region_label || '選択地域')}に表示できる候補がありません</strong><span>${esc(targetSearchData.notice || '取得日数が増えるまでお待ちください。')}</span></div>
      ${insufficient.length ? `<details class="insufficient-halls" open><summary>除外・データ不足 ${insufficient.length}店</summary><div>${insufficient.map(item => `<span>${esc(item.hall_name)}：${esc(item.reason)}</span>`).join('')}</div></details>` : ''}`;
    return;
  }
  if (result.personal_calibration?.calibration_active) {
    adjustmentRows.push(`<div><span>自分の実戦データ補正</span><strong>${result.personal_calibration.calibration_pct}%</strong><small>${result.personal_calibration.count}件を使用／補正前 ${money(result.expected_value_before_personal_calibration_yen, true)} → 安全側 ${money(result.expected_value_yen, true)}</small></div>`);
  }
  container.innerHTML = `
    <div class="target-result-heading"><div><span class="page-step">${esc(targetSearchData.region_label || '')}・${esc(targetSearchData.visit_date)} ${esc(targetSearchData.weekday)}曜日</span><h2>店舗・狙い機種ランキング</h2></div><span class="count-badge">${halls.length}店</span></div>
    ${halls.map((hall, hallIndex) => `<article class="target-hall-card">
      <div class="target-action target-action-${hall.action?.startsWith('狙う') ? 'go' : hall.action === '見送り' ? 'stop' : 'check'}"><b>${esc(hall.action || '要確認')}</b><span>${esc(hall.action_reason || '')}</span></div>
      <div class="target-hall-head">
        <span class="target-rank">${hall.rank}</span>
        <div><strong>${esc(hall.hall_name)}</strong><small>${esc(hall.basis)}・最終 ${esc(hall.latest_date)}</small></div>
        <div class="target-score"><b>${hall.score}</b><small>点</small></div>
      </div>
      <div class="target-metrics"><span><small>店舗平均</small><b class="${hall.avg_diff >= 0 ? 'money-up' : 'money-down'}">${signedCoins(hall.avg_diff)}</b></span><span><small>プラス日率</small><b>${hall.positive_rate}%</b></span><span><small>過去検証</small><b>${hall.validation?.test_days || 0}日</b></span><span><small>狙い時成功</small><b>${hall.validation?.recommendation_success_pct == null ? '--' : `${hall.validation.recommendation_success_pct}%`}</b></span></div>
      <div class="target-validation"><b>${esc(hall.validation?.trust_level || 'データ不足')}</b><span>方向的中 ${hall.validation?.direction_accuracy_pct ?? 0}% ／ 推奨 ${hall.validation?.recommended_days ?? 0}回 ／ 安全側下限 ${hall.validation?.recommendation_lower_bound_pct ?? '--'}%</span></div>
      <div class="target-reasons">${(hall.reasons || []).map(reason => `<span>${esc(reason)}</span>`).join('')}</div>
      <div class="target-machine-list">
        ${(hall.target_machines || []).slice(0, 3).map((machine, machineIndex) => `<div class="target-machine-row">
          <div><strong>${esc(machine.machine_name)}</strong><small>${esc(machine.action || '要確認')}・狙い時${machine.validation?.recommendation_success_pct == null ? '--' : `${machine.validation.recommendation_success_pct}%`}・${machine.sample_days}日・${esc(machine.installation_status || '設置未確認')}・補正${signedCoins(machine.avg_diff)}</small></div>
          <span>${machine.score}点</span>
          <button type="button" data-target-hall-index="${hallIndex}" data-target-machine-index="${machineIndex}" ${machine.action?.startsWith('狙う') ? '' : 'disabled'}>${machine.action?.startsWith('狙う') ? '朝一候補に保存' : '保存不可'}</button>
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
    <div class="target-action target-action-${hall.action?.startsWith('狙う') ? 'go' : hall.action === '見送り' ? 'stop' : 'check'}"><b>${esc(hall.action || '要確認')}</b><span>${esc(hall.action_reason || '')}</span></div>
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
  const region = setTargetRegion(byId('target-map-region').value);
  const longDays = byId('target-map-long-days').value;
  const status = byId('target-map-status');
  byId('target-map-button').disabled = true;
  status.textContent = '指定日の店舗熱量と長期傾向を計算中...';
  try {
    const response = await fetch(apiUrl(`/api/map/target_heat?visit_date=${encodeURIComponent(visitDate)}&days=120&long_days=${encodeURIComponent(longDays)}&region=${encodeURIComponent(region)}`));
    if (!response.ok) throw new Error(`マップAPI ${response.status}`);
    targetMapData = await response.json();
    renderTargetHeatMap();
    status.textContent = `${targetMapData.region_label}・${targetMapData.visit_date}（${targetMapData.weekday}）・${targetMapData.halls.length}店舗を表示中`;
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
  ['trend-hall', 'floor-hall', 'scan-hall', 'quick-hall'].forEach(id => {
    const select = byId(id);
    const current = select.value;
    select.innerHTML = options;
    if ([...select.options].some(option => option.value === current)) select.value = current;
  });
  const scanHall = byId('scan-hall');
  if ([...scanHall.options].some(option => option.value === state.settings.scan_hall)) scanHall.value = state.settings.scan_hall;
  return targetHallOptions;
}

function currentTimeValue() {
  const now = new Date();
  return `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
}

function strategyAt(value) {
  const [hours, minutes] = String(value || '').split(':').map(Number);
  const total = Number.isFinite(hours) && Number.isFinite(minutes) ? hours * 60 + minutes : -1;
  return TIME_STRATEGIES.find(strategy => total >= strategy.start && total < strategy.end) || null;
}

function renderTimeStrategy() {
  const input = byId('scan-strategy-time');
  const selected = strategyAt(input.value);
  byId('scan-time-tabs').innerHTML = TIME_STRATEGIES.map(strategy => `
    <button type="button" class="${selected?.id === strategy.id ? 'active' : ''}" data-strategy-time="${strategy.sample}">
      <b>${esc(strategy.label)}</b><small>${String(Math.floor(strategy.start / 60)).padStart(2, '0')}:${String(strategy.start % 60).padStart(2, '0')}〜</small>
    </button>`).join('');
  const result = byId('scan-time-strategy');
  if (!selected) {
    result.innerHTML = '<div class="time-strategy-closed"><strong>営業時間外・準備時間</strong><span>開店後の時刻を選ぶと、その時間帯の立ち回りを確認できます。</span></div>';
    return;
  }
  result.innerHTML = `
    <div class="time-strategy-head strategy-${selected.tone}">
      <div><span>${esc(selected.label)}の優先行動</span><strong>${esc(selected.title)}</strong></div>
      <p><small>巡回目安</small><b>${esc(selected.interval)}</b></p>
    </div>
    <div class="time-strategy-body">
      <div><b>見るもの</b><ol>${selected.check.map(item => `<li>${esc(item)}</li>`).join('')}</ol></div>
      <div class="strategy-avoid"><b>見送り基準</b><p>${esc(selected.avoid)}</p></div>
    </div>`;
}

function renderDataCoverage(containerId, coverage) {
  const container = byId(containerId);
  if (!container) return;
  if (!coverage) {
    container.innerHTML = '<p class="empty">データ量を取得できませんでした。分析結果は参考値として扱ってください。</p>';
    return;
  }
  const performance = coverage.performance || {};
  const installation = coverage.installation || {};
  const intraday = coverage.intraday || {};
  const readiness = coverage.readiness || {};
  const level = readiness.trend_level || 'insufficient';
  const ageText = performance.age_days == null ? '実績日なし' : performance.age_days === 0 ? '本日まで' : `最新から${performance.age_days}日`;
  container.innerHTML = `
    <div class="coverage-head">
      <div><span class="page-step">DATA COVERAGE</span><strong>${esc(coverage.hall_name)}の分析データ</strong></div>
      <span class="coverage-status coverage-${esc(level)}">傾向分析：${esc(readiness.trend_label || '不足')}</span>
    </div>
    <div class="coverage-metrics">
      <span><small>日別実績</small><b>${Number(performance.performance_days || 0).toLocaleString('ja-JP')}日</b><em>${esc(ageText)}</em></span>
      <span><small>機種別</small><b>${Number(performance.machine_records || 0).toLocaleString('ja-JP')}件</b><em>差枚・勝率</em></span>
      <span><small>台番号別</small><b>${Number(performance.seat_records || 0).toLocaleString('ja-JP')}件</b><em>${readiness.seat_ready ? '座席分析可' : '座席分析不足'}</em></span>
      <span><small>設置情報</small><b>${Number(installation.records || 0).toLocaleString('ja-JP')}件</b><em>${installation.latest_date || '未取得'}</em></span>
      <span><small>時間帯別</small><b>${Number(intraday.records || 0).toLocaleString('ja-JP')}件</b><em>${intraday.ready ? '実測分析可' : 'まだ未収集'}</em></span>
    </div>
    <details class="coverage-reasons"><summary>分析できること・足りないこと</summary><ul>${(readiness.reasons || []).map(reason => `<li>${esc(reason)}</li>`).join('')}</ul></details>`;
}

function comparableMachineName(value) {
  return String(value || '')
    .normalize('NFKC')
    .toLowerCase()
    .replace(/[\s・･~〜～:：!！?？_\-‐]/g, '')
    .replace(/^(l|スマスロ|パチスロ)/, '')
    .replace(/モンキーターンv$/, 'モンキーターン5');
}

function isInstalledProfile(profileName, installedNames) {
  const profileKey = comparableMachineName(profileName);
  return installedNames.some(name => {
    const installedKey = comparableMachineName(name);
    return profileKey === installedKey || profileKey.includes(installedKey) || installedKey.includes(profileKey);
  });
}

function renderScanList(snapshot, fallback = false) {
  const installedNames = (snapshot?.machines || []).map(row => row.machine_name);
  const availableProfiles = fallback || !installedNames.length
    ? profiles
    : profiles.filter(profile => isInstalledProfile(profile.machine_name, installedNames));
  const grouped = new Map();
  availableProfiles.forEach(profile => {
    if (!grouped.has(profile.machine_name)) grouped.set(profile.machine_name, []);
    grouped.get(profile.machine_name).push(profile);
  });
  const machineGroups = [...grouped.entries()].sort((a, b) => a[0].localeCompare(b[0], 'ja'));
  const hall = byId('scan-hall').value;
  const dateLabel = snapshot?.snapshot_date ? `設置確認 ${snapshot.snapshot_date}` : '設置情報未取得';
  byId('scan-summary').innerHTML = `<strong>${esc(hall)}：見るのは${machineGroups.length}機種だけ</strong><span>${esc(dateLabel)}・登録${profiles.length}条件から店舗設置機種を抽出。通過ライン未満は島を歩きながら数字だけ見て通過します。</span>`;
  const list = byId('scan-machine-list');
  if (!machineGroups.length) {
    list.innerHTML = '<p class="empty">この店舗で対応機種を照合できませんでした。設置情報を更新するか、早見表を使用してください。</p>';
    return;
  }
  list.innerHTML = machineGroups.map(([machine, machineProfiles], machineIndex) => `
    <article class="scan-machine-card">
      <div class="scan-machine-head"><span>${machineIndex + 1}</span><div><strong>${esc(machine)}</strong><small>${machineProfiles.length}条件を確認</small></div></div>
      <div class="scan-condition-list">${machineProfiles.map(profile => {
        const passLine = Math.max(0, Number(profile.start_threshold) - 100);
        return `<div class="scan-condition-row">
          <div><strong>${esc(profile.condition_label)}</strong><small>${esc(profile.metric_name)}</small></div>
          <span class="scan-pass"><small>通過</small><b>${passLine.toLocaleString('ja-JP')}${esc(profile.unit_label)}未満</b></span>
          <span class="scan-play"><small>打ち始め</small><b>${Number(profile.start_threshold).toLocaleString('ja-JP')}${esc(profile.unit_label)}〜</b><em>${money(profile.expected_value_yen, true)}</em></span>
          <button type="button" data-scan-profile="${profile.id}">入力</button>
        </div>`;
      }).join('')}</div>
    </article>`).join('');
  const machineSelect = byId('patrol-machine');
  const current = machineSelect.value;
  machineSelect.innerHTML = '<option value="">機種を選ぶ</option>' + machineGroups.map(([machine]) => `<option value="${esc(machine)}">${esc(machine)}</option>`).join('');
  if ([...machineSelect.options].some(option => option.value === current)) machineSelect.value = current;
  const resetMachineSelect = byId('reset-record-machine');
  const resetCurrent = resetMachineSelect.value;
  resetMachineSelect.innerHTML = '<option value="">機種を選ぶ</option>' + machineGroups.map(([machine]) => `<option value="${esc(machine)}">${esc(machine)}</option>`).join('');
  if ([...resetMachineSelect.options].some(option => option.value === resetCurrent)) resetMachineSelect.value = resetCurrent;
  renderPatrol();
  renderResetTendency();
}

function activePatrolSession() {
  return state.patrol_sessions.find(session => !session.ended_at) || null;
}

function renderPatrol() {
  const session = activePatrolSession();
  const hall = byId('scan-hall')?.value || state.settings.scan_hall;
  const rows = state.patrol_observations.filter(row => !session || row.session_id === session.id).slice(0, 8);
  byId('patrol-session-button').textContent = session ? '巡回を終了' : '巡回を開始';
  byId('patrol-session-status').textContent = session
    ? `${session.hall_name}を巡回中・${state.patrol_observations.filter(row => row.session_id === session.id).length}台記録`
    : `${hall || '店舗未選択'}・巡回を始めると確認時刻と台の変化を保存します。`;
  byId('patrol-recent').innerHTML = rows.length ? rows.map(row => {
    const previous = state.patrol_observations.find(item => item.id !== row.id && item.hall_name === row.hall_name && item.seat_number === row.seat_number && item.observed_at < row.observed_at);
    const delta = previous ? Number(row.current_value) - Number(previous.current_value) : null;
    return `<div class="patrol-log-row"><b>${esc(row.seat_number)}番</b><div><strong>${esc(row.machine_name)}</strong><small>${new Date(row.observed_at).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' })}・${row.status === 'watch' ? '候補' : row.status === 'pass' ? '通過' : '稼働中'}</small></div><em>${Number(row.current_value).toLocaleString('ja-JP')}G${delta == null ? '' : `<small>${delta >= 0 ? '+' : ''}${delta}G</small>`}</em></div>`;
  }).join('') : '<p class="empty">巡回記録はまだありません。</p>';
}

function resetTendencyStats(hallName, machineName = '', weekday = null) {
  let rows = state.hall_reset_records.filter(row => row.hall_name === hallName && ['reset_confirmed', 'normal'].includes(row.reset_status));
  if (weekday !== null) rows = rows.filter(row => (new Date(`${row.recorded_on}T12:00:00`).getDay() + 6) % 7 === weekday);
  const machineRows = machineName ? rows.filter(row => row.machine_name === machineName) : [];
  if (machineRows.length >= 3) rows = machineRows;
  const resets = rows.filter(row => row.reset_status === 'reset_confirmed').length;
  const rate = rows.length ? resets / rows.length : null;
  const suggestion = rows.length >= 3 && rate >= .70 ? 'reset_confirmed'
    : rows.length >= 3 && rate <= .30 ? 'normal' : 'unknown';
  return { samples: rows.length, resets, rate, suggestion };
}

function renderResetTendency() {
  const hall = byId('scan-hall')?.value || byId('quick-hall')?.value || '';
  const machine = byId('reset-record-machine')?.value || byId('quick-machine')?.value || '';
  const rows = state.hall_reset_records.filter(row => !hall || row.hall_name === hall);
  byId('reset-tendency-badge').textContent = `${rows.length}件`;
  const stats = resetTendencyStats(hall, machine, (new Date().getDay() + 6) % 7);
  const label = stats.suggestion === 'reset_confirmed' ? 'リセット寄り'
    : stats.suggestion === 'normal' ? '据え置き寄り' : 'まだ判断不可';
  const recent = rows.slice(0, 3);
  byId('reset-tendency-summary').innerHTML = rows.length ? `
    <div class="reset-tendency-card"><div><strong>${esc(hall)}・今日と同じ曜日${machine ? `・${esc(machine)}` : ''}</strong><b>${esc(label)}</b></div><small>${stats.samples}件中リセット${stats.resets}件${stats.rate === null ? '' : `（${Math.round(stats.rate * 100)}%）`}。3件未満、または30～70%は自動確定しません。</small></div>
    ${recent.map(row => `<div class="reset-tendency-card"><div><strong>${esc(row.recorded_on)}・${esc(row.machine_name)}</strong><b>${row.reset_status === 'reset_confirmed' ? 'リセット' : row.reset_status === 'normal' ? '据え置き' : '不明'}</b></div><small>${esc(row.evidence || '根拠メモなし')}${row.seat_number ? `・${row.seat_number}番台` : ''}</small></div>`).join('')}`
    : '<p class="empty">確定記録がまだありません。</p>';
}

async function loadScanHall() {
  await loadTargetHallOptions();
  const hall = byId('scan-hall').value;
  const button = byId('scan-load-button');
  const status = byId('scan-status');
  button.disabled = true;
  status.textContent = '店舗の設置機種と期待値カタログを照合中...';
  state.settings.scan_hall = hall;
  await writeLocalState();
  try {
    const [snapshotResult, coverageResult] = await Promise.allSettled([
      fetch(apiUrl(`/api/hall/installation_snapshot?hall_name=${encodeURIComponent(hall)}&ts=${Date.now()}`), { cache: 'no-store' }),
      fetch(apiUrl(`/api/hall/data_coverage?hall_name=${encodeURIComponent(hall)}&ts=${Date.now()}`), { cache: 'no-store' }),
    ]);
    const response = snapshotResult.status === 'fulfilled' ? snapshotResult.value : null;
    if (!response?.ok) throw new Error(`設置機種API ${response?.status || '接続失敗'}`);
    scanSnapshot = await response.json();
    const coverageResponse = coverageResult.status === 'fulfilled' ? coverageResult.value : null;
    renderDataCoverage('scan-data-coverage', coverageResponse?.ok ? await coverageResponse.json() : null);
    renderScanList(scanSnapshot);
    status.textContent = scanSnapshot.snapshot_date
      ? `${scanSnapshot.snapshot_date}取得分から、対応している島だけに絞りました。`
      : '設置履歴がないため、登録済み全機種を表示します。';
    if (!scanSnapshot.machines?.length) renderScanList(scanSnapshot, true);
  } catch (error) {
    scanSnapshot = { hall_name: hall, machines: [] };
    renderScanList(scanSnapshot, true);
    renderDataCoverage('scan-data-coverage', null);
    status.textContent = `設置情報を取得できないため全対応機種を表示：${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function loadOccupancyPriorityList() {
  const target = byId('occupancy-priority-list');
  try {
    // Service Worker(mobile/sw.js)はGETをキャッシュ優先で返すため、都度ユニークなURLにして必ずネットワークから取り直す。
    const params = new URLSearchParams({ ts: String(Date.now()) });
    const prefecture = byId('hyena-store-prefecture')?.value || '';
    if (prefecture) params.set('prefecture', prefecture);
    const rows = await mobileArchiveRequest(`/api/occupancy/patrol-list?${params}`);
    occupancyPriorityData = rows;
    renderOccupancyPriorityList(rows);
  } catch (error) {
    target.innerHTML = `<p class="empty">巡回優先度を取得できません：${esc(error.message)}</p>`;
  }
}

function localDateTimeValue(date = new Date()) {
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

async function loadHyenaStoreRanking() {
  const target = byId('hyena-store-ranking');
  const atInput = byId('hyena-store-at');
  if (!atInput.value) atInput.value = localDateTimeValue();
  const prefecture = byId('hyena-store-prefecture').value;
  target.innerHTML = '<p class="empty">候補店を計算中...</p>';
  try {
    const params = new URLSearchParams({ at: atInput.value, limit: '20', ts: String(Date.now()) });
    if (prefecture) params.set('prefecture', prefecture);
    const data = await mobileArchiveRequest(`/api/occupancy/hyena-stores?${params}`);
    hyenaStoreRankingData = data;
    renderHyenaStoreRanking(data);
  } catch (error) {
    target.innerHTML = `<p class="empty">候補店を計算できません：${esc(error.message)}</p>`;
  }
}

function renderHyenaStoreRanking(data) {
  const target = byId('hyena-store-ranking');
  const rows = data?.halls || [];
  if (!rows.length) {
    target.innerHTML = '<p class="empty">対象店舗がありません。設定で店舗を登録し、データ収集を実行してください。</p>';
    return;
  }
  target.innerHTML = rows.map(row => {
    const occ = row.occupancy || {};
    const machines = row.machines || {};
    const reason = (row.reasons || []).slice(0, 2).join('・');
    const warning = (row.warnings || [])[0];
    return `<button class="hyena-store-row verdict-${esc(row.verdict)}" type="button" data-hyena-hall="${esc(row.hall_name)}">
      <span class="hyena-store-rank">${row.rank}</span>
      <span class="hyena-store-main"><strong>${esc(row.hall_name)}</strong><small>${esc(reason)}</small>${warning ? `<em>${esc(warning)}</em>` : ''}</span>
      <span class="hyena-store-score"><b>${Number(row.score || 0)}</b><small>/100</small><i>${esc(row.verdict_label)}</i></span>
      <span class="hyena-store-meta">対応 ${Number(machines.supported_machine_count || 0)}機種・混雑 ${esc(occ.predicted_label || '不明')}・信頼度 ${esc(occ.confidence_label || '不足')}</span>
    </button>`;
  }).join('');
  byId('hyena-store-notice').textContent = data.notice || '到着後は必ず個別台を判定してください。';
}

function renderOccupancyPriorityList(rows) {
  const target = byId('occupancy-priority-list');
  if (!rows?.length) {
    target.innerHTML = '<p class="empty">対象ホールがありません。</p>';
    return;
  }
  const levelLabel = { high: '高', mid: '中', low: '低' };
  target.innerHTML = rows.slice(0, 8).map(row => {
    const level = row.last_level;
    const badgeClass = level ? `level-${level}` : 'level-none';
    const badgeText = level ? (levelLabel[level] || level) : '未記録';
    const meta = row.hours_since == null
      ? '記録なし'
      : row.hours_since < 1
        ? '1時間以内に記録'
        : row.hours_since < 24
          ? `${Math.round(row.hours_since)}時間前に記録`
          : `${Math.round(row.hours_since / 24)}日前に記録`;
    return `<div class="occupancy-priority-row">
      <span class="occ-hall-name">${esc(row.hall_name)}</span>
      <span class="occ-level-badge ${badgeClass}">${esc(badgeText)}</span>
      <span class="occ-meta">${esc(meta)}</span>
    </div>`;
  }).join('');
}

async function recordOccupancyLevel(level) {
  const hall = byId('scan-hall').value;
  if (!hall) { showToast('先に店舗を選んでください'); return; }
  const status = byId('occupancy-status');
  try {
    const rotationValue = byId('occupancy-avg-rotation').value;
    const result = await mobileArchiveRequest('/api/occupancy', {
      method: 'POST',
      body: JSON.stringify({
        hall_name: hall,
        level,
        avg_rotation_games_per_hour: rotationValue === '' ? null : Number(rotationValue),
      }),
    });
    document.querySelectorAll('.occupancy-button').forEach(btn => {
      btn.classList.toggle('is-selected', btn.dataset.occupancyLevel === level);
    });
    const levelLabel = { high: '高', mid: '中', low: '低' }[level] || level;
    const recordedTime = new Date(result.recorded_at).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
    status.textContent = `${hall} を「${levelLabel}」で記録しました（${recordedTime}）`;
    showToast('稼働状況を記録しました');
    occupancyPriorityData = null;
    hyenaStoreRankingData = null;
    loadOccupancyPriorityList();
    loadHyenaStoreRanking();
  } catch (error) {
    status.textContent = `記録できませんでした：${error.message}`;
  }
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
    <article class="panel"><div class="section-bar"><h2>扱いが強い機種</h2><span class="count-badge">上位10</span></div><div class="trend-machine-list">${topMachines.slice(0, 10).map((machine, index) => `<div><span class="target-rank">${index + 1}</span><div><strong>${esc(machine.machine_name)}</strong><small>${machine.sample_days}日・信頼${machine.reliability_pct ?? 0}%・プラス率${machine.positive_rate}%${machine.trend == null ? '' : `・直近差${signedCoins(machine.trend)}`}</small></div><b class="${machine.avg_diff >= 0 ? 'money-up' : 'money-down'}">補正${signedCoins(machine.avg_diff)}</b><button type="button" data-trend-machine="${index}">朝一候補に保存</button></div>`).join('')}</div></article>
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
    const [profileResponse, aiResponse, coverageResponse] = await Promise.all([
      fetch(apiUrl(`/api/hall/trend_profile?${query}`)),
      fetch(apiUrl(`/api/ai/hall_profile?${query}`)).catch(() => null),
      fetch(apiUrl(`/api/hall/data_coverage?hall_name=${encodeURIComponent(hall)}&ts=${Date.now()}`), { cache: 'no-store' }).catch(() => null),
    ]);
    if (!profileResponse.ok) throw new Error(`傾向API ${profileResponse.status}`);
    trendData = await profileResponse.json();
    const aiData = aiResponse?.ok ? await aiResponse.json() : null;
    renderDataCoverage('trend-data-coverage', coverageResponse?.ok ? await coverageResponse.json() : null);
    renderTrendProfile(aiData);
    status.textContent = `${trendData.sample_days || 0}日分・信頼度${trendData.confidence || '不足'}で分析`;
  } catch (error) {
    renderDataCoverage('trend-data-coverage', null);
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
  const floorSources = (floorData.floor_map_sources || []).map(item => `<a href="${esc(safeUrl(item.image_url))}" target="_blank" rel="noopener">公式掲載マップ${item.floor_index}</a>`).join(' ');
  meta.innerHTML = `<span>${esc(layout.floor_name)}・${esc(layout.verification_status)}</span><small>${esc(layout.source_label || '利用者登録マップ')}・台番号実績 ${floorData.data_coverage.seat_count}台</small>${layout.source_url ? `<a href="${esc(safeUrl(layout.source_url))}" target="_blank" rel="noopener">出典を開く</a>` : ''}${floorSources}`;
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
  const region = setTargetRegion(byId('target-search-region').value);
  const days = byId('target-search-days').value;
  const status = byId('target-search-status');
  status.textContent = '店舗・曜日・機種データを分析中...';
  byId('target-search-button').disabled = true;
  try {
    const response = await fetch(apiUrl(`/api/hall/target_search?visit_date=${encodeURIComponent(visitDate)}&days=${encodeURIComponent(days)}&region=${encodeURIComponent(region)}`));
    if (!response.ok) throw new Error(`分析API ${response.status}`);
    targetSearchData = await response.json();
    renderTargetSearch();
    status.textContent = `${targetSearchData.region_label}・${targetSearchData.generated_at}時点の公開データで分析しました。`;
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
    const location = [result.hall_name, result.seat_number ? `${result.seat_number}番台` : ''].filter(Boolean).join('・');
    const detail = [result.outcome === 'hit' ? '当選' : result.outcome === 'closing' ? '閉店終了' : result.outcome === 'stop' ? '見切り' : '', result.hit_game != null ? `当選${result.hit_game}G` : '', result.end_state].filter(Boolean).join('・');
    return `<article class="list-card"><div class="list-card-head"><div class="list-card-main"><strong>${esc(result.machine_name)}</strong><small>${esc(result.played_on)}・${result.played_minutes || 0}分${location ? `・${esc(location)}` : ''}</small></div><strong class="${net >= 0 ? 'result-profit' : 'result-loss'}">${money(net, true)}</strong></div><p class="card-meta">投資 ${money(result.investment_yen)}・回収 ${money(result.returns_yen)}${hasExpected ? `・期待値 ${money(result.expected_value_yen, true)}・差 ${money(gap, true)}` : '・期待値記録なし'}${detail ? `・${esc(detail)}` : ''}${result.notes ? `・${esc(result.notes)}` : ''}</p></article>`;
  }).join('');
}

function renderValidation() {
  const rows = buildValidationSummary(state.results);
  byId('validation-summary').innerHTML = rows.length ? rows.map(row => `
    <div class="validation-row"><div><strong>${esc(row.machine_name)} <small>${row.count}件</small></strong><span class="validation-status ${row.sample_level}">${row.sample_label}</span></div>
    <p>累計期待値 ${money(row.expected_yen, true)}／実収支 ${money(row.actual_yen, true)}／差 ${money(row.gap_yen, true)}${row.avg_minutes ? `／平均${row.avg_minutes}分` : ''}</p></div>`).join('')
    : '<p class="empty">期待値付きの実戦結果がまだありません。</p>';
}

function renderSettings() {
  byId('budget-bankroll').value = state.budget.starting_bankroll || '';
  byId('budget-loss').value = state.budget.loss_limit_yen || '';
  byId('quick-close').value = state.settings.closing_time || '22:45';
  byId('sync-key').value = state.sync.key || '';
  const syncStatus = byId('sync-status');
  syncStatus.textContent = state.sync.enabled
    ? (state.sync.pending
      ? `未送信 ${Number(state.sync.pending_count || 1)}件・電波復帰後に自動送信${state.sync.last_error && state.sync.last_error !== 'offline' ? `・${state.sync.last_error}` : ''}`
      : `同期オン${state.sync.last_synced_at ? `・最終 ${new Date(state.sync.last_synced_at).toLocaleString('ja-JP')}` : ''}`)
    : '同期はオフです。';
  syncStatus.classList.toggle('sync-queue-note', Boolean(state.sync.pending));
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
  renderValidation();
  renderPatrol();
  renderResetTendency();
  renderSettings();
}

async function loadJugglerCatalog() {
  if (jugglerCatalog.length) return jugglerCatalog;
  const response = await fetch(apiUrl('/api/juggler/catalog'));
  if (!response.ok) throw new Error('ジャグラー機種一覧を読み込めません');
  jugglerCatalog = await response.json();
  byId('juggler-machine').innerHTML = jugglerCatalog
    .map(profile => `<option value="${esc(profile.id)}">${esc(profile.name)}</option>`).join('');
  return jugglerCatalog;
}

function probabilityLabel(value) {
  return `${Number(value || 0).toFixed(1)}%`;
}

function renderJugglerAssessment(result) {
  const tone = result.action === '続行候補' ? 'continue'
    : result.action === '見送り候補' ? 'stop'
      : result.action === '判定保留' ? 'hold' : 'watch';
  const denominator = value => value ? `1/${Number(value).toLocaleString('ja-JP')}` : '当選なし';
  const probabilities = Object.entries(result.setting_probabilities_pct || {}).map(([setting, value]) => `
    <div class="juggler-probability"><span>設定${esc(setting)}</span><i style="--probability:${Math.min(100, Number(value || 0))}%"></i><b>${probabilityLabel(value)}</b></div>
  `).join('');
  byId('juggler-assess-result').innerHTML = `
    <article class="juggler-result-card ${tone}">
      <div class="juggler-result-head"><div><span class="page-step">LIVE ASSESSMENT</span><h2>${esc(result.machine_name)}</h2></div><span class="juggler-action">${esc(result.action)}</span></div>
      <div class="juggler-metrics"><span>BIG<b>${denominator(result.bb_denominator)}</b></span><span>REG<b>${denominator(result.rb_denominator)}</b></span><span>合算<b>${denominator(result.combined_denominator)}</b></span></div>
      <p class="juggler-high">設定4以上の相対確率 ${Number(result.high_setting_probability_pct || 0)}%・信頼度 ${esc(result.confidence)}</p>
      <p class="juggler-result-reason">${esc(result.reason)}</p>
      <div class="juggler-probabilities">${probabilities}</div>
      <a class="juggler-source" href="${esc(result.source_url)}" target="_blank" rel="noopener">北電子の公式スペックを確認 ↗</a>
      <p class="juggler-result-note">${esc(result.notice)}</p>
    </article>`;
}

async function runJugglerAssessment() {
  const button = byId('juggler-assess-button');
  button.disabled = true;
  button.textContent = '計算中…';
  try {
    await loadJugglerCatalog();
    const response = await fetch(apiUrl('/api/juggler/assess'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        profile_id: byId('juggler-machine').value,
        games: Number(byId('juggler-games').value),
        bb_count: Number(byId('juggler-bb').value),
        rb_count: Number(byId('juggler-rb').value),
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || '判定できません');
    renderJugglerAssessment(data);
  } catch (error) {
    byId('juggler-assess-result').innerHTML = `<p class="juggler-empty">${esc(error.message)}</p>`;
  } finally {
    button.disabled = false;
    button.textContent = '設定傾向を判定';
  }
}

function renderJugglerTargets(data) {
  const coverage = data.data_coverage || {};
  byId('juggler-target-status').textContent = `${data.region_label || ''}：${Number(coverage.rows || 0).toLocaleString('ja-JP')}台日・${coverage.days || 0}日・${coverage.halls || 0}店舗`;
  const candidates = data.candidates || [];
  const coverageHtml = `<div class="juggler-coverage"><b>収集状況</b>　${Number(coverage.rows || 0).toLocaleString('ja-JP')}台日 / ${coverage.days || 0}営業日 / ${coverage.halls || 0}店舗${coverage.latest_date ? `・最新 ${esc(coverage.latest_date)}` : ''}<br>${esc(data.notice || '')}</div>`;
  if (!candidates.length) {
    byId('juggler-target-results').innerHTML = `${coverageHtml}<div class="juggler-empty">まだ朝一候補を出せる量のジャグラー履歴がありません。取得機能は対応済みで、収集後に自動で候補が育ちます。</div>`;
    return;
  }
  byId('juggler-target-results').innerHTML = coverageHtml + candidates.map(item => `
    <article class="juggler-target-card">
      <div class="juggler-target-head"><div><span class="page-step">#${item.rank} ${esc(item.action)}</span><strong>${esc(item.hall_name)}・${item.seat_number == null ? '機種候補' : `${esc(item.seat_number)}番台`}</strong></div><span>${item.score}点</span></div>
      <p>${esc(item.machine_name)}<br>${esc(item.reason)}</p>
      <dl><div><dt>高設定寄り</dt><dd>${item.strong_rate_pct}%</dd></div><div><dt>平均差枚</dt><dd>${Number(item.avg_diff || 0) >= 0 ? '+' : ''}${Number(item.avg_diff || 0).toLocaleString('ja-JP')}枚</dd></div><div><dt>サンプル</dt><dd>${item.sample_days}日</dd></div></dl>
      <p class="juggler-validation">過去検証：狙い時 ${item.validation?.recommendation_success_pct ?? '--'}%／${item.validation?.test_days ?? 0}日・${esc(item.validation?.trust_level || 'データ不足')}</p>
    </article>`).join('');
}

async function runJugglerTargets() {
  const button = byId('juggler-target-button');
  button.disabled = true;
  button.textContent = '分析中…';
  const params = new URLSearchParams({
    visit_date: byId('juggler-visit-date').value,
    days: byId('juggler-days').value,
    region: byId('juggler-region').value,
    limit: '20',
  });
  try {
    const response = await fetch(apiUrl(`/api/juggler/targets?${params}`));
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || '朝一候補を分析できません');
    jugglerTargetData = data;
    renderJugglerTargets(data);
  } catch (error) {
    byId('juggler-target-results').innerHTML = `<p class="juggler-empty">${esc(error.message)}</p>`;
  } finally {
    button.disabled = false;
    button.textContent = '朝一候補を分析';
  }
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
  else if (['check', 'scan', 'guide'].includes(name)) activeModule = 'hyena';
  else if (name === 'juggler') activeModule = 'juggler';
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
  byId('brand-mode-label').textContent = activeModule === 'hyena' ? 'ハイエナ専用' : activeModule === 'target' ? '狙い台捜索専用' : activeModule === 'juggler' ? 'ジャグラー専用' : 'スロット攻略ホーム';
  scrollTo({ top: 0, behavior: 'smooth' });
  if (name === 'scan' && !scanSnapshot) setTimeout(loadScanHall, 80);
  if (name === 'scan' && !occupancyPriorityData) setTimeout(loadOccupancyPriorityList, 80);
  if (name === 'scan' && !hyenaStoreRankingData) setTimeout(loadHyenaStoreRanking, 100);
  if (name === 'target-map' && !targetMapData) setTimeout(loadTargetHeatMap, 80);
  if (name === 'trend' && !trendData) setTimeout(loadTrendProfile, 80);
  if (name === 'floor-map' && !floorData) setTimeout(loadFloorHeat, 80);
  if (name === 'juggler') {
    loadJugglerCatalog().catch(error => { byId('juggler-target-status').textContent = error.message; });
    if (!jugglerTargetData) setTimeout(runJugglerTargets, 80);
  }
  if (name === 'settings') {
    setTimeout(loadCollectionHealth, 40);
    setTimeout(loadMobileArchiveCollector, 80);
    setTimeout(loadValueCrawlerStatus, 120);
  }
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
byId('quick-current').addEventListener('input', updateEffectiveValue);
byId('quick-reset').addEventListener('change', updateEffectiveValue);
byId('quick-extra-fields').addEventListener('input', updateEffectiveValue);
byId('quick-templates').addEventListener('click', event => {
  const button = event.target.closest('[data-quick-template]');
  if (!button) return;
  const profile = currentProfile();
  if (button.dataset.quickTemplate === 'zero') byId('quick-current').value = 0;
  if (button.dataset.quickTemplate === 'heaven') {
    byId('quick-current').value = Number(profile?.heaven_exit_games ?? 32);
    showToast('天国抜けは機種差があるため、32Gは画面と照合してください');
  }
  if (button.dataset.quickTemplate === 'ceiling') {
    if (profile?.ceiling_threshold == null) return showToast('この条件は天井値が未登録です');
    byId('quick-current').value = Number(profile.ceiling_threshold);
  }
  if (button.dataset.quickTemplate === 'miss') {
    const missInput = [...document.querySelectorAll('[data-profile-input]')]
      .find(input => /miss|through|スルー/i.test(input.dataset.profileInput || ''));
    if (!missInput) return showToast('この条件にはスルー回数欄がありません');
    missInput.value = Number(missInput.value || 0) + 1;
  }
  updateEffectiveValue();
  byId('quick-current').focus();
});
byId('quick-ocr-file').addEventListener('change', async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  const status = byId('quick-ocr-status');
  status.className = 'reading';
  status.textContent = '画像を解析しています…';
  try {
    const result = await recognizeNumberFromFile(file);
    byId('quick-current').value = result.value;
    updateEffectiveValue();
    status.className = 'success';
    status.textContent = `${result.value.toLocaleString('ja-JP')} を読み取りました（${result.method}）。台の表示と一致するか確認してください。`;
    showToast(`OCR候補 ${result.value}Gを入力しました`);
  } catch (error) {
    status.className = '';
    status.textContent = error.message;
    showToast('OCRできませんでした。手入力してください');
  } finally {
    event.target.value = '';
  }
});
['quick-exchange', 'quick-funding', 'quick-section-diff'].forEach(id => byId(id).addEventListener('change', updateAdjustmentNote));
['quick-exchange-rate', 'quick-replay-limit', 'quick-replay-used'].forEach(id => byId(id).addEventListener('change', persistReplayUsageInputs));
byId('quick-hall').addEventListener('change', () => {
  loadReplayUsageInputs();
  renderResetTendency();
});
byId('quick-close').addEventListener('change', async event => {
  state.settings.closing_time = event.target.value;
  await writeLocalState();
});

byId('scan-hall-form').addEventListener('submit', async event => {
  event.preventDefault();
  await loadScanHall();
});
byId('occupancy-priority-refresh').addEventListener('click', () => {
  occupancyPriorityData = null;
  loadOccupancyPriorityList();
});
byId('hyena-store-refresh').addEventListener('click', () => {
  hyenaStoreRankingData = null;
  loadHyenaStoreRanking();
});
byId('hyena-store-at').addEventListener('change', () => {
  hyenaStoreRankingData = null;
  loadHyenaStoreRanking();
});
byId('hyena-store-prefecture').value = localStorage.getItem('pachi-hyena-prefecture') || '';
byId('hyena-store-prefecture').addEventListener('change', event => {
  localStorage.setItem('pachi-hyena-prefecture', event.target.value);
  hyenaStoreRankingData = null;
  occupancyPriorityData = null;
  loadHyenaStoreRanking();
  loadOccupancyPriorityList();
});
byId('hyena-store-ranking').addEventListener('click', event => {
  const button = event.target.closest('[data-hyena-hall]');
  if (!button) return;
  byId('scan-hall').value = button.dataset.hyenaHall;
  loadScanHall();
  byId('scan-hall-form').scrollIntoView({ behavior: 'smooth', block: 'start' });
});
document.querySelectorAll('.occupancy-button').forEach(button => {
  button.addEventListener('click', () => recordOccupancyLevel(button.dataset.occupancyLevel));
});
byId('scan-strategy-time').addEventListener('input', renderTimeStrategy);
byId('scan-time-tabs').addEventListener('click', event => {
  const button = event.target.closest('[data-strategy-time]');
  if (!button) return;
  byId('scan-strategy-time').value = button.dataset.strategyTime;
  renderTimeStrategy();
});
byId('patrol-session-button').addEventListener('click', async () => {
  const active = activePatrolSession();
  if (active) active.ended_at = new Date().toISOString();
  else state.patrol_sessions.unshift({ id: newId(), hall_name: byId('scan-hall').value, started_at: new Date().toISOString(), ended_at: null });
  await writeLocalState();
  renderPatrol();
  showToast(active ? '巡回を終了しました' : '巡回を開始しました');
});
byId('patrol-observation-form').addEventListener('submit', async event => {
  event.preventDefault();
  let session = activePatrolSession();
  if (!session) {
    session = { id: newId(), hall_name: byId('scan-hall').value, started_at: new Date().toISOString(), ended_at: null };
    state.patrol_sessions.unshift(session);
  }
  const observedAt = new Date().toISOString();
  state.patrol_observations.unshift({
    id: newId(), session_id: session.id, observed_at: observedAt,
    time_bucket: `${String(new Date().getHours()).padStart(2, '0')}:00`, hall_name: session.hall_name,
    seat_number: Number(byId('patrol-seat').value), machine_name: byId('patrol-machine').value,
    current_value: Number(byId('patrol-current').value), status: byId('patrol-status').value,
  });
  await writeLocalState();
  byId('patrol-seat').value = '';
  byId('patrol-current').value = '';
  renderPatrol();
  showToast('台チェックを記録しました');
});

byId('reset-record-machine').addEventListener('change', renderResetTendency);
byId('reset-record-form').addEventListener('submit', async event => {
  event.preventDefault();
  const hall = byId('scan-hall').value;
  if (!hall) return showToast('先に店舗を選んでください');
  state.hall_reset_records.unshift({
    id: newId(),
    recorded_on: byId('reset-record-date').value || todayValue(),
    hall_name: hall,
    machine_name: byId('reset-record-machine').value,
    seat_number: byId('reset-record-seat').value ? Number(byId('reset-record-seat').value) : null,
    reset_status: byId('reset-record-status').value,
    evidence: byId('reset-record-evidence').value.trim(),
    notes: '',
    created_at: new Date().toISOString(),
  });
  state.hall_reset_records = state.hall_reset_records.slice(0, 2000);
  await writeLocalState();
  byId('reset-record-seat').value = '';
  byId('reset-record-evidence').value = '';
  renderResetTendency();
  showToast('リセット・据え置き記録を保存しました');
});

byId('scan-machine-list').addEventListener('click', event => {
  const button = event.target.closest('[data-scan-profile]');
  if (!button) return;
  const profileId = Number(button.dataset.scanProfile);
  populateMachines(profileId);
  byId('quick-hall').value = byId('scan-hall').value;
  byId('quick-current').value = '';
  showScreen('check');
  setTimeout(() => byId('quick-current').focus(), 250);
  showToast('近い台だけ履歴を確認して数値を入力');
});

byId('target-search-form').addEventListener('submit', async event => {
  event.preventDefault();
  await runTargetSearch();
});

byId('juggler-assess-form').addEventListener('submit', async event => {
  event.preventDefault();
  await runJugglerAssessment();
});

byId('juggler-target-form').addEventListener('submit', async event => {
  event.preventDefault();
  setTargetRegion(byId('juggler-region').value);
  await runJugglerTargets();
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
  if (!machine.action?.startsWith('狙う')) {
    showToast(`保存できません：${machine.action_reason || '検証基準に未達です'}`);
    return;
  }
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
    notes: `検証済み候補：${machine.action}／店舗${hall.score}点／機種${machine.score}点／狙い時成功${machine.validation?.recommendation_success_pct ?? '--'}%／平均${signedCoins(machine.avg_diff)}／${machine.sample_days}日`,
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

byId('quick-form').addEventListener('submit', async event => {
  event.preventDefault();
  const profile = currentProfile();
  const valueState = effectiveCurrentValue();
  const currentValue = valueState.effective;
  const dynamicInputs = adjustmentInputs();
  const baseResult = assessQuick({
    profile,
    currentValue,
    riskCapacityYen: calculateSummary(state).risk_capacity_yen,
    exchangeType: byId('quick-exchange').value,
    fundingMode: byId('quick-funding').value,
    resetStatus: byId('quick-reset').value,
    minutesUntilClose: minutesUntilClosing(byId('quick-close').value),
    extraInputs: valueState.inputs,
    ...dynamicInputs,
  });
  const result = applyPersonalCalibration(baseResult, profile, state.results);
  await persistReplayUsageInputs();
  lastAssessment = { result, profile, currentValue, rawCurrentValue: valueState.current, extraInputs: valueState.inputs, dynamicInputs };
  renderQuickResult(result, profile, currentValue);
  setTimeout(() => byId('quick-result').scrollIntoView({ behavior: 'smooth', block: 'start' }), 60);
});

byId('quick-result').addEventListener('click', async event => {
  if (!event.target.closest('#save-candidate-button') || !lastAssessment?.result.actionable) return;
  const { result, profile, currentValue, rawCurrentValue, extraInputs, dynamicInputs } = lastAssessment;
  state.candidates.unshift({
    id: newId(),
    created_at: new Date().toISOString(),
    catalog_key: profile.catalog_key,
    machine_name: profile.machine_name,
    condition_label: profile.condition_label,
    unit_label: profile.unit_label,
    current_value: currentValue,
    raw_current_value: rawCurrentValue,
    hall_name: byId('quick-hall').value,
    seat_number: byId('quick-seat').value ? Number(byId('quick-seat').value) : null,
    observed_at: new Date().toISOString(),
    profile_inputs: extraInputs,
    exchange_type: byId('quick-exchange').value,
    funding_mode: byId('quick-funding').value,
    reset_status: byId('quick-reset').value,
    estimated_play_minutes: result.estimated_play_minutes,
    ev_per_hour_yen: result.ev_per_hour_yen,
    expected_value_yen: result.expected_value_yen,
    worst_case_investment_yen: result.worst_case_investment_yen,
    base_expected_value_yen: result.base_expected_value_yen,
    cash_gap_yen: result.cash_gap_yen || 0,
    adjusted_start_threshold: result.adjusted_start_threshold,
    section_difference_coins: dynamicInputs.sectionDifferenceCoins,
    replay_limit_medals: dynamicInputs.replayLimitMedals,
    replay_used_medals: dynamicInputs.replayUsedMedals,
    exchange_rate: dynamicInputs.exchangeRate,
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
    byId('result-context').textContent = [candidate.hall_name, candidate.seat_number ? `${candidate.seat_number}番台` : '', `${candidate.current_value}${candidate.unit_label || 'G'}から`, candidate.condition_label].filter(Boolean).join('・');
    byId('result-date').value = todayValue();
    byId('result-investment').value = '';
    byId('result-returns').value = '';
    byId('result-minutes').value = '';
    byId('result-outcome').value = 'hit';
    byId('result-hit-game').value = '';
    byId('result-end-game').value = '';
    byId('result-end-state').value = '';
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
    catalog_key: candidate.catalog_key,
    hall_name: candidate.hall_name || '',
    seat_number: candidate.seat_number || null,
    start_value: candidate.current_value,
    condition_label: candidate.condition_label,
    exchange_type: candidate.exchange_type,
    funding_mode: candidate.funding_mode,
    reset_status: candidate.reset_status,
    profile_inputs: candidate.profile_inputs || {},
    played_on: byId('result-date').value,
    expected_value_yen: candidate.expected_value_yen !== null && candidate.expected_value_yen !== undefined ? Number(candidate.expected_value_yen) : null,
    investment_yen: Number(byId('result-investment').value),
    returns_yen: Number(byId('result-returns').value),
    played_minutes: Number(byId('result-minutes').value || 0),
    outcome: byId('result-outcome').value,
    hit_game: byId('result-hit-game').value === '' ? null : Number(byId('result-hit-game').value),
    end_game: byId('result-end-game').value === '' ? null : Number(byId('result-end-game').value),
    end_state: byId('result-end-state').value.trim(),
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

byId('quick-result').addEventListener('change', event => {
  if (!event.target.matches('[data-seat-confirm]')) return;
  const checks = [...byId('quick-result').querySelectorAll('[data-seat-confirm]')];
  const button = byId('save-candidate-button');
  if (!button) return;
  const confirmed = checks.length > 0 && checks.every(input => input.checked);
  button.disabled = !confirmed;
  button.textContent = confirmed ? 'この台を候補に保存' : `${checks.filter(input => input.checked).length}/4 確認済み`;
});

let mobileArchivePoll = null;

async function loadValueCrawlerStatus() {
  const target = byId('mobile-value-crawl-status');
  if (!target) return;
  try {
    const data = await mobileArchiveRequest('/api/opportunity/crawler/status');
    const run = data.last_run;
    const counts = data.candidate_counts || {};
    target.innerHTML = run
      ? `<strong>${run.status === 'running' ? '確認中' : '前回確認済み'}</strong><br>確認 ${Number(run.checked_count || 0)}件・新規候補 ${Number(run.candidate_count || 0)}件・エラー ${Number(run.error_count || 0)}件<br>承認待ち ${Number(counts.pending || 0)}件・差異あり ${Number(counts.conflict || 0)}件${data.supabase_configured ? '<br>Supabase同期：設定済み' : '<br>Supabase同期：未設定（ローカルDBのみ）'} `
      : 'まだ期待値ソースの確認履歴はありません。';
  } catch (error) {
    target.textContent = `状態を取得できません：${error.message}`;
  }
}

async function loadCollectionHealth() {
  const target = byId('mobile-collection-health');
  if (!target) return;
  try {
    const data = await mobileArchiveRequest('/api/scrape/health');
    const labels = {
      public_machine_daily: 'スマスロ・ジャグラー日次', pworld_snapshot: '設置機種',
      dmm_store_snapshot: '四條畷店・フロアマップ', minrepo_daily: '機種別差枚',
      minrepo_startup: '起動時補完',
    };
    const rows = (data.sources || []).filter(item =>
      labels[item.source] || String(item.source).includes('キコーナ四條畷店')
    );
    const badge = data.overall === 'healthy' ? '正常' : data.overall === 'not_started' ? '未実行' : '一部要確認';
    target.innerHTML = `<strong>${data.scheduler_running ? '自動収集ON' : '自動収集停止'}・${badge}</strong><br>${rows.length
      ? rows.map(item => `${item.status === 'success' ? '✓' : item.status === 'partial' ? '△' : '×'} ${esc(labels[item.source] || item.source)}：${esc(String(item.finished_at || '').replace('T', ' '))}・${Number(item.records || 0).toLocaleString('ja-JP')}件${item.error ? `（${esc(item.error)}）` : ''}`).join('<br>')
      : '実行履歴はまだありません。サーバー起動後の初回収集を待っています。'}`;
  } catch (error) {
    target.textContent = `収集状態を取得できません：${error.message}`;
  }
}

async function mobileArchiveRequest(path, options = {}) {
  let response;
  try {
    response = await fetch(apiUrl(path), {
      ...options,
      headers: options.body ? { 'Content-Type': 'application/json', ...(options.headers || {}) } : options.headers,
    });
  } catch (_) {
    throw new Error('収集サーバーに接続できません');
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `通信エラー ${response.status}`);
  return payload;
}

function archiveLocalDateOffset(days) {
  const now = new Date();
  now.setDate(now.getDate() + days);
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}

async function loadMobileArchiveCollector() {
  const statusEl = byId('mobile-archive-status');
  if (!statusEl) return;
  try {
    const data = await mobileArchiveRequest('/api/scrape/archive/status');
    const hall = byId('mobile-archive-hall');
    const selected = hall.value;
    hall.innerHTML = (data.supported_halls || []).map(item => `<option value="${esc(item.hall_name)}">${esc(item.hall_name)}</option>`).join('');
    if (selected && [...hall.options].some(option => option.value === selected)) hall.value = selected;
    if (!byId('mobile-archive-from').value) byId('mobile-archive-from').value = archiveLocalDateOffset(-365);
    if (!byId('mobile-archive-to').value) byId('mobile-archive-to').value = archiveLocalDateOffset(-1);
    const job = data.job;
    const labels = { queued: '開始待ち', collecting: '収集中', paused: '一時停止中', completed: '収集完了', failed: '失敗' };
    statusEl.innerHTML = job
      ? `<strong>${labels[job.status] || esc(job.status)}</strong><br>${esc(job.hall_name)}　${esc(job.date_from)}〜${esc(job.date_to)}<br>処理 ${job.processed}/${job.discovered}ページ（${job.progress_pct}%）<br>新規保存：機種 ${job.machine_rows}件・台 ${job.seat_rows}件　失敗 ${job.failed_count}件${job.error ? `<br><span>${esc(job.error)}</span>` : ''}`
      : 'まだ収集履歴はありません。店舗と期間を選んで開始してください。';
    byId('mobile-archive-coverage').innerHTML = (data.coverage || []).map(item =>
      `${esc(item.hall_name)}：${item.days || 0}日${item.oldest ? `（${esc(item.oldest)}〜${esc(item.newest)}）` : ''}`
    ).join('<br>');
    const running = job && ['queued', 'collecting'].includes(job.status);
    byId('mobile-archive-start').disabled = Boolean(running || job?.status === 'paused');
    byId('mobile-archive-pause').disabled = !running;
    byId('mobile-archive-resume').disabled = job?.status !== 'paused';
    clearTimeout(mobileArchivePoll);
    if (running && document.visibilityState === 'visible') mobileArchivePoll = setTimeout(loadMobileArchiveCollector, 3000);
  } catch (error) {
    statusEl.textContent = `進捗を取得できません：${error.message}。公開サーバーが最新版か確認してください。`;
  }
}

byId('mobile-archive-start').addEventListener('click', async () => {
  const body = {
    hall_name: byId('mobile-archive-hall').value,
    date_from: byId('mobile-archive-from').value,
    date_to: byId('mobile-archive-to').value,
    max_pages: Number(byId('mobile-archive-max').value),
  };
  if (!body.hall_name || !body.date_from || !body.date_to) return showToast('店舗と期間を指定してください');
  try {
    await mobileArchiveRequest('/api/scrape/archive/jobs', { method: 'POST', body: JSON.stringify(body) });
    showToast('過去データ収集を開始しました');
    await loadMobileArchiveCollector();
  } catch (error) { showToast(`開始できません：${error.message}`); }
});

byId('mobile-archive-pause').addEventListener('click', async () => {
  try {
    const data = await mobileArchiveRequest('/api/scrape/archive/pause', { method: 'POST' });
    showToast(data.message);
    setTimeout(loadMobileArchiveCollector, 600);
  } catch (error) { showToast(error.message); }
});

byId('mobile-archive-resume').addEventListener('click', async () => {
  try {
    const data = await mobileArchiveRequest('/api/scrape/archive/resume', { method: 'POST' });
    showToast(data.message);
    await loadMobileArchiveCollector();
  } catch (error) { showToast(error.message); }
});

byId('mobile-value-crawl-run').addEventListener('click', async () => {
  try {
    await mobileArchiveRequest('/api/opportunity/crawler/run', { method: 'POST' });
    showToast('公開元の確認を開始しました');
    setTimeout(loadValueCrawlerStatus, 1200);
  } catch (error) { showToast(`開始できません：${error.message}`); }
});

byId('sync-enable-button').addEventListener('click', async () => {
  if (!byId('sync-key').value.trim()) byId('sync-key').value = `${newId().replace(/-/g, '')}${newId().replace(/-/g, '')}`;
  state.sync.key = byId('sync-key').value.trim();
  state.sync.enabled = true;
  await pushMobileSync();
});
byId('sync-push-button').addEventListener('click', () => pushMobileSync());
byId('sync-pull-button').addEventListener('click', () => {
  if (state.results.length || state.candidates.length || state.patrol_observations.length) {
    if (!confirm('この端末の現在データをサーバー保存データで置き換えますか？')) return;
  }
  pullMobileSync();
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

window.addEventListener('online', () => {
  updateNetworkBadge();
  if (state.sync?.enabled && state.sync?.pending) pushMobileSync(true);
});
window.addEventListener('offline', () => {
  updateNetworkBadge();
  renderSettings();
});

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
    byId('juggler-visit-date').value = tomorrowValue();
    byId('juggler-region').value = storedTargetRegion();
    byId('target-map-date').value = tomorrowValue();
    setTargetRegion(storedTargetRegion());
    byId('trend-date').value = tomorrowValue();
    byId('floor-date').value = tomorrowValue();
    byId('floor-valid-from').value = todayValue();
    byId('floor-result-date').value = todayValue();
    byId('reset-record-date').value = todayValue();
    byId('scan-strategy-time').value = currentTimeValue();
    renderTimeStrategy();
    await loadTargetHallOptions();
    populateMachines();
    loadReplayUsageInputs();
    renderAll();
    renderResetTendency();
    updateNetworkBadge();
    if (state.sync?.enabled && state.sync?.pending && navigator.onLine) pushMobileSync(true);
    clearInterval(syncHeartbeat);
    syncHeartbeat = setInterval(() => {
      if (document.visibilityState === 'visible' && state.sync?.enabled && state.sync?.pending && navigator.onLine) pushMobileSync(true);
    }, 30000);
    if ('serviceWorker' in navigator) {
      await navigator.serviceWorker.register('./sw.js', { scope: './' });
    }
  } catch (error) {
    byId('guide-list').innerHTML = `<p class="empty">起動失敗：${esc(error.message)}<br>一度オンラインで開き直してください。</p>`;
    updateNetworkBadge();
  }
}

initialize();
