// Service Worker — Cadastro Veicular (demo local)
// Estratégia:
//   - Same-origin (HTML, JS, /api, sw, manifest): NETWORK-FIRST
//     → sempre pega a versão nova quando online; cache só vira fallback offline.
//     (Antes era cache-first no HTML → navegador ficava preso em versão antiga.)
//   - CDN cross-origin (tailwind, lucide): cache-first (libs não mudam)
//   - POST/PATCH/DELETE: network-only (não cacheia mutações)

const CACHE_NAME = 'cadastro-veicular-v3';
const STATIC_ASSETS = [
  '/',
  '/manifest.json',
  'https://cdn.tailwindcss.com',
  'https://unpkg.com/lucide@latest/dist/umd/lucide.min.js',
];

self.addEventListener('install', (ev) => {
  ev.waitUntil(
    caches.open(CACHE_NAME).then((c) => c.addAll(STATIC_ASSETS).catch(() => {}))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (ev) => {
  ev.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (ev) => {
  const req = ev.request;
  const url = new URL(req.url);

  // Não cacheia mutações
  if (req.method !== 'GET') return;

  // Same-origin (HTML principal, /api, sw.js, manifest) → NETWORK-FIRST.
  // Online: sempre versão nova. Offline: cai pro cache.
  if (url.origin === self.location.origin) {
    ev.respondWith(
      fetch(req)
        .then((resp) => {
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then((c) => c.put(req, clone));
          }
          return resp;
        })
        .catch(() =>
          caches.match(req).then((cached) =>
            cached || new Response(
              url.pathname.startsWith('/api/')
                ? JSON.stringify({error: 'offline'})
                : 'offline',
              {status: 503, headers: {'Content-Type':
                url.pathname.startsWith('/api/') ? 'application/json' : 'text/plain'}}
            )
          )
        )
    );
    return;
  }

  // CDN cross-origin (tailwind, lucide) → cache-first (libs estáveis)
  ev.respondWith(
    caches.match(req).then((cached) =>
      cached || fetch(req).then((resp) => {
        if (resp.ok) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(req, clone));
        }
        return resp;
      }).catch(() => cached)
    )
  );
});
