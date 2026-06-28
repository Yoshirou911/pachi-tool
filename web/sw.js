const CACHE_NAME = 'pachi-tool-v3';
const STATIC = ['/icons/icon-192.png', '/icons/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // HTML, JS, CSS: ネットワーク優先（常に最新を取得）
  if (url.pathname.endsWith('.html') || url.pathname.endsWith('.js') ||
      url.pathname.endsWith('.css') || url.pathname === '/') {
    e.respondWith(
      fetch(e.request).catch(() =>
        caches.match(e.request)
      )
    );
    return;
  }

  // API: ネットワークのみ
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(e.request).catch(() => new Response(JSON.stringify({error: 'offline'}), {
        status: 503,
        headers: {'Content-Type': 'application/json'},
      }))
    );
    return;
  }

  // アイコン等: キャッシュ優先
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
