/* Service worker for the DEV TSC app.
 *
 * Served from `/sw.js` (not `/static/`) so its scope is the whole app — a
 * worker only controls pages at or below its own URL.
 *
 * It deliberately caches the shell and *nothing else*. Every tab renders
 * figures read from Odoo, and the top bar says when they were last refreshed;
 * serving a cached tab offline would show those numbers with a timestamp that
 * is no longer true. So: assets from the cache, pages from the network, and an
 * honest "hors ligne" page when the Mac or the tailnet is unreachable.
 *
 * Bump CACHE when the shell assets change — the old cache is dropped on
 * activate.
 */
const CACHE = "dev-tsc-shell-v2";

// Live data: always the network, never the cache.
const LIVE = ["/status", "/resync", "/refresh", "/lines", "/repartition"];

const OFFLINE_PAGE = `<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DEV TSC — hors ligne</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; text-align: center; padding: 2rem;
         font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #ffffff; color: #1c1e21; }
  @media (prefers-color-scheme: dark) { body { background: #16181d; color: #e6e8eb; } }
  .mark { width: 72px; height: 72px; border-radius: 16px; background: #1a56db;
          color: #fff; font-weight: 700; font-size: 44px; line-height: 72px;
          margin: 0 auto 1.5rem; }
  h1 { font-size: 1.25rem; margin: 0 0 .5rem; }
  p { margin: 0 0 1.5rem; color: #6b7280; max-width: 32ch; }
  button { font: inherit; font-weight: 600; color: #fff; background: #1a56db;
           border: 0; border-radius: 8px; padding: .6rem 1.2rem; cursor: pointer; }
</style></head>
<body><div>
  <div class="mark">D</div>
  <h1>Hors ligne</h1>
  <p>Le Mac qui héberge l'application n'est pas joignable. Vérifie que
     Tailscale est actif sur cet appareil.</p>
  <button onclick="location.reload()">Réessayer</button>
</div></body></html>`;

// Nothing is precached: every asset is requested with the `?v=<mtime>` buster,
// so the URL to precache is not knowable here. The fetch handler fills the
// cache as the shell loads instead.
self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (LIVE.some((path) => url.pathname === path)) return;

  // Pages: network only, with the offline card as the fallback. htmx tab swaps
  // (HX-Request) fall through to the network untouched so the app surfaces
  // its own error rather than swapping this page into the pane.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => new Response(OFFLINE_PAGE, {
        status: 503,
        headers: { "Content-Type": "text/html; charset=utf-8" },
      }))
    );
    return;
  }

  // Shell assets: cache first, on an exact match. An edited file is served
  // under a new `?v=`, which misses here and goes to the network — so the
  // cache never hands back a stale stylesheet. The stored copies of the
  // previous version are dropped as the new one lands.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then(async (cache) => {
            for (const old of await cache.keys()) {
              if (new URL(old.url).pathname === url.pathname) await cache.delete(old);
            }
            await cache.put(request, copy);
          });
        }
        return response;
      }))
    );
  }
});
