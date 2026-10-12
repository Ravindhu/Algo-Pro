"""
theme.py - mode compatibility shim plus responsive DMC shell CSS.

The palette and the mode mapping now live in
:mod:`blue_engine.dashboard.ui.mode`, the single source of truth shared by the
DMC shell (:mod:`blue_engine.dashboard.ui.provider`) and the Plotly figures.

The old mode-dependent palette CSS was removed. ``_THEME_CSS`` now contains
only shared dark-shell surfaces and responsive layout; component colors and
the mode safety signal come from the shared provider and mode palette.

Callers should import from ``blue_engine.dashboard.ui.mode`` directly:

    from blue_engine.dashboard.ui.mode import theme, scheme_for_mode

The import shim remains until all page imports have migrated.
"""
from __future__ import annotations

from typing import Any, Dict

import dash.html as html

from blue_engine.dashboard.ui.mode import (  # noqa: F401
    COLOR_SCHEMES,
    scheme_for_mode,
    theme,
)

# ---------------------------------------------------------------------------
# Structural CSS only
# ---------------------------------------------------------------------------
# DMC owns component colours, radii and typography (via MantineProvider). This
# stylesheet owns the shared dark shell surfaces, responsive layout, and
# spacing. Mode colors remain in ui.mode to keep the safety banner canonical.
_THEME_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; background: #141517; }
body { color: #C1C2C5; font-family: 'Segoe UI', system-ui, sans-serif; }
.app-shell { display: flex; flex-direction: column; min-height: 100vh;
             background: #141517; }
.app-body { display: flex; flex: 1; min-height: 0; }
.app-body .sidebar { width: 248px; flex: 0 0 248px; padding: 22px 14px;
                     position: sticky; top: 0; height: 100vh; overflow-y: auto;
                     background: #1A1B1E; border-right: 1px solid #2C2E33; }
.app-body .content { flex: 1; padding: 28px 32px; overflow-x: auto;
                     min-width: 0; max-width: 1800px; margin: 0 auto; }
.nav-link { display: block; padding: 9px 11px; border-radius: 8px;
            text-decoration: none; color: #C1C2C5; transition: background .15s ease,
            color .15s ease; }
.nav-link:hover { background: #25262B; color: #fff; }
.nav-link:focus-visible { outline: 2px solid #748ffc; outline-offset: 2px; }
.mode-banner { text-align: center; padding: 8px 14px; font-size: 12px;
               font-weight: 800; letter-spacing: .08em; flex: 0 0 auto;
               width: 100%; box-sizing: border-box; }
.page h1, .page h2 { color: #E9ECEF; letter-spacing: -.02em; }
.panel, .kpi-card { background: #1A1B1E; border-color: #2C2E33; }
.empty { font-style: italic; opacity: .7; padding: 6px 0; }
@media (max-width: 900px) {
  .app-body .sidebar { width: 208px; flex-basis: 208px; }
  .app-body .content { padding: 22px 20px; }
}
@media (max-width: 640px) {
  .app-body { flex-direction: column; }
  .app-body .sidebar { width: 100%; height: auto; position: static;
                       padding: 12px; border-right: 0;
                       border-bottom: 1px solid #2C2E33; }
  .app-body .sidebar .sidebar-nav { display: flex; flex-direction: row;
                                    overflow-x: auto; }
  .app-body .content { padding: 18px 14px; }
  .workspace-header { align-items: flex-start; flex-wrap: wrap; }
}
/* Embedded inside OpenAlgo's UI: OpenAlgo supplies the chrome. */
.app-body.embedded .sidebar { display: none; }
.app-body.embedded .content { padding: 14px 18px; }
"""

__all__ = ["COLOR_SCHEMES", "scheme_for_mode", "theme"]
