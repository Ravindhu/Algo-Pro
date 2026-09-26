// Capture the exact DBIE gateway requests so a provider can replay them.
//
// data.rbi.org.in is an Angular SPA that calls a CIMS gateway. My hand-built
// request returns errorCode 4302 ("Internal Server Error") even though the
// session token succeeds -- so the request *shape* is wrong somewhere. Rather
// than guess at the envelope, watch what the app actually sends.
export default async function run(page, ui) {
  const calls = [];

  page.on('request', (req) => {
    const u = req.url();
    if (!u.includes('CIMS_Gateway')) return;
    let body = null;
    try { body = req.postData(); } catch { /* not text */ }
    calls.push({
      method: req.method(),
      url: u,
      body: body ? body.slice(0, 500) : null,
      headers: Object.fromEntries(
        Object.entries(req.headers()).filter(([k]) =>
          ['authorization', 'channelkey', 'datatype', 'content-type'].includes(k))
      ),
    });
  });

  const responses = [];
  page.on('response', async (res) => {
    const u = res.url();
    if (!u.includes('CIMS_Gateway')) return;
    let txt = '';
    try { txt = (await res.text()).slice(0, 400); } catch { /* ignore */ }
    responses.push({ status: res.status(), url: u, body: txt });
  });

  // The homepage's "Economy Watch" / quick-search widgets are what trigger
  // the data calls, so interact a little rather than just landing.
  await page.waitForTimeout(4000);

  const links = await page.evaluate(() =>
    [...document.querySelectorAll('a,button')]
      .map(e => (e.innerText || '').trim())
      .filter(t => t && t.length < 30)
      .slice(0, 25)
  );

  // Click a likely data widget: "Economy Watch" or a quick-search term.
  for (const label of ['CPI', 'Inflation', 'Foreign Exchange', 'Reserves']) {
    const hit = await page.evaluate((lbl) => {
      const els = [...document.querySelectorAll('a,button,span,div')];
      const el = els.find(e => (e.innerText || '').trim().toLowerCase() === lbl.toLowerCase());
      if (el) { el.click(); return true; }
      return false;
    }, label);
    if (hit) {
      await page.waitForTimeout(4000);
      break;
    }
  }

  await page.waitForTimeout(3000);

  return {
    gatewayCalls: calls,
    gatewayResponses: responses,
    pageLinksSample: links,
  };
}