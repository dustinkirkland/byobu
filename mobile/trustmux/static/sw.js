'use strict';

const CACHE = 'trustmux-v8';

// Only truly static assets are cached — icons, logo, and the bundled font
// never change between releases and are safe to serve from cache
// indefinitely.
// index.html and app.js are intentionally excluded: they change with every
// release and must always be fetched fresh so updates are visible immediately
// without any cache-busting dance. The server is always local/Tailscale, so
// there is no latency cost to fetching them from the network.
const SHELL = ['/trustmux.svg', '/icons/icon-192.png?v=3', '/icons/icon-512.png?v=3',
              '/fonts/DejaVuSansMono.woff2', '/fonts/DejaVuSansMono-Bold.woff2'];

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

  // Pass API endpoints, main HTML, and JS straight to the network.  Exact
  // matches only: with startsWith(), '/' matched every request and the
  // cache branch below was dead code -- harmless, but not what the comments
  // promised, and one edit away from caching authenticated responses.
  if (NETWORK_ONLY.includes(url.pathname)) return;

  // Cache-first only for the truly static assets listed in SHELL; anything
  // else goes to the network and is never stored.
  if (!SHELL.includes(url.pathname + url.search)) return;
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
