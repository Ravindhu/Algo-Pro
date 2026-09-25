"""
dash_app.py - Dash/Plotly terminal for the POV engine.

A **read-only**, multi-page dashboard that surfaces everything
``blue_engine`` produces but OpenAlgo's own dashboard does not: the
buyer-initiated VWAP band, dynamic POV rate, execution-quality stats,
risk-guard matrix and raw logs.

Run it:

    python -m blue_engine.dashboard.dash_app
    # or
    python blue_engine/dashboard/dash_app.py

Then open http://127.0.0.1:8050

Optional environment
--------------------
    POV_CONFIG_PATH      override the config JSON path
    POV_DASH_HOST        bind host (default 127.0.0.1)
    POV_DASH_PORT        bind port (default 8050)
    POV_DASH_DEBUG       "1" to enable Dash debug/reload

Attaching a live engine
-----------------------
By default the pages read the config + ledger. If you want the pages to
also show the *in-process* paper/live ``LiveDashboard`` state, host the
Dash app and the engine in the same process::

    from blue_engine.dashboard.app import LiveDashboard, DashboardConfig
    from blue_engine.dashboard.dash_app import create_app

    engine = LiveDashboard(DashboardConfig(), history_fn=..., quotes_fn=...)
    app = create_app(engine=engine)
    app.run(debug=False)

Nothing in this module places an order.
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

import dash
from dash import dcc, html
logger = logging.getLogger(__name__)

# Pages live next to this module in ./pages.
_PAGES_DIR = Path(__file__).resolve().parent / "pages"

# In-process engine handle. Set by :func:`create_app` and read by the
# page callbacks below. Kept as a module global because Dash's Pages
# callbacks are registered at import time, before app.run() is called.
_STATE: dict = {"engine": None}


# ---------------------------------------------------------------------------
# Theme (analyzer = black, live = white) â€” see pages/theme.py
# ---------------------------------------------------------------------------

from blue_engine.dashboard.pages.theme import _THEME_CSS as _PALETTE_CSS
from blue_engine.core.mode import read_mode


def _sidebar() -> html.Div:
    """The left nav. Dash Pages marks links active via ``use_pages``."""
    return html.Div(
        [
            html.H1("POV Terminal"),
            dcc.Link("Terminal", href="/", className="nav-link"),
            dcc.Link("Portfolio", href="/portfolio", className="nav-link"),
            dcc.Link("Portfolio Analytics", href="/analytics-portfolio",
                     className="nav-link"),
            dcc.Link("Risk Control", href="/risk", className="nav-link"),
            dcc.Link("Analytics", href="/analytics", className="nav-link"),
            dcc.Link("Economic", href="/economic", className="nav-link"),
            dcc.Link("Infrastructure", href="/infra", className="nav-link"),
        ],
        className="sidebar",
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(*, engine: Optional[Any] = None, config_path: Optional[str] = None,
               external_stylesheets: Optional[list] = None) -> dash.Dash:
    """Create and return the configured Dash app.

    Args:
        engine: an optional in-process ``LiveDashboard`` to read live
            state from. ``None`` -> pages show config + ledger only.
        config_path: override the config JSON path for this app.
        external_stylesheets: pass-through for Dash extras.

    Returns:
        A ``dash.Dash`` app with all pages registered.
    """
    _STATE["engine"] = engine
    if config_path:
        os.environ["POV_CONFIG_PATH"] = str(config_path)

    app = dash.Dash(
        __name__,
        use_pages=True,
        pages_folder=str(_PAGES_DIR),
        title="POV Terminal",
        update_title=None,
        external_stylesheets=external_stylesheets or [],
        suppress_callback_exceptions=True,
    )

    # Mode-aware theme: black = analyzer, white = live. Read once at
    # startup; the client JS at the bottom of _index_html re-syncs
    # data-mode on every page refresh so a mid-session toggle is picked up.
    from blue_engine.dashboard.pages.theme import mode_badge, theme

    _initial_mode = theme(read_mode(getattr(engine, "client", None)))
    app.index_string = _index_html(_initial_mode["key"])
    # Layout: a full-width mode banner on TOP, then the sidebar + content in a
    # flex row BELOW it. The banner must NOT be a flex sibling of the sidebar -
    # otherwise it stretches to full height as a flex column and covers the page
    # (the "MODE UNKNOWN covers half the page" bug).
    app.layout = html.Div(
        [
            mode_badge(_initial_mode),
            html.Div(
                [
                    _sidebar(),
                    html.Div(dash.page_container, className="content"),
                ],
                className="app-body",
            ),
        ],
        className="app-shell",
    )

    _register_refresh_callbacks(app)

    # JSON endpoint the page shell polls so a mid-session analyzer/live
    # toggle re-themes the whole UI without a hard reload.
    @app.server.route("/pov/mode")
    def _pov_mode():
        from blue_engine.dashboard.pages.theme import theme

        m = read_mode(getattr(_STATE["engine"], "client", None))
        pal = theme(m)
        return {"mode": pal["key"], "label": pal["label"]}

    return app


def _index_html(initial_mode: str = "analyzer") -> str:
    """Best-effort page shell. Dash uses ``index_string`` verbatim.

    ``data-mode`` on <body> drives every CSS rule in the palette
    (black theme for "analyzer", white theme for "live"). A tiny
    inline script re-reads the current mode from the server on each
    render so a mid-session toggle updates the whole shell.
    """
    return f"""<!DOCTYPE html>
