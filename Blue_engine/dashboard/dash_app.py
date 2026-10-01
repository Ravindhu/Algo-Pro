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
            dcc.Link("ML Models", href="/ml", className="nav-link"),
            dcc.Link("Optimizer", href="/optimizer", className="nav-link"),
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

      // -----------------------------------------------------------------
      // Mode sync: keep the analyzer/live banner INSTANT.
      //
      // The banner is the one signal that must never lag, because it is what
      // tells the operator whether they are about to trade real money. A
      // version of this that only ran on page load meant a mid-session flip
      // in the OpenAlgo UI was invisible until a manual refresh -- measured
      // 0 self-updates in 13s of idle observation.
      //
      // Three triggers, because each covers a case the others miss:
      //   1. poll  every MODE_POLL_MS  - catches a flip while watching
      //   2. focus                     - catches a flip made in OpenAlgo's
      //                                  tab and then switched back to
      //   3. visibilitychange          - same, for tab-hide (mobile/alt-tab),
      //                                  and it avoids polling a hidden tab
      //
      // Deliberately a plain fetch loop and not a WebSocket: this endpoint is
      // one tiny JSON read, the Dash server owns no socket here, and a poll
      // keeps working if the WS proxy on :8765 is down.
      // -----------------------------------------------------------------
      (function modeSync() {{
        var MODE_POLL_MS = 2000;
        var last = null;

        function apply(j) {{
          // Skip no-op DOM writes; a needless setAttribute on every poll
          // would retrigger style recalc (and any MutationObserver) 30x/min.
          if (j.mode === last) return;
          last = j.mode;
          document.body.setAttribute('data-mode', j.mode);
          var b = document.querySelector('.mode-banner > span');
          if (b) b.textContent = j.label;
        }}

        function refresh() {{
          return fetch('/pov/mode', {{ cache: 'no-store' }})
            .then(function (r) {{ return r.ok ? r.json() : null; }})
            .then(function (j) {{ if (j) apply(j); }})
            .catch(function () {{ /* keep last known mode; never blank it */ }});
        }}

        refresh();                                  // 1. immediate, on load
        setInterval(refresh, MODE_POLL_MS);         // 1. steady poll
        window.addEventListener('focus', refresh);  // 2. tab/window focus
        document.addEventListener('visibilitychange', function () {{
          if (!document.hidden) refresh();          // 3. became visible
        }});
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
        # Phase 6: the portfolio-analytics console. Without this entry the page's
        # dcc.Interval ticks forever and nothing re-renders, so the panels freeze
        # at whatever they showed on first paint.
        "analytics-portfolio-refresh": "analytics_portfolio",
        "risk-refresh": "risk_control",
        "analytics-refresh": "analytics",
        "economic-refresh": "economic",
        "infra-refresh": "infrastructure",
        # ML scoreboard. Reads the signed artifacts the training orchestrator
        # wrote; without this entry the page's Interval ticks and nothing
        # re-renders, so a finished training run would be invisible until a
        # manual browser refresh.
        "ml-refresh": "ml_models",
        # AUDIT FINDING D1 (2026-10-01). The strategy Optimizer page is
        # read-only and replays the journal on every tick; without this entry
        # the page's Interval fires every 60 s but no callback listens, so
        # the operator would have to hard-refresh the browser to see a newly
        # completed study. The fix is symmetric with the ML scoreboard above:
        # 60s polling because a study is a measurement, not a stream.
        "optimizer-refresh": "optimizer",
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

        # AUDIT FINDING D11 (2026-10-01). If a page exposes a
        # ``register_callbacks(app)`` helper, that helper fully owns the
        # page's wiring (the helper itself wires the refresh callback).
        # The generic ``m.body()`` poller below is skipped to avoid a
        # ``DuplicateCallbackOutput`` for the same target.
        if hasattr(module, "register_callbacks"):
            try:
                module.register_callbacks(app)
            except Exception as exc:  # noqa: BLE001
                logger.warning("page %s register_callbacks failed: %s",
                               page_name, exc)
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

        # Attach a real SDK client purely so the mode banner can be truthful.
        #
        # The terminal is read-only and runs in paper mode, so history/quotes
        # stay stubbed. But `read_mode()` needs an OpenAlgo SDK client to call
        # /api/v1/analyzer, and a paper stub cannot answer that call -- with no
        # client the banner degraded to "MODE UNKNOWN - VERIFY IN OPENALGO UI"
        # on every page. Building the client here costs one HTTP round trip at
        # startup and makes LIVE vs ANALYZER accurate for the whole session.
        #
        # This is blocked deliberately: if OpenAlgo is not running, the banner
        # SHOULD say unknown rather than pretend to know the mode.
        client = None
        if os.getenv("OPENALGO_API_KEY"):
            try:
                from blue_engine.core.client import init_client

                client = init_client()
                logger.info("mode probe: SDK client attached")
            except Exception as exc:  # noqa: BLE001
                logger.warning("mode probe: no SDK client (%s); "
                               "banner will read 'MODE UNKNOWN'", exc)
        else:
            logger.warning("mode probe: OPENALGO_API_KEY unset; banner will "
                           "read 'MODE UNKNOWN'")

        # Paper mode by default: no broker calls, but lets the terminal
        # show the state machines for the default watchlist.
        engine = LiveDashboard(
            DashboardConfig(),
            history_fn=_resolve_history_fn(False),
            quotes_fn=_resolve_quotes_fn(False),
            client=client,
        )
        from blue_engine.core.mode import read_mode

        logger.info("attached in-process engine (paper): %d symbols, mode=%s",
                    len(engine._machines),  # noqa: SLF001
                    read_mode(client))
    except Exception as exc:  # pragma: no cover - optional
        logger.info("running without a live engine (config+ledger only): %s", exc)

    app = create_app(engine=engine)
    logger.info("Dash terminal on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
