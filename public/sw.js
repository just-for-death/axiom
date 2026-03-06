const VER   = 'axiom-v1';
const SHELL = ['/', '/index.html', '/manifest.json', '/favicon.svg', '/favicon.ico'];

self.addEventListener('install',   e => e.waitUntil(caches.open(VER).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener('activate',  e => e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k!==VER).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', e => {
  if (new URL(e.request.url).pathname.startsWith('/api/')) return; // always network for API
  e.respondWith(fetch(e.request).then(r => { caches.open(VER).then(c => c.put(e.request, r.clone())); return r; }).catch(() => caches.match(e.request).then(r => r || caches.match('/index.html'))));
});
