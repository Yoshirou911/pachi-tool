import assert from 'node:assert/strict';
import {renderAiEvidence} from '../mobile/ai-evidence.mjs';

const html = renderAiEvidence({contract_version: '1.0', target_date: '2026-09-14',
  scope: '<script>alert(1)</script>', generated_at: '2026-09-14T10:00Z', answer_status: 'fallback',
  claims: [{evidence_id: 'E001'}], evidence: [{id: 'E001', hall_name: '店舗A',
    metrics: [{label: '平均差枚', value: 123, unit: '枚/台日'}],
    period: {start: '2026-09-01', end: '2026-09-13'},
    sources: [{url: 'javascript:alert(1)', retrieved_at: null},
      {url: 'https://example.com/data', retrieved_at: '2026-09-13T23:00Z'}],
    missing_information: ['<img src=x onerror=alert(1)>']}],
});
assert.match(html, /根拠・出典を見る/);
assert.match(html, /固定説明へ復帰/);
assert.match(html, /123枚\/台日/);
assert.match(html, /取得 未記録/);
assert.match(html, /href="https:\/\/example.com\/data"/);
assert.match(html, /2026-09-13T23:00Z/);
assert.doesNotMatch(html, /<script>|<img|href="javascript:/);
assert.match(renderAiEvidence(null), /未取得/);
const guarded=renderAiEvidence({contract_version:'1.0',answer_status:'abstained',evidence:[],
  answer_guard:{excluded_count:2,reasons:['<script>店舗が違う</script>'],warnings:['出典未記録']}});
assert.match(guarded,/AIが判断を控えました/);
assert.match(guarded,/説明から除外 2件/);
assert.doesNotMatch(guarded,/<script>/);
assert.match(renderAiEvidence({contract_version: '1.0', evidence: []}), /対象に合う根拠がありません/);
console.log('AI evidence UI tests passed');
