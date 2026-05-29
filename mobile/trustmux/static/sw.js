'use strict';

const CACHE = 'trustmux-v7';

// Only truly static assets are cached — icons and logo never change between
// releases and are safe to serve from cache indefinitely.
// index.html and app.js are intentionally excluded: they change with every
// release and must always be fetched fresh so updates are visible immediately
// without any cache-busting dance. The server is always local/Tailscale, so
// there is no latency cost to fetching them from the network.
const SHELL = ['/trustmux.svg', '/icons/icon-192.png?v=3', '/icons/icon-512.png?v=3'];

// These are always fetched from the network — never cache.
const NETWORK_ONLY = ['/ws', '/pair', '/ping', '/status', '/machines',
                      '/', '/app.js', '/manifest.json'];

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

  // Pass API endpoints, main HTML, and JS straight to the network.
  if (NETWORK_ONLY.some(p => url.pathname === p || url.pathname.startsWith(p))) return;

  // Cache-first only for the truly static assets listed in SHELL.
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
