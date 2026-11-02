/* flow.js - draws every neural shell as SVG from the /api/flow and
 * /api/engines/<key> payloads. Built with createElementNS and textContent
 * only: no innerHTML, so server data is never parsed as markup. */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const COLOR = { ok: "#2ee6a6", warn: "#ffb347", bad: "#ff4d6d", idle: "#3a4a5a", external: "#7fa3bf" };
  const TEXT = { ok: "live", warn: "empty", bad: "error", idle: "not wired", external: "external" };
  const GROUP_COLOR = { frontline: "#3fd0ff", alpha: "#b18cff", execution: "#ffb347", risk: "#ff7ad9" };
  let uid = 0;

  function svg(tag, attrs, parent) {
    const node = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach((k) => {
      const v = attrs[k];
      if (v !== null && v !== undefined && v !== false) node.setAttribute(k, String(v));
    });
    if (parent) parent.appendChild(node);
    return node;
  }

  function place(graph, W, H, padX, padY, labelH) {
    const cols = Math.max(1, graph.cols || 1, ...graph.nodes.map((n) => n.col + 1));
    const pos = {};
    graph.nodes.forEach((n) => {
      const x = cols === 1 ? W / 2 : padX + (n.col * (W - 2 * padX)) / (cols - 1);
      const y = padY + n.row * (H - 2 * padY - labelH);
      pos[n.id] = { x: x, y: y };
    });
    return pos;
  }

  function curve(a, b) {
    const mx = (a.x + b.x) / 2;
    return `M${a.x},${a.y} C${mx},${a.y} ${mx},${b.y} ${b.x},${b.y}`;
  }

  function radius(n) {
    if (n.kind === "out") return 13;
    if (n.kind === "store") return 12;
    if (n.kind === "src" || n.kind === "external") return 10;
    return 14;
  }

  /** Draw one graph into `container`. opts: compact, label, onNode. */
  function draw(container, graph, opts) {
    opts = opts || {};
    const W = 1200, H = opts.compact ? 330 : 470, padX = 105, padY = 60, labelH = 52;
    const id = "fl" + (++uid);
    const pos = place(graph, W, H, padX, padY, labelH);
    container.replaceChildren();

    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": opts.label || "Neural shell", class: "neural" }, container);
    const defs = svg("defs", {}, root);
    const glow = svg("filter", { id: id + "-glow", x: "-60%", y: "-60%", width: "220%", height: "220%" }, defs);
    svg("feGaussianBlur", { stdDeviation: "3", result: "b" }, glow);
    const merge = svg("feMerge", {}, glow);
    svg("feMergeNode", { in: "b" }, merge);
    svg("feMergeNode", { in: "SourceGraphic" }, merge);
    svg("rect", { x: 0, y: 0, width: W, height: H, rx: 18, class: "bg" }, root);

    const edgeLayer = svg("g", {}, root);
    const pulseLayer = svg("g", {}, root);
    const nodeLayer = svg("g", {}, root);

    graph.edges.forEach((e, i) => {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) return;
      const pid = `${id}-p${i}`;
      const feedback = e.kind === "feedback";
      svg("path", {
        id: pid, d: curve(a, b), fill: "none",
        stroke: feedback ? "#ffb347" : (e.active ? "#38bdf8" : "#2a3b52"),
        "stroke-width": e.active ? 2 : 1.2,
        "stroke-dasharray": feedback ? "6 5" : null,
        opacity: e.active ? 0.9 : 0.5,
        class: e.active ? "edge live" : "edge",
      }, edgeLayer);
      if (e.active) {
        const dot = svg("circle", { r: 4, fill: "#fde68a", filter: `url(#${id}-glow)` }, pulseLayer);
        const dur = (3 + (i % 4) * 0.8).toFixed(1) + "s";
        const motion = svg("animateMotion", { dur: dur, repeatCount: "indefinite" }, dot);
        svg("mpath", { href: "#" + pid }, motion);
      }
    });

    graph.nodes.forEach((n) => {
      const p = pos[n.id];
      if (!p) return;
      const status = n.status || "idle";
      const color = COLOR[status] || COLOR.idle;
      const r = radius(n);
      const clickable = !!(opts.onNode && n.engine && n.kind !== "external");
      const g = svg("g", {
        class: "node" + (clickable ? " clickable" : ""),
        "data-id": n.id,
        tabindex: clickable ? 0 : null,
        role: clickable ? "link" : "img",
        "aria-label": `${n.label}: ${TEXT[status] || status}`,
      }, nodeLayer);
      const title = svg("title", {}, g);
      title.textContent = `${n.label} — ${TEXT[status] || status}`;
      if (n.kind === "store") {
        svg("circle", { cx: p.x, cy: p.y, r: r + 5, fill: "none", stroke: color, "stroke-width": 1, opacity: 0.6 }, g);
      }
      svg("circle", {
        cx: p.x, cy: p.y, r: r, fill: "#0b1220", stroke: color, "stroke-width": 2.2,
        "stroke-dasharray": (n.kind === "src" || n.kind === "external") ? "4 3" : null,
        filter: status === "ok" ? `url(#${id}-glow)` : null,
      }, g);
      svg("circle", { cx: p.x, cy: p.y, r: 4, fill: color }, g);
      const label = svg("text", { x: p.x, y: p.y + r + 17, "text-anchor": "middle", class: "node-label" }, g);
      label.textContent = n.label;
      if (clickable) {
        g.addEventListener("click", () => opts.onNode(n));
        g.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); opts.onNode(n); }
        });
      }
    });
    return root;
  }

  /** Colour key shown under every shell. Built with DOM calls only. */
  function legend() {
    const wrap = document.createElement("div");
    wrap.className = "legend";
    [["ok", "Live"], ["warn", "Empty"], ["bad", "Error"], ["idle", "Not wired"], ["external", "External"]]
      .forEach(([key, text]) => {
        const item = document.createElement("span");
        item.className = "legend-item";
        const dot = document.createElement("i");
        dot.style.background = COLOR[key];
        item.append(dot, document.createTextNode(text));
        wrap.append(item);
      });
    const note = document.createElement("span");
    note.className = "legend-note";
    note.textContent = "Bright path = data flowing. Dashed amber = feedback. " +
      "Node colour = health of the page that publishes that engine's data.";
    wrap.append(note);
    return wrap;
  }

  window.Flow = { draw: draw, legend: legend, COLOR: COLOR, TEXT: TEXT, GROUP_COLOR: GROUP_COLOR };
})();
