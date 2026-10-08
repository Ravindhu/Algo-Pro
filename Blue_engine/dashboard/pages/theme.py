"""
theme.py - DEPRECATED shim, kept so existing imports keep working.

The palette and the mode mapping now live in
:mod:`blue_engine.dashboard.ui.mode`, the single source of truth shared by the
DMC shell (:mod:`blue_engine.dashboard.ui.provider`) and the Plotly figures.

This module used to carry ~95 lines of hand-written CSS (``_THEME_CSS``)
driving ``body[data-mode=...]``. That CSS is gone: Dash Mantine Components
themes components through ``MantineProvider`` props, not global stylesheets, so
keeping both would mean two competing sources of truth for the colours.

Callers should import from ``blue_engine.dashboard.ui.mode`` directly:

    from blue_engine.dashboard.ui.mode import theme, scheme_for_mode

This shim exists only to avoid a flag-day rename across the nine pages; it is
removed once every page has migrated.
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
# DMC owns colours, radii and typography (via MantineProvider). What it does
# NOT own is the two-column layout of this app and the mode banner, so those
# stay here. Everything colour-related that used to live in this file has been
# deleted rather than duplicated -- two sources of truth for "what colour is
# live mode" is how the UI and the safety banner drift apart.
_THEME_CSS = """
body { margin: 0; }
.app-shell { display: flex; flex-direction: column; min-height: 100vh; }
.app-body { display: flex; flex: 1; min-height: 0; }
.app-body .sidebar { width: 220px; flex: 0 0 220px; padding: 16px 12px;
                     position: sticky; top: 0; height: 100vh; overflow-y: auto;
                     border-right: 1px solid rgba(128,128,128,.25); }
.app-body .content { flex: 1; padding: 20px 24px; overflow-x: auto; min-width: 0; }
.nav-link { display: block; padding: 7px 10px; border-radius: 8px;
            text-decoration: none; color: inherit; }
.nav-link:hover { background: rgba(128,128,128,.14); }
.mode-banner { text-align: center; padding: 7px 14px; font-size: 13px;
               font-weight: 800; letter-spacing: .12em; flex: 0 0 auto;
               width: 100%%; box-sizing: border-box; }
.empty { font-style: italic; opacity: .7; padding: 6px 0; }
/* Embedded inside OpenAlgo's UI: OpenAlgo supplies the chrome. */
.app-body.embedded .sidebar { display: none; }
.app-body.embedded .content { padding: 14px 18px; }
"""

__all__ = ["COLOR_SCHEMES", "scheme_for_mode", "theme"]