<html>
  <head>
    {{%metas%}}
    <title>{{%title%}}</title>
    {{%favicon%}}
    {{%css%}}
    <style>{_PALETTE_CSS}</style>
  </head>
  <body data-mode="{initial_mode}">
    {{%app_entry%}}
    <footer>{{%config%}}{{%scripts%}}{{%renderer%}}</footer>
    <script>
      // Embedded mode: when loaded inside OpenAlgo's UI iframe
      // (?embed=1), hide Dash's own left sidebar so only the content
      // area renders â€” OpenAlgo's navbar/sidebar provide the chrome.
      if (new URLSearchParams(window.location.search).get('embed') === '1') {{
        document.body.classList.add('embedded');
      }}
      (async function syncMode() {{
        try {{
          const r = await fetch('/pov/mode');
          if (!r.ok) return;
          const j = await r.json();
          document.body.setAttribute('data-mode', j.mode);
          const b = document.querySelector('.mode-banner > span');
          if (b) b.textContent = j.label;
        }} catch (e) {{ /* keep initial mode */ }}
      }})();
    </script>
  </body>
</html>"""


def _register_refresh_callbacks(app: dash.Dash) -> None:
    """Wire each page's Interval to a re-render of that page's container.

    The pages are pure functions of the current snapshot, so an interval tick
    simply rebuilds the page layout. This keeps the pages stateless.

    Identity note: we import the pages by the SAME name Dash used when it
    discovered them via ``pages_folder``. Dash adds the pages folder's parent to
    ``sys.path`` and imports them as ``pages.<name>``; importing them under the
    canonical package path instead would create a *second* module object for the
    same file and double-register the page (Dash then raises duplicate paths).
    """
    page_modules = {
        "terminal-refresh": "terminal",
        "portfolio-refresh": "portfolio",
        "risk-refresh": "risk_control",
        "analytics-refresh": "analytics",
        "economic-refresh": "economic",
        "infra-refresh": "infrastructure",
    }

    for interval_id, page_name in page_modules.items():
        # Dash discovers pages from ``pages_folder`` and imports each as the
        # top-level module ``pages.<name>`` (it adds the folder's parent to
        # sys.path). Look up those identities first; fall back to the canonical
        # package path in case a page was imported normally elsewhere.
        module = (sys.modules.get(f"pages.{page_name}")
                  or sys.modules.get(f"blue_engine.dashboard.pages.{page_name}"))
        if module is None:
            try:
                module = importlib.import_module(
                    f"blue_engine.dashboard.pages.{page_name}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("refresh callback skipped for %s: %s",
                               page_name, exc)
                continue
        if not hasattr(module, "body"):
            logger.warning("refresh callback skipped: %s has no body()",
                           page_name)
            continue

        def _make(m=module):
            def _refresh(_n, **_kwargs):
                return m.body()

            return _refresh
        app.callback(
            dash.Output(f"{interval_id}-out", "children"),
            dash.Input(interval_id, "n_intervals"),
        )(_make())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(
        level=os.getenv("POV_DASH_LOGLEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.getenv("POV_DASH_HOST", "127.0.0.1")
    port = int(os.getenv("POV_DASH_PORT", "8050"))
    debug = os.getenv("POV_DASH_DEBUG", "") in {"1", "true", "yes"}

    engine = None
    try:
        from blue_engine.dashboard.app import DashboardConfig, LiveDashboard
        from blue_engine.dashboard.cli import _resolve_history_fn, _resolve_quotes_fn

        # Paper mode by default: no broker calls, but lets the terminal
        # show the state machines for the default watchlist.
        engine = LiveDashboard(
            DashboardConfig(),
            history_fn=_resolve_history_fn(False),
            quotes_fn=_resolve_quotes_fn(False),
        )
        logger.info("attached in-process engine (paper): %d symbols",
                    len(engine._machines))  # noqa: SLF001
    except Exception as exc:  # pragma: no cover - optional
        logger.info("running without a live engine (config+ledger only): %s", exc)

    app = create_app(engine=engine)
    logger.info("Dash terminal on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
