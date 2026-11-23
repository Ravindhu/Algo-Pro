# High-Density Quant Terminal UI Design Rules (`UI_RULES.md`)

## 1. Visual Identity & Surface Rules
- **Base Canvas:** Deep obsidian-black background (`#05080D`) with zero white or light-gray surfaces anywhere in the UI.
- **Panel Containers:** Elevated dark navy-slate surfaces (`#0A1018`) bounded by crisp, continuous `1px` structural grid lines (`#162231`) and sharp corners (`border-radius: 2px` to `4px`). Avoid soft drop shadows, gradients on cards, or rounded consumer-style bubbles.
- **Grid Geometry:** Pack panels edge-to-edge in a tight bento/terminal grid anchored to a `100vh` desktop viewport (`gap: 4px` to `6px`). Internal scrolling is allowed strictly inside data tables, log streams, and popup bodies.

## 2. Color Palette & Semantic Restraint
- **Neutral Dominance:** Keep 85%+ of the screen in dark obsidian, slate, and muted steel grays.
- **Strict Semantic Accents:**
  - **Positive / Long / Active:** Electric Emerald (`#00E676` / `#10B981`)
  - **Negative / Short / Alert:** Crimson Red (`#FF3B5C` / `#EF4444`)
  - **Highlights / Signals / Nodes:** Amber Gold (`#FFD166` / `#F59E0B`)
  - **Telemetry / Connectors / Focus:** Electric Cyan (`#00D4FF` / `#38BDF8`)
  - **Text Hierarchy:** Primary crisp off-white (`#E2E8F0`), secondary slate (`#94A3B8`), muted labels (`#475569`).

## 3. Typography & Micro-Layout Rules
- **Monospace Terminal Standard:** Use a crisp monospace typeface (`JetBrains Mono`, `Fira Code`, or `IBM Plex Mono`) for all numbers, tickers, tables, timestamps, badges, and logs; pair with a clean condensed geometric sans-serif for micro section headers.
- **Tabular Numerals:** Enforce `font-variant-numeric: tabular-nums` on every numeric value to eliminate horizontal jitter during high-frequency updates.
- **Compact Type Scale:**
  - Top Bar Brand & Hero KPI: `18px–26px` bold monospace
  - Section Headers: `10px–11px` uppercase bold with `0.08em` letter-spacing
  - Table Cells, Logs & Inputs: `11px–12px` regular monospace
  - Status Pills & Micro Badges: `9px–10px` uppercase semibold

## 4. Data Visualization & Canvas Rules
- **Centerpiece Interactive Canvas:** Render complex network/model visualizations using hardware-accelerated `HTML5 Canvas` or `WebGL/Three.js` with glowing particle nodes, vector connector lines, and smooth `60fps` rotation/pulse transitions.
- **Inline Micro-Visuals:** Embed thin horizontal progress/depth bars directly behind table cells and KPI cards using translucent semantic fills (`15%–25%` opacity) to maximize information density without extra containers.
- **Charts & Curves:** Use dark-grid candlestick, multi-line latency, and dual-color threshold spline charts (transitioning smoothly between crimson below baseline and emerald above baseline) with minimal axis chrome.

## 5. Interactive Popups, Modals & Drawers
- **Clean Canvas, Layered Inputs:** Keep the primary terminal grid unobstructed by multi-field forms. Trigger all configuration forms, parameter editors, and deep inspectors inside **centered modal popups** or **right-side slide-over drawers**.
- **Popup Styling:** Match the terminal theme (`#0A1018` surface, `1px solid #1E293B` border, `backdrop-filter: blur(4px)` with `70%` dark overlay).
- **Standardized Popup Controls:**
  - Include a compact uppercase header bar with a `✕` close button.
  - Support closing via `ESC` key and backdrop click.
  - Use `150ms` ease-out open/close transitions.
  - Display non-intrusive corner toast notifications (`3s` auto-dismiss) for action feedback.

## 6. Component States & Micro-Interactions
- **Hover & Focus Affordance:** Every clickable pill, table row, log line, and canvas node must show `cursor: pointer` and a crisp `1px` cyan/amber border or subtle surface tint (`#111B27`) on hover.
- **Explicit State Styling:** Define distinct dark-mode visual treatments for `Default`, `Hover`, `Active`, `Disabled` (`40%` opacity), `Loading` (minimal pulse bar), `Empty` (muted centered monospace label), and `Error` (`1px` crimson border).
