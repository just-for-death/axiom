const VER   = 'axiom-v3';
const SHELL = ['/', '/index.html', '/manifest.json', '/favicon.svg', '/favicon.ico',
               '/icon-192x192.png', '/icon-512x512.png'];

// ── Install: pre-cache shell ─────────────────────────────────────────────────
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(VER)
      .then(c => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

// ── Activate: clean old caches ───────────────────────────────────────────────
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== VER).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// ── Background sync stub (reconnect after going offline) ─────────────────────
self.addEventListener('sync', e => {
  if (e.tag === 'axiom-reconnect') {
    e.waitUntil(Promise.resolve());
  }
});

// ── Push notifications ────────────────────────────────────────────────────────
self.addEventListener('push', e => {
  if (!e.data) return;
  let data = {};
  try { data = e.data.json(); } catch { data = { title: 'AXIOM Alert', body: e.data.text() }; }
  e.waitUntil(
    self.registration.showNotification(data.title || 'AXIOM', {
      body:    data.body  || '',
      icon:    '/icon-192x192.png',
      badge:   '/icon-96x96.png',
      tag:     data.tag   || 'axiom-alert',
      data,
      actions: [
        { action: 'open',    title: 'Open Dashboard' },
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
      return clients.openWindow('/');
    })
  );
});

// ── Fetch: tiered caching strategy ───────────────────────────────────────────
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Never cache — API, EventSource streams, WebSocket upgrades
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws')) return;

  // Cache-first for icons & fonts (rarely change)
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

  // Network-first for HTML navigation (always fresh)
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

  // Stale-while-revalidate for JS/CSS chunks
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
