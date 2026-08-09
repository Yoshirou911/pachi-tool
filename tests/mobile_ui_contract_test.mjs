import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../mobile/index.html', import.meta.url), 'utf8');
const css = readFileSync(new URL('../mobile/app.css', import.meta.url), 'utf8');
const app = readFileSync(new URL('../mobile/app.js', import.meta.url), 'utf8');

for (const id of ['screen-home', 'screen-check', 'screen-guide', 'screen-planner', 'screen-trend', 'screen-target-map', 'screen-floor-map', 'screen-strategy', 'screen-results', 'screen-settings']) {
  assert.match(html, new RegExp(`id="${id}"`), `${id} が必要`);
}

assert.match(html, /id="screen-home" class="screen active"/, 'ホーム画面を最初に表示する');
assert.equal((html.match(/class="nav-button/g) || []).length, 10, 'モード別に切り替える専用メニューを持つ');
assert.match(html, /data-screen-target="check"/, 'ホームからハイエナ判定へ移動できる');
assert.match(html, /data-screen-target="planner"/, 'ホームから狙い台捜索へ移動できる');
assert.doesNotMatch(html, /data-screen="candidates"/, '候補台は狙い目画面へ統合する');
assert.match(html, /data-screen="planner"/, '明日の狙い台を独立メニューにする');
assert.match(html, /id="planner-form"/, '狙い台を登録できる');
assert.match(html, /id="planner-list"/, '狙い台を独立して一覧表示する');
assert.match(html, /id="target-search-form"/, '蓄積データから狙い台を検索できる');
assert.match(html, /id="target-search-results"/, '分析ランキングを表示できる');
assert.match(html, /id="target-map-form"/, '指定日で店舗ヒートマップを更新できる');
assert.match(html, /id="target-heat-map"/, '地図上に店舗の熱量を表示できる');
assert.match(html, /id="target-map-long-days"/, '長期表示期間を選べる');
assert.match(html, /id="trend-form"/, '店舗ごとの傾向分析を持つ');
assert.match(html, /id="floor-form"/, '店内座席ヒートマップを持つ');
assert.match(html, /id="floor-editor-form"/, '出典付きレイアウト編集を持つ');
assert.match(html, /id="screen-strategy"/, '分析から保存した朝一作戦を独立表示する');
assert.match(html, /data-module-nav="hyena"/, 'ハイエナ専用メニューが必要');
assert.match(html, /data-module-nav="target"/, '狙い台専用メニューが必要');
assert.match(html, /data-screen-target="guide"/, '判定画面から狙い目へ移動できる');
assert.match(html, /id="performance-chart"/, '期待値と実収支の比較グラフを表示する');
assert.match(html, /id="performance-summary"/, '比較指標を表示する');
assert.match(html, /id="catalog-scope"/, '現在の対象機種を明示する');
assert.match(html, /スマスロ攻略ホーム/, 'スマスロ専門ツールであることを明示する');
assert.match(html, /id="mobile-version-button"/, 'ヘッダーから更新内容を開ける');
assert.match(html, /id="patch-notes-group"/, '設定画面にパッチノートを表示する');
assert.match(html, /app\.css\?v=1\.9\.4/);
assert.match(html, /app\.js\?v=1\.9\.4/);
assert.match(html, /id="brand-home"[^>]*data-screen-target="home"/, '左上ブランドからホームへ戻れる');
assert.match(html, /id="mobile-menu-button"/, '全画面共通のメニューボタンが必要');
assert.match(html, /id="mobile-menu-overlay"/, '開閉できる全機能メニューが必要');
for (const target of ['home', 'check', 'guide', 'planner', 'trend', 'target-map', 'floor-map', 'strategy', 'results', 'settings']) {
  assert.match(html, new RegExp(`data-menu-screen[^>]*data-screen-target="${target}"`), `共通メニューに ${target} が必要`);
}
assert.match(app, /hostname === 'yoshirou911\.github\.io'/, '公開PWAではAPI接続先を切り替える');
assert.match(app, /https:\/\/pachi-tool\.fly\.dev/, '公開PWAの分析API接続先が必要');
assert.match(css, /min-height:\s*calc\(68px \+ var\(--safe-bottom\)\)/, '下部メニューのタップ領域を確保する');

console.log('mobile UI contract tests passed');
