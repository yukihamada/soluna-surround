// SOLUNA Sound — minimal shell cache (音源/WS はネットワーク直)
const C = 'soluna-v1';
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
  e.respondWith(fetch(e.request).then(r => {
    const cp = r.clone(); caches.open(C).then(c => c.put(e.request, cp)); return r;
  }).catch(() => caches.match(e.request)));
});
