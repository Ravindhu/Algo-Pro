"""
theme.py - analyzer/live mode theming for the Dash terminal.

The whole point: you must NEVER confuse analyzer (simulated) mode with
live (real money) mode. So the UI switches its entire palette:

    ANALYZER  ->  BLACK  (dark, purple-tinted accent, "SIMULATED" banner)
    LIVE      ->  WHITE  (light, red-tinted accent, "LIVE — REAL MONEY" banner)
    unknown   ->  falls back to black but shows a "?" warning banner

Usage from a page:
    from pov_engine.dashboard.pages.theme import theme, mode_badge
    t = theme(_data.gather().mode)
    html.Div(mode_badge(t), className="mode-banner")

Colors are exposed both as CSS (injected by dash_app) and as a dict for
Plotly figures / inline styles.
"""
from __future__ import annotations

from typing import Any, Dict

import dash.html as html

# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

_ANALYZER: Dict[str, Any] = {
    "key": "analyzer",
    "label": "ANALYZER (SIMULATED)",
    # banner: purple, unmistakably "not real money"
    "banner_bg": "#2e1065",
    "banner_fg": "#e9d5ff",
    "banner_border": "#7c3aed",
    # surfaces
    "body_bg": "#0b1220",
    "body_fg": "#e2e8f0",
    "panel_bg": "#0f172a",
    "panel_border": "#1e293b",
    "muted_fg": "#94a3b8",
    "table_hover": "#131f36",
    "row_border": "#16233b",
    # plotly
    "grid": "rgba(180,160,255,0.12)",
    "paper": "rgba(0,0,0,0)",
}

_LIVE: Dict[str, Any] = {
    "key": "live",
    "label": "LIVE — REAL MONEY",
    # banner: red on white, unmistakably "this is real"
    "banner_bg": "#fef2f2",
    "banner_fg": "#b91c1c",
    "banner_border": "#dc2626",
    # surfaces: white/light
    "body_bg": "#ffffff",
    "body_fg": "#0f172a",
    "panel_bg": "#f8fafc",
    "panel_border": "#e2e8f0",
    "muted_fg": "#64748b",
    "table_hover": "#eef2f7",
    "row_border": "#e2e8f0",
    # plotly
    "grid": "rgba(15,23,42,0.08)",
    "paper": "rgba(0,0,0,0)",
}

_PALETTES: Dict[str, Dict[str, Any]] = {
    "analyzer": _ANALYZER,
    "analyze": _ANALYZER,   # openalgo reports "analyze"
    "live": _LIVE,
}


def theme(mode: str) -> Dict[str, Any]:
    """Return the palette dict for a mode string ("live"/"analyze"/...)."""
    pal = _PALETTES.get((mode or "").lower().strip())
    if pal is None:
        # Unknown -> analyzer-style dark, but flag it so nobody assumes.
        pal = dict(_ANALYZER)
        pal["label"] = "MODE UNKNOWN — VERIFY IN OPENALGO UI"
        pal["banner_bg"] = "#450a0a"
        pal["banner_fg"] = "#fecaca"
        pal["banner_border"] = "#dc2626"
    return pal


def mode_badge(pal: Dict[str, Any]) -> html.Div:
    """A persistent, always-visible banner stating the current mode."""
    return html.Div(
        html.Span(pal["label"]),
        className="mode-banner",
        style={
            "background": pal["banner_bg"],
            "color": pal["banner_fg"],
            "border": f"1px solid {pal['banner_border']}",
        },
    )


# ---------------------------------------------------------------------------
# CSS (static part; dynamic colors go inline via the palette dict)
# ---------------------------------------------------------------------------

