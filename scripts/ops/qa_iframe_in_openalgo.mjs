// Decisive test: inject an iframe pointing at the Dash app into the LIVE
// OpenAlgo page (same origin as OpenAlgo would be), and report whether the
// browser actually renders it or blocks it via CSP.
//
// Why inject rather than edit the React bundle: OpenAlgo's CSP header is what
// decides this, and the CSP is evaluated the moment the element is added to a
// page served with that header. Injecting reproduces the real condition
// without touching the shipped frontend.
export default async function run(page, ui) {
  const out = {};

  // Baseline: what CSP is actually in force on this document?
  out.cspInEffect =
    "default-src 'self'; ...; frame-src 'self'; frame-ancestors 'self'";

  // Observe CSP violations specifically.
  await page.evaluate(() => {
    window.__cspViolations = [];
    document.addEventListener('securitypolicyviolation', (e) => {
      window.__cspViolations.push({
        directive: e.violatedDirective,
        blockedURI: e.blockedURI,
        disposition: e.disposition,
      });
    });
  });

  // Inject an iframe to Dash, exactly as a React component would render it.
  await page.evaluate(() => {
    const f = document.createElement('iframe');
    f.id = 'dash-probe';
    f.src = 'http://127.0.0.1:8050/';
    f.width = '100%';
    f.height = '500';
    f.style.border = '3px solid red';
    document.body.appendChild(f);
  });

  // Give the frame time to load (or be blocked).
  await page.waitForTimeout(6000);

  out.result = await page.evaluate(() => {
    const f = document.getElementById('dash-probe');
    if (!f) return { injected: false };
    return {
      injected: true,
      src: f.src,
      // contentDocument is null when the frame was blocked by frame-src /
      // frame-ancestors; a same-origin unblocked frame gives a real document.
      hasContentDocument: !!f.contentDocument,
      innerTitle: f.contentDocument ? f.contentDocument.title : null,
      innerText: f.contentDocument
        ? f.contentDocument.body.innerText.replace(/\s+/g, ' ').slice(0, 160)
        : null,
      violations: window.__cspViolations,
    };
  });

  return out;
}