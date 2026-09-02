// SOLUNA Sound — minimal shell cache (音源/WS はネットワーク直)
const C = 'soluna-v2';
const SHELL = ['/', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/icon-512.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(C).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks =>
    Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.pathname.startsWith('/audio') ||
      u.pathname.startsWith('/api') || u.pathname.startsWith('/status')) return;
  // 音源/映像(/assets/…、CDN含む)は cache-first: 入場時に先読みしたものを開演時に再DLしない
  if (u.pathname.startsWith('/assets/')){
    e.respondWith(caches.match(e.request, {ignoreSearch:true}).then(hit => hit || fetch(e.request).then(r => {
      if (r.ok){ const cp = r.clone(); caches.open(C).then(c => c.put(e.request, cp)); } return r; })));
    return;
  }
  e.respondWith(fetch(e.request).then(r => {
    const cp = r.clone(); caches.open(C).then(c => c.put(e.request, cp)); return r;
  }).catch(() => caches.match(e.request)));
});
