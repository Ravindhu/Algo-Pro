// Browser QA for OpenAlgo: walk the public routes, then confirm the
// Socket.IO handshake from a real browser page. Reports console errors,
// failed requests and per-route render state.
export default async function run(page, ui) {
  const routes = ['/', '/login', '/faq', '/download', '/docs'];
  const results = [];

  for (const r of routes) {
    const errors = [];
    const onErr = (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 120)); };
    page.on('console', onErr);
    let status = null;
    try {
      const resp = await page.goto('http://127.0.0.1:5000' + r, {
        waitUntil: 'domcontentloaded', timeout: 30000,
      });
      status = resp ? resp.status() : null;
      await page.waitForTimeout(2500);
    } catch (e) {
      results.push({ route: r, error: String(e).slice(0, 120) });
      page.off('console', onErr);
      continue;
    }
    const info = await page.evaluate(() => ({
      title: document.title,
      bodyChars: document.body.innerText.replace(/\s+/g, ' ').trim().length,
      text: document.body.innerText.replace(/\s+/g, ' ').trim().slice(0, 90),
    }));
    page.off('console', onErr);
    results.push({ route: r, status, ...info, consoleErrors: errors });
  }

  // Socket.IO: the dashboard talks to the web app over socket.io, and market
  // ticks come from the separate WS proxy on 8765. Confirm the socket.io
  // client library loaded and can reach the server's polling endpoint.
  await page.goto('http://127.0.0.1:5000/login', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);
  const sock = await page.evaluate(async () => {
    const out = { hasIO: typeof window.io !== 'undefined', polling: null };
    try {
      const r = await fetch('/socket.io/?EIO=4&transport=polling', { credentials: 'include' });
      const t = await r.text();
      out.polling = { status: r.status, firstChars: t.slice(0, 60) };
    } catch (e) {
      out.polling = { error: String(e).slice(0, 120) };
    }
    return out;
  });

  return { routes: results, socketio: sock };
}