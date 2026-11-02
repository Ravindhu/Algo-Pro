/* POV Terminal front end. Read-only: GET requests only.
 * Server data is written with textContent or DOM calls, never innerHTML. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const view = $("view");
  const nav = $("nav");
  const banner = $("banner");
  const popup = $("popup");
  const popupTitle = $("popup-title");
  const popupStatus = $("popup-status");
  const popupBody = $("popup-body");
  const REFRESH_MS = 5000;
  let lastTrigger = null;
  let currentPopupKey = null;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  async function getJson(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
    return res.json();
  }

  function showBanner(msg) { banner.textContent = msg; banner.hidden = false; }
  function clearBanner() { banner.hidden = true; banner.textContent = ""; }

  const STATUS_LABEL = { ok: "LIVE", warn: "EMPTY", bad: "ERROR", idle: "NOT WIRED", external: "EXTERNAL" };
  function pill(status) {
    return el("span", "pill " + (status || "idle"), STATUS_LABEL[status] || String(status).toUpperCase());
  }

  // ---------------------------------------------------------------- blocks
  function kpiGrid(rows) {
    const grid = el("div", "grid");
    (rows || []).forEach((r) => {
      const card = el("div", "kpi");
      card.append(el("div", "label", r.label), el("div", "value", r.value));
      grid.append(card);
    });
    return grid;
  }

  function tableBlock(t) {
    const frag = document.createDocumentFragment();
    const more = t.total > t.rows.length ? ` (showing ${t.rows.length} of ${t.total})` : "";
    frag.append(el("h3", null, t.title + more));
    const wrap = el("div", "table-wrap");
    const table = el("table");
    const thead = el("thead");
    const headRow = el("tr");
    t.columns.forEach((c) => headRow.append(el("th", null, c)));
    thead.append(headRow);
    const tbody = el("tbody");
    t.rows.forEach((r) => {
      const tr = el("tr");
      r.forEach((c) => tr.append(el("td", null, c)));
      tbody.append(tr);
    });
    table.append(thead, tbody);
    wrap.append(table);
    frag.append(wrap);
    return frag;
  }

  function payloadBlocks(payload) {
    const frag = document.createDocumentFragment();
    if (payload.error) frag.append(el("p", "error-line", payload.error));
    if ((payload.summary || []).length) frag.append(kpiGrid(payload.summary));
    (payload.tables || []).forEach((t) => frag.append(tableBlock(t)));
    if ((payload.notes || []).length) {
      const ul = el("ul", "notes");
      payload.notes.forEach((n) => ul.append(el("li", null, n)));
      frag.append(ul);
    }
    if (!(payload.summary || []).length && !(payload.tables || []).length && !payload.error) {
      frag.append(el("p", "muted", "No values published yet."));
    }
    return frag;
  }

  function card(title, subtitle) {
    const section = el("section", "card");
    const head = el("h2", null, title);
    if (subtitle) head.append(el("span", "sub", subtitle));
    section.append(head);
    return section;
  }

  function summaryValue(payload, label) {
    const row = ((payload && payload.summary) || []).find((r) => r.label === label);
    return row ? row.value : null;
  }

  // ---------------------------------------------------------- header popups
  function renderPopup(payload) {
    popupTitle.textContent = payload.title;
    popupBody.replaceChildren(payloadBlocks(payload));
    const text = { ok: "Live snapshot", empty: "No data available yet", error: "This section failed to load" };
    popupStatus.className = "status" + (payload.status === "error" ? " error" : "");
    popupStatus.textContent = `${text[payload.status] || payload.status} · updated ${new Date().toLocaleTimeString()}`;
  }

  async function openSection(key, trigger) {
    currentPopupKey = key;
    lastTrigger = trigger || lastTrigger;
    popupBody.replaceChildren(el("p", "muted", "Loading…"));
    popupStatus.textContent = "";
    if (!popup.open) popup.showModal();
    try {
      renderPopup(await getJson(`/api/sections/${encodeURIComponent(key)}`));
    } catch (err) {
      popupStatus.className = "status error";
      popupStatus.textContent = String(err.message || err);
      popupBody.replaceChildren();
    }
  }

  function closePopup() {
    popup.close();
    if (lastTrigger) lastTrigger.focus();
  }

  async function buildNav() {
    try {
      const data = await getJson("/api/sections");
      nav.replaceChildren();
      data.sections.forEach((s) => {
        const btn = el("button", null, s.title);
        btn.type = "button";
        btn.setAttribute("aria-haspopup", "dialog");
        btn.addEventListener("click", () => openSection(s.key, btn));
        nav.append(btn);
      });
    } catch (err) {
      nav.replaceChildren();
      showBanner(`Could not load sections: ${err.message || err}`);
    }
  }

  // ------------------------------------------------------------ main view
  function engineCard(e) {
    const card = el("div", "engine-card " + (e.status || "idle"));
    card.tabIndex = 0;
    card.setAttribute("role", "link");
    card.setAttribute("aria-label", `${e.title}: ${e.status}`);
    const top = el("div", "engine-top");
    top.append(el("b", null, e.title), pill(e.status));
    card.append(top, el("small", "group", e.group), el("p", "muted", e.blurb));
    const go = () => { location.hash = "#/engine/" + e.key; };
    card.addEventListener("click", go);
    card.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); } });
    return card;
  }

  function eventLog(flowData, payloads) {
    const ul = el("ul", "log");
    const add = (cls, text) => ul.append(el("li", cls, text));
    flowData.engines.forEach((e) => {
      if (e.status === "bad") add("bad", `ERROR  ${e.title}: its page raised an error`);
      if (e.status === "warn") add("warn", `EMPTY  ${e.title}: nothing published yet`);
    });
    const idle = flowData.engines.filter((e) => e.status === "idle").map((e) => e.title);
    if (idle.length) add("idle", `NOT WIRED  ${idle.join(", ")}`);
    payloads.forEach((p) => {
      if (p.error) add("bad", `ERROR  ${p.title}: ${p.error}`);
      (p.notes || []).forEach((n) => add("note", `NOTE  ${p.title}: ${n}`));
    });
    if (!ul.children.length) add("ok", "OK  no warnings in the latest snapshot");
    return ul;
  }

  function kvList(payload, limit) {
    const wrap = el("div", "kv");
    ((payload && payload.summary) || []).slice(0, limit || 12).forEach((r) => {
      const row = el("div", "kv-row");
      row.append(el("span", null, r.label), el("b", null, r.value));
      wrap.append(row);
    });
    if (!wrap.children.length) wrap.append(el("p", "muted", "No values published yet."));
    return wrap;
  }

  async function renderMain() {
    const [flowData, terminal, risk, portfolio, engines] = await Promise.all([
      getJson("/api/flow"),
      getJson("/api/sections/terminal"),
      getJson("/api/sections/risk"),
      getJson("/api/sections/portfolio"),
      getJson("/api/engines"),
    ]);
    const mode = summaryValue(terminal, "Mode");
    $("chip-mode").textContent = "mode: " + (mode || "unknown");

    const frag = document.createDocumentFragment();
    const head = el("div", "page-head");
    const c = flowData.counts;
    head.append(
      el("h1", null, "Trading system · full flow"),
      el("p", "muted", "Market data and macro feed the frontline engines, then alpha models, " +
        "execution, the risk gate, the OpenAlgo bridge, and broker and exchange. Click an engine to open its own shell."),
      el("div", "counts", `${c.ok} live · ${c.warn} empty · ${c.bad} error · ${c.idle} not wired`));
    frag.append(head);

    const shell = card("Neural shell · full trading flow", "click any engine node");
    const box = el("div", "shell-box");
    shell.append(box, Flow.legend());
    frag.append(shell);
    Flow.draw(box, flowData, {
      label: "Full trading flow neural shell",
      onNode: (n) => { location.hash = "#/engine/" + n.engine; },
    });

    const engineSec = card("Engines", "each has its own neural shell");
    const grid = el("div", "engine-grid");
    engines.engines.forEach((e) => grid.append(engineCard(e)));
    engineSec.append(grid);
    frag.append(engineSec);

    const details = el("div", "split");

    const kpi = card("Important details", "terminal snapshot");
    kpi.append(kpiGrid(terminal.summary));
    details.append(kpi);

    const riskSec = card("Risk & guards", "risk section");
    riskSec.append(kvList(risk, 14));
    details.append(riskSec);

    const port = card("Portfolio", "portfolio section");
    port.append(kvList(portfolio, 8));
    details.append(port);

    const log = card("Event log", "latest snapshot");
    log.append(eventLog(flowData, [terminal, risk, portfolio]));
    details.append(log);

    frag.append(details);

    const tape = card("Execution tape", "terminal alerts and trades");
    const tapeTables = (terminal.tables || []);
    if (tapeTables.length) tapeTables.forEach((t) => tape.append(tableBlock(t)));
    else tape.append(el("p", "muted", "No executions or alerts recorded yet."));
    frag.append(tape);

    view.replaceChildren(frag);
  }

  // ---------------------------------------------------------- engine view
  async function renderEngine(key) {
    const [data, list] = await Promise.all([
      getJson(`/api/engines/${encodeURIComponent(key)}`),
      getJson("/api/engines"),
    ]);
    const frag = document.createDocumentFragment();

    const head = el("div", "page-head");
    const back = el("a", "back", "← main dashboard");
    back.href = "#/";
    const title = el("h1", null, data.title);
    head.append(back, title, pill(data.status), el("p", "muted", data.blurb));
    frag.append(head);

    const shell = card(`${data.title} · engine shell`, data.group);
    const box = el("div", "shell-box");
    shell.append(box, Flow.legend());
    frag.append(shell);
    Flow.draw(box, data, { compact: true, label: `${data.title} neural shell` });

    const detail = card("Engine details", data.section ? `from the ${data.section} page` : "no page wired");
    if (data.payload) detail.append(payloadBlocks(data.payload));
    else detail.append(el("p", "muted", "No page publishes this engine yet. Its node shows as not wired until one does."));
    frag.append(detail);

    const others = card("Other engines");
    const chips = el("div", "chip-row");
    list.engines.forEach((e) => {
      const a = el("a", "chip-link " + e.status, e.title);
      a.href = "#/engine/" + e.key;
      if (e.key === key) a.setAttribute("aria-current", "page");
      chips.append(a);
    });
    others.append(chips);
    frag.append(others);

    view.replaceChildren(frag);
    $("chip-mode").textContent = "engine: " + data.title;
  }

  // ------------------------------------------------------------- routing
  function currentRoute() {
    const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
    const m = h.match(/^engine\/([a-z_]+)$/);
    return m ? { name: "engine", key: m[1] } : { name: "main" };
  }

  async function render() {
    try {
      const r = currentRoute();
      if (r.name === "engine") await renderEngine(r.key);
      else await renderMain();
      clearBanner();
    } catch (err) {
      showBanner(`Could not load this view: ${err.message || err}`);
    }
  }

  // ------------------------------------------------------------- wiring
  $("popup-refresh").addEventListener("click", () => currentPopupKey && openSection(currentPopupKey));
  $("popup-close").addEventListener("click", closePopup);
  popup.addEventListener("click", (e) => { if (e.target === popup) closePopup(); });
  popup.addEventListener("close", () => { if (lastTrigger) lastTrigger.focus(); });
  window.addEventListener("hashchange", render);

  buildNav();
  render();
  setInterval(render, REFRESH_MS);
})();
