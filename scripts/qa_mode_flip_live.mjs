// Prove the mode banner now flips WITHOUT a page reload.
//
// Safety: this never touches OpenAlgo's real mode. It stubs the browser's
// view of /pov/mode via page.route(), which intercepts the request before it
// reaches the server. The real analyzer state stays exactly as the operator
// left it -- flipping live mode just to test a banner would be reckless.
//
// Sequence:
//   1. load page, observe initial banner (expect ANALYZER)
//   2. stub /pov/mode -> live, and WAIT for the page to notice on its own
//   3. stub back -> analyzer, wait again
//   4. report the latency of each transition
export default async function run(page, ui) {
  const snap = () => page.evaluate(() => ({
    t: Date.now(),
    banner: document.querySelector('.mode-banner > span')?.textContent || null,
    dataMode: document.body.getAttribute('data-mode'),
  }));

  const initial = await snap();

  // --- install a fake /pov/mode ------------------------------------------
  let served = { mode: 'live', label: 'LIVE — REAL MONEY' };
  await page.route('**/pov/mode', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(served),
    });
  });

  // Poll until the page reflects the stubbed mode, or time out.
  async function waitForMode(target, timeoutMs = 8000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const s = await snap();
      if (s.dataMode === target) return { changed: true, latencyMs: Date.now() - start, ...s };
      await page.waitForTimeout(100);
    }
    return { changed: false, latencyMs: null, ...(await snap()) };
  }

  // --- flip to LIVE (stubbed) --------------------------------------------
  served = { mode: 'live', label: 'LIVE — REAL MONEY' };
  const toLive = await waitForMode('live');

  // --- flip back to ANALYZER (stubbed) -----------------------------------
  served = { mode: 'analyzer', label: 'ANALYZER (SIMULATED)' };
  const toAnalyzer = await waitForMode('analyzer');

  return {
    initial,
    flipToLiveWithoutReload: toLive,
    flipBackToAnalyzerWithoutReload: toAnalyzer,
    verdict:
      toLive.changed && toAnalyzer.changed
        ? 'PASS: banner self-updates both ways, no reload needed'
        : 'FAIL: banner did not self-update',
  };
}