const VER   = 'axiom-v2';
const SHELL = ['/', '/index.html', '/manifest.json', '/favicon.svg', '/favicon.ico'];

// Install: pre-cache shell
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(VER)
      .then(c => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

// Activate: clean up old caches
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== VER).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Fetch: network-first for API, stale-while-revalidate for assets
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Always network for API calls — never cache
  if (url.pathname.startsWith('/api/')) return;

  // Network-first for navigation (HTML)
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request)
        .then(r => {
          const clone = r.clone();
          caches.open(VER).then(c => c.put(e.request, clone));
          return r;
        })
        .catch(() => caches.match('/index.html'))
    );
    return;
  }

  // Stale-while-revalidate for static assets
  e.respondWith(
    caches.open(VER).then(cache =>
      cache.match(e.request).then(cached => {
        const fetchPromise = fetch(e.request).then(r => {
          if (r.ok) cache.put(e.request, r.clone());
          return r;
        }).catch(() => cached);
        return cached || fetchPromise;
      })
    )
  );
});
