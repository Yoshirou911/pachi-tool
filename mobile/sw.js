const VERSION = 'pachi-tool-mobile-v3.49.0';
const CORE_ASSETS = [
  './ai-review.mjs?v=3.49.0',
  './ai-evaluation.mjs?v=3.49.0',
  './ai-knowledge.mjs?v=3.49.0',
  './image-analysis.mjs?v=3.49.0',
  './ai-comparison.mjs?v=3.49.0',
  './hall-ai.mjs?v=3.49.0',
  './ai-evidence.mjs?v=3.49.0',
  './',
  './index.html',
  './app.css?v=3.49.0',
  './app.js?v=3.49.0',
  './core.mjs?v=3.49.0',
  './ocr.mjs?v=3.49.0',
  './catalog.json?v=3.49.0',
  './manifest.webmanifest',
  './prediction-verification.mjs?v=3.49.0',
  './placement-analysis.mjs?v=3.49.0',
  './model-review.mjs?v=3.49.0',
  './probability-validation.mjs?v=3.49.0',
  './candidate-evaluation.mjs?v=3.49.0',
  './prediction-monitor.mjs?v=3.49.0',
  './icons/icon-192.png',
  './icons/icon-512.png',
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(VERSION).then(cache => cache.addAll(CORE_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(key => key !== VERSION).map(key => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  // 保存状況・答え合わせ・予測APIを古いキャッシュで表示しない。
  if (new URL(event.request.url).pathname.startsWith('/api/')) {
    event.respondWith(fetch(event.request));
    return;
  }
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).then(response => {
      const copy = response.clone();
      caches.open(VERSION).then(cache => cache.put('./index.html', copy));
      return response;
    }).catch(() => caches.match('./index.html')));
    return;
  }
  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request).then(response => {
      if (response.ok && new URL(event.request.url).origin === self.location.origin) {
        const copy = response.clone();
        caches.open(VERSION).then(cache => cache.put(event.request, copy));
      }
      return response;
    }))
  );
});
