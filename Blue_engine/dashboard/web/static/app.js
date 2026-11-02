/* POV Terminal front end. Read-only: it only calls GET endpoints.
 * Server data is always written with textContent, never innerHTML. */
(function () {
  "use strict";

  const nav = document.getElementById("nav");
  const banner = document.getElementById("banner");
  const popup = document.getElementById("popup");
  const popupTitle = document.getElementById("popup-title");
  const popupStatus = document.getElementById("popup-status");
  const popupBody = document.getElementById("popup-body");
  const refreshBtn = document.getElementById("popup-refresh");
  let currentKey = null;
  let lastTrigger = null;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function showBanner(msg) {
    banner.textContent = msg;
    banner.hidden = false;
  }

  async function getJson(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
    return res.json();
  }

  function renderSummary(payload) {
    const grid = el("div", "grid");
    (payload.summary || []).forEach((row) => {
      const card = el("div", "kpi");
      card.append(el("div", "label", row.label), el("div", "value", row.value));
      grid.append(card);
    });
    popupBody.append(grid);
  }

  function renderTables(payload) {
    (payload.tables || []).forEach((t) => {
      popupBody.append(el("h3", null, `${t.title}${t.total > t.rows.length ? ` (showing ${t.rows.length} of ${t.total})` : ""}`));
      const wrap = el("div", "table-wrap");
      const table = el("table");
      const thead = el("thead");
      const headRow = el("tr");
      t.columns.forEach((c) => headRow.append(el("th", null, c)));
      thead.append(headRow);
      const tbody = el("tbody");
      t.rows.forEach((r) => {
        const tr = el("tr");
        r.forEach((cell) => tr.append(el("td", null, cell)));
        tbody.append(tr);
      });
      table.append(thead, tbody);
      wrap.append(table);
      popupBody.append(wrap);
    });
  }

  function renderPayload(payload) {
    popupTitle.textContent = payload.title;
    popupBody.replaceChildren();
    const statusText = {
      ok: "Live snapshot",
      empty: "No data available yet",
      error: "This section failed to load",
    }[payload.status] || payload.status;
    popupStatus.className = "status" + (payload.status === "error" ? " error" : "");
    popupStatus.textContent = `${statusText} · updated ${new Date().toLocaleTimeString()}`;
    if (payload.error) popupBody.append(el("p", "status error", payload.error));
    renderSummary(payload);
    renderTables(payload);
    if ((payload.notes || []).length) {
      const ul = el("ul", "notes");
      payload.notes.forEach((n) => ul.append(el("li", null, n)));
      popupBody.append(ul);
    }
  }

  async function openSection(key, trigger) {
    currentKey = key;
    lastTrigger = trigger || lastTrigger;
    popupBody.replaceChildren(el("p", "muted", "Loading…"));
    popupStatus.textContent = "";
    if (!popup.open) popup.showModal();
    try {
      renderPayload(await getJson(`/api/sections/${encodeURIComponent(key)}`));
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

  refreshBtn.addEventListener("click", () => currentKey && openSection(currentKey));
  document.getElementById("popup-close").addEventListener("click", closePopup);
  popup.addEventListener("click", (e) => { if (e.target === popup) closePopup(); });
  popup.addEventListener("close", () => { if (lastTrigger) lastTrigger.focus(); });

  buildNav();
})();
