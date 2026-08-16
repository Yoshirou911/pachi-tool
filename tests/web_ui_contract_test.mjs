import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const desktopHtml = readFileSync(new URL('../web/index.html', import.meta.url), 'utf8');
const desktopJs = readFileSync(new URL('../web/js/app.js', import.meta.url), 'utf8');
const desktopCss = readFileSync(new URL('../web/css/style.css', import.meta.url), 'utf8');
const mobileHtml = readFileSync(new URL('../mobile/index.html', import.meta.url), 'utf8');

assert.match(desktopHtml, /id="page-home" class="page active"/, 'PC・ソフト版はホームから開始する');
assert.match(desktopHtml, /data-tab="home"/, 'PC・ソフト版のメニューからホームへ戻れる');
assert.match(desktopHtml, /data-home-destination="opportunity"/, 'ホームからハイエナへ進める');
assert.match(desktopHtml, /data-home-destination="target-search"/, 'ホームから狙い台捜索へ進める');
assert.match(desktopHtml, /id="page-target-search"/, '分析ランキング専用画面が必要');
assert.match(desktopHtml, /id="page-trend-profile"/, '店舗傾向カルテを独立画面にする');
assert.match(desktopHtml, /id="page-floor-map"/, '店内座席ヒートマップを独立画面にする');
assert.match(desktopHtml, /id="desktop-heat-form"/, '指定日で店舗ヒートマップを更新できる');
assert.match(desktopHtml, /id="desktop-heat-long-days"/, 'PC・ソフト版でも長期表示期間を選べる');
assert.match(desktopHtml, /id="desktop-heat-detail"/, '店舗ごとの長期傾向を表示できる');
assert.match(desktopJs, /api\/map\/target_heat/, '日付別ヒートマップAPIを利用する');
assert.match(desktopJs, /api\/hall\/trend_profile/, '店舗傾向APIを利用する');
assert.match(desktopJs, /api\/layouts\/seat_heat/, '店内座席ヒートAPIを利用する');
assert.match(desktopHtml, /data-module-nav="hyena"/, 'ハイエナ専用メニューが必要');
assert.match(desktopHtml, /data-module-nav="target"/, '狙い台専用メニューが必要');
assert.match(desktopJs, /navigationEntries = \['home'\]/, '画面履歴もホームから開始する');
assert.match(desktopJs, /switchTab\('home', \{ record: false, resetHistory: true \}\)/, '再起動時もホームを表示する');
assert.match(desktopCss, /\.home-mode-grid/, 'ホームのモード選択レイアウトが必要');
assert.match(desktopHtml, /id="desktop-version-button"/, 'PC・ソフト版のヘッダーにバージョンを表示する');
assert.match(desktopHtml, /id="version-overlay"/, 'PC・ソフト版にパッチノート画面が必要');
assert.match(desktopJs, /api\/version/, '共通のバージョンAPIを利用する');
assert.match(desktopHtml, /id="opp-section-diff"/, 'PC・ソフト版でも有利区間差枚を入力できる');
assert.match(desktopHtml, /id="opp-replay-used"/, 'PC・ソフト版でも再プレイ上限を計算できる');
assert.match(desktopHtml, /id="opp-quick-ocr"/, 'PC・ソフト版でも画像OCRを利用できる');
assert.match(desktopHtml, /id="opp-crawler-candidates"/, 'PC・ソフト版で期待値更新候補を確認できる');
assert.match(desktopHtml, /id="opp-store-ranking-list"/, 'PC・ソフト版でも候補店の自動ランキングを表示する');
assert.match(desktopHtml, /id="opp-store-ranking-prefecture"/, 'PC・ソフト版でも地域を切り替えられる');
assert.match(desktopHtml, /id="opp-occupancy-rotation"/, 'PC・ソフト版でも平均回転数を記録できる');
assert.match(desktopJs, /api\/occupancy\/hyena-stores/, 'PC・ソフト版でも店舗ランキングAPIを利用する');
assert.match(desktopJs, /data-opp-crawler-approve/, '期待値更新は利用者が承認してから反映する');
assert.match(desktopJs, /api\/opportunity\/crawler\/run/, '公開ソース確認APIを利用する');
assert.match(desktopHtml, /id="archive-collector"/, 'PC・ソフト版に過去データ収集管理が必要');
assert.match(desktopHtml, /id="archive-pause-btn"/, '収集を一時停止できる');
assert.match(desktopHtml, /id="archive-resume-btn"/, '収集を再開できる');
assert.match(desktopJs, /api\/scrape\/archive\/jobs/, '永続キューの収集APIを利用する');

for (const label of ['ハイエナ', '狙い台捜索']) {
  assert.match(desktopHtml, new RegExp(label), `PC・ソフト版に${label}が必要`);
  assert.match(mobileHtml, new RegExp(label), `iPhone版に${label}が必要`);
}

assert.match(desktopHtml, /id="opp-exchange-rate"[^>]*min="5"[^>]*step="0\.1"/);

console.log('web UI contract tests passed');
