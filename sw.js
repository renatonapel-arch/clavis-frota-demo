// Service Worker — Cadastro Veicular (demo local)
// Estratégia:
//   - HTML/JS/CSS estáticos: cache-first com fallback de rede
//   - GET de API (listagens, detalhes): stale-while-revalidate
//   - POST/PATCH/DELETE: network-only (não cacheia mutações)
//   - Offline: IndexedDB sync queue (TODO em prod — aqui só cache simples)

const CACHE_NAME = 'cadastro-veicular-v2';
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

  // API GET → network-first (sempre fresco online; cache só como fallback offline)
  // Antes era stale-while-revalidate → UI ficava com dado velho após cadastrar.
  if (url.pathname.startsWith('/api/')) {
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
            cached || new Response(JSON.stringify({error: 'offline'}), {
              status: 503, headers: {'Content-Type': 'application/json'},
            })
          )
        )
    );
    return;
  }

  // estáticos → cache-first
  ev.respondWith(
    caches.match(req).then((cached) => cached || fetch(req).then((resp) => {
      if (resp.ok && url.origin === self.location.origin) {
        const clone = resp.clone();
        caches.open(CACHE_NAME).then((c) => c.put(req, clone));
      }
      return resp;
    }))
  );
});