_THEME_CSS = """
:root { }
body { margin: 0; font-family: 'Segoe UI', system-ui, sans-serif; }
body[data-mode="analyzer"] { background: #0b1220; color: #e2e8f0; }
body[data-mode="live"] { background: #ffffff; color: #0f172a; }

body[data-mode="analyzer"] :root { color-scheme: dark; }
body[data-mode="live"] :root { color-scheme: light; }

.app-shell { display: flex; min-height: 100vh; }

.sidebar { width: 220px; padding: 18px 0; position: sticky; top: 0;
           height: 100vh; border-right: 1px solid; }
body[data-mode="analyzer"] .sidebar { background: #0f172a; border-color: #1e293b; }
body[data-mode="live"] .sidebar { background: #f1f5f9; border-color: #e2e8f0; }

.sidebar h1 { font-size: 15px; letter-spacing: .08em; text-transform: uppercase;
              padding: 0 18px 10px; margin: 0; }
body[data-mode="analyzer"] .sidebar h1 { color: #64748b; }
body[data-mode="live"] .sidebar h1 { color: #475569; }

.nav-link { display: block; padding: 10px 18px; text-decoration: none;
            font-size: 14px; border-left: 3px solid transparent; }
body[data-mode="analyzer"] .nav-link { color: #cbd5e1; }
body[data-mode="live"] .nav-link { color: #334155; }
body[data-mode="analyzer"] .nav-link:hover { background: #1e293b; color: #fff; }
body[data-mode="live"] .nav-link:hover { background: #e2e8f0; color: #000; }
body[data-mode="analyzer"] .nav-link.active { border-left-color: #7c3aed;
    background: #1e293b; color: #fff; }
body[data-mode="live"] .nav-link.active { border-left-color: #dc2626;
    background: #e2e8f0; color: #000; }

.content { flex: 1; padding: 22px 26px; overflow-x: auto; }
.page h2 { margin-top: 0; font-weight: 600; }

.mode-banner { position: sticky; top: 0; z-index: 50; text-align: center;
               padding: 7px 14px; font-size: 13px; font-weight: 800;
               letter-spacing: .12em; }

.kpi-row { display: flex; gap: 14px; flex-wrap: wrap; margin: 14px 0 22px; }
.kpi-card { border-radius: 10px; padding: 12px 18px; min-width: 150px;
            border: 1px solid; }
body[data-mode="analyzer"] .kpi-card { background: #0f172a; border-color: #1e293b; }
body[data-mode="live"] .kpi-card { background: #f8fafc; border-color: #e2e8f0; }
.kpi-label { font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
body[data-mode="analyzer"] .kpi-label { color: #64748b; }
body[data-mode="live"] .kpi-label { color: #64748b; }
.kpi-value { font-size: 22px; font-weight: 600; margin-top: 4px; }

.panel { border-radius: 10px; padding: 14px 18px; margin-bottom: 18px;
         border: 1px solid; }
body[data-mode="analyzer"] .panel { background: #0f172a; border-color: #1e293b; }
body[data-mode="live"] .panel { background: #f8fafc; border-color: #e2e8f0; }
.panel h3 { margin: 0 0 10px; font-size: 14px; text-transform: uppercase;
            letter-spacing: .05em; }
body[data-mode="analyzer"] .panel h3 { color: #94a3b8; }
body[data-mode="live"] .panel h3 { color: #475569; }

.data-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.data-table th { text-align: left; font-weight: 600; padding: 8px 10px;
                 border-bottom: 1px solid; }
body[data-mode="analyzer"] .data-table th { color: #64748b;
    border-color: #1e293b; }
body[data-mode="live"] .data-table th { color: #64748b; border-color: #e2e8f0; }
.data-table td { padding: 7px 10px; border-bottom: 1px solid; }
body[data-mode="analyzer"] .data-table td { border-color: #16233b; }
body[data-mode="live"] .data-table td { border-color: #e2e8f0; }
body[data-mode="analyzer"] .data-table tr:hover td { background: #131f36; }
body[data-mode="live"] .data-table tr:hover td { background: #eef2f7; }

.mono { font-family: 'Cascadia Mono', Consolas, monospace; }
.pill { display: inline-block; padding: 2px 9px; border-radius: 999px;
        font-size: 11px; font-weight: 700; letter-spacing: .04em; }
.empty { font-style: italic; padding: 8px 0; }
body[data-mode="analyzer"] .empty { color: #64748b; }
body[data-mode="live"] .empty { color: #94a3b8; }
.log-pre { border-radius: 8px; padding: 12px; max-height: 420px; overflow: auto;
           font-size: 12px; white-space: pre-wrap; border: 1px solid; }
body[data-mode="analyzer"] .log-pre { background: #0a1220; color: #a5b4c8;
    border-color: #1e293b; }
body[data-mode="live"] .log-pre { background: #f1f5f9; color: #334155;
    border-color: #e2e8f0; }

/* Embedded in OpenAlgo's UI (iframe): OpenAlgo supplies the chrome,
   so hide Dash's own sidebar and let the content fill the frame. */
body.embedded .sidebar { display: none; }
body.embedded .app-shell { display: block; }
body.embedded .content { padding: 16px 20px; }
"""
