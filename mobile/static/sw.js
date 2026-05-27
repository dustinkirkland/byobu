'use strict';

const CACHE = 'trustmux-v5';

// Static shell — load instantly from cache on repeat visits.
const SHELL = ['/', '/app.js', '/trustmux.svg', '/manifest.json',
               '/icons/icon-192.png?v=3', '/icons/icon-512.png?v=3'];

// These are always fetched from the network — never cache.
const NETWORK_ONLY = ['/ws', '/pair', '/ping', '/status', '/machines'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Pass WebSocket upgrades and API endpoints straight through.
  if (NETWORK_ONLY.some(p => url.pathname.startsWith(p))) return;

  // Cache-first for everything else (shell assets).
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
