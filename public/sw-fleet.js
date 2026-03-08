const VER   = 'axiom-fleet-v5';
const SHELL = [
  '/dashboard.html',
  '/manifest-fleet.json',
  '/fleet-favicon.svg',
  '/fleet-icon-192x192.png',
  '/fleet-icon-512x512.png',
  '/apple-touch-fleet.png',
];

// ── Install ──────────────────────────────────────────────────────────────────
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(VER)
      .then(c => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

// ── Activate — purge ALL old caches ──────────────────────────────────────────
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== VER).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// ── Background sync stub ──────────────────────────────────────────────────────
self.addEventListener('sync', e => {
  if (e.tag === 'axiom-fleet-sync') e.waitUntil(Promise.resolve());
});

// ── Push notifications ────────────────────────────────────────────────────────
self.addEventListener('push', e => {
  if (!e.data) return;
  let data = {};
  try { data = e.data.json(); } catch { data = { title: 'AXIOM Fleet', body: e.data.text() }; }
  e.waitUntil(
    self.registration.showNotification(data.title || 'AXIOM Fleet', {
      body:  data.body || '',
      icon:  '/fleet-icon-192x192.png',
      badge: '/fleet-icon-96x96.png',
      tag:   data.tag  || 'axiom-fleet-alert',
      data,
      actions: [
        { action: 'open',    title: 'Open Fleet' },
        { action: 'dismiss', title: 'Dismiss' },
      ],
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  if (e.action === 'dismiss') return;
  e.waitUntil(
    clients.matchAll({ type: 'window' }).then(ws => {
      const focused = ws.find(w => w.focused);
      if (focused) return focused.focus();
      if (ws.length) return ws[0].focus();
      return clients.openWindow('/dashboard.html');
    })
  );
});

// ── Fetch ─────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Never cache API calls
  if (url.pathname.startsWith('/api/')) return;

  // Cache-first for fleet icons & static assets
  if (/\.(png|ico|svg|woff2?)$/.test(url.pathname)) {
    e.respondWith(
      caches.open(VER).then(c =>
        c.match(e.request).then(hit => {
          if (hit) return hit;
          return fetch(e.request).then(r => {
            if (r.ok) c.put(e.request, r.clone());
            return r;
          });
        })
      )
    );
    return;
  }

  // Network-first for dashboard HTML
  if (url.pathname === '/dashboard.html' || e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request)
        .then(r => {
          const clone = r.clone();
          caches.open(VER).then(c => c.put(e.request, clone));
          return r;
        })
        .catch(() => caches.match('/dashboard.html'))
    );
    return;
  }

  // Stale-while-revalidate for everything else
  e.respondWith(
    caches.open(VER).then(cache =>
      cache.match(e.request).then(cached => {
        const fresh = fetch(e.request).then(r => {
          if (r.ok) cache.put(e.request, r.clone());
          return r;
        }).catch(() => cached);
        return cached || fresh;
      })
    )
  );
});
