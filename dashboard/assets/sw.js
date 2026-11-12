// Service worker for the Algo Pro dashboard (M2). Caches the page shell only.
// /api/* and any non-GET request always go to the network, never the cache.
const SHELL = "algo-pro-shell-v1";
const SHELL_URLS = ["/ui", "/assets/icon.svg", "/assets/flow_svg.js", "/assets/pwa.js", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_URLS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/")) return; // live data: network only
  if (!SHELL_URLS.includes(url.pathname)) return;
  event.respondWith(
    fetch(event.request)
      .then((res) => { caches.open(SHELL).then((c) => c.put(event.request, res.clone())); return res; })
      .catch(() => caches.match(event.request))
  );
});
