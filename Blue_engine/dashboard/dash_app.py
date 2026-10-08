"""
dash_app.py - Dash/Plotly terminal for the POV engine.

A **read-only**, multi-page dashboard built on Dash Mantine Components (DMC).
It surfaces everything ``blue_engine`` produces but OpenAlgo's own dashboard
does not: the buyer-initiated VWAP band, dynamic POV rate, execution-quality
stats, risk-guard matrix and raw logs.

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

NOTHING IN THIS MODULE PLACES AN ORDER
--------------------------------------
The Terminal page exposes config-only controls (see
:mod:`blue_engine.dashboard.ui.inputs`): they write parameters to the config
the engine reads. No code path here reaches a broker. That is what makes the
dashboard safe to leave open.
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

import dash
import dash_mantine_components as dmc
from dash import dcc, html

logger = logging.getLogger(__name__)

# Pages live next to this module in ./pages.
_PAGES_DIR = Path(__file__).resolve().parent / "pages"

# Process-wide handle the callbacks read. Kept as a module global because
# Dash's Pages callbacks are registered at import time, before app.run().
#
# ``engine``      - the in-process LiveDashboard, or None for config-only.
# ``data_source`` - the dashboard's OWN market-data wiring ("paper" | "live"),
#                   kept SEPARATE from OpenAlgo's order-routing mode so the
#                   banner can report both without conflating them. Default
#                   "paper": the terminal is config-only and its reads are
#                   stubbed. See ``ui/mode.py`` for why the two axes are
#                   distinct.
_STATE: dict = {"engine": None, "data_source": "paper"}


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

    # Mode-aware theming is now a MantineProvider prop, not a stylesheet.
    # `forceColorScheme` pins dark/light to the OpenAlgo mode so a user with a
    # dark OS theme cannot be shown a light UI while trading live.
    from blue_engine.core.mode import read_mode
    from blue_engine.dashboard.ui.mode import banner_text, theme
    from blue_engine.dashboard.ui.provider import app_shell

    _mode = read_mode(getattr(engine, "client", None))
    _initial_mode = theme(_mode)
    # The panel data wiring is fixed for the process lifetime (you cannot
    # re-wire the readers without a restart), so it is read once here and both
    # the banner and /pov/mode report the SAME value. ``_STATE`` carries it to
    # the callbacks below.
    # NOTE: _STATE is a dict, so read it with .get(), not getattr().
    _data_source = _STATE.get("data_source") or "paper"
    app.index_string = _index_html(_initial_mode["key"])
    app.layout = app_shell(dash.page_container, mode=_mode,
                           data_source=_data_source)

    _register_refresh_callbacks(app)
    _register_mode_callbacks(app)

    # Service Control (Infrastructure page): status probes + the guarded
    # OpenAlgo START button. Registered here so the control has one owner;
    # it lives in ui/service_control.py alongside its safety rationale.
    try:
        from blue_engine.dashboard.ui import service_control
        service_control.register_callbacks(app)
    except Exception as exc:  # noqa: BLE001 - a missing control must not
        logger.warning("service_control callbacks not registered: %r", exc)

    # JSON endpoint the page shell polls so a mid-session analyzer/live
    # toggle re-themes the whole UI without a hard reload.
    #
    # It reports BOTH axes: ``mode`` is OpenAlgo's order routing (this can flip
    # mid-session), ``data_source`` is the dashboard's panel wiring (fixed for
    # the process). Shipping only the mode is what let a red "LIVE - REAL
    # MONEY" banner sit over simulated panels.
    @app.server.route("/pov/mode")
    def _pov_mode():
        m = read_mode(getattr(_STATE["engine"], "client", None))
        pal = theme(m)
        ds = _STATE.get("data_source") or "paper"
        return {"mode": pal["key"], "label": pal["label"],
                "color_scheme": pal["color_scheme"],
                "data_source": ds,
                "banner": banner_text(pal, data_source=ds)}

    return app


def _register_mode_callbacks(app: dash.Dash) -> None:
    """Re-theme the shell when OpenAlgo's analyzer/live mode flips.

    The interval lives in the shell (``ui/provider.app_shell``) and fires every
    2 s. We update two things together and they MUST stay together:

      * ``forceColorScheme`` on MantineProvider -- recolours every DMC widget.
      * the banner text and ``data-mode`` on <body> -- the human-facing signal.

    Updating only one is the failure mode this callback exists to prevent: a
    white UI with a black-theme banner, or a live-red banner over a dark shell,
    is worse than either being stale, because it looks intentional.
    """
    # AUDIT FIX (2026-10-08): ``read_mode``/``theme`` are imported inside
    # ``create_app`` (local scope), so referring to them here raised
    # ``NameError: name 'read_mode' is not defined`` on EVERY mode poll -- a
    # 500 on /_dash-update-component every 2 s (691 callbacks failed in one
    # session). Import them into THIS function's scope so the callback resolves.
    from blue_engine.core.mode import read_mode
    from blue_engine.dashboard.ui.mode import banner_text, theme

    @app.callback(
        dash.Output("mantine-provider", "forceColorScheme"),
        dash.Output("mode-poll-target", "children"),
        dash.Input("mode-poll", "n_intervals"),
        prevent_initial_call=True,
    )
    def _sync_mode(_n):
        m = read_mode(getattr(_STATE["engine"], "client", None))
        pal = theme(m)
        # Report BOTH axes, exactly as the server-rendered banner did. The
        # data_source is fixed for the process (set in ``main``); re-reading it
        # here keeps this callback from silently DROPPING the panel clause on
        # the first poll -- which would re-introduce a bare "LIVE - REAL MONEY"
        # over paper panels ~2 s after a correct first paint.
        ds = _STATE.get("data_source") or "paper"
        # A hidden node carries the data-mode flips to the client script; the
        # banner's own span is rewritten there too so we do not fight Dash's
        # renderer over the same DOM node every 2 seconds.
        return pal["color_scheme"], html.Span(
            banner_text(pal, data_source=ds), id="mode-label-current",
            **{"data-mode": pal["key"], "data-source": ds},
        )



def _index_html(initial_mode: str = "analyzer") -> str:
    """Best-effort page shell. Dash uses ``index_string`` verbatim.

    Only STRUCTURAL css is injected here (``pages.theme._THEME_CSS``). Colours,
    radii and typography belong to Dash Mantine Components and are set on the
    ``MantineProvider`` from ``ui/provider.py`` -- injecting a second colour
    system here is exactly the drift that makes a live/analyzer mix-up possible.

    ``data-mode`` on <body> is retained purely as a hook for the tiny client
    script below (and for anyone debugging in devtools); nothing styles off it.
    """
    from blue_engine.dashboard.pages.theme import _THEME_CSS as _STRUCTURAL_CSS

    return f"""<!DOCTYPE html>
<html>
  <head>
    {{%metas%}}
    <title>{{%title%}}</title>
    {{%favicon%}}
    {{%css%}}
    <style>{_STRUCTURAL_CSS}</style>
  </head>
  <body data-mode="{initial_mode}">
    {{%app_entry%}}
    <footer>{{%config%}}{{%scripts%}}{{%renderer%}}</footer>
    <script>
      // Embedded mode: when loaded inside OpenAlgo's UI iframe
      // (?embed=1), hide Dash's own left sidebar so only the content
      // area renders -- OpenAlgo's navbar/sidebar provide the chrome.
      (function embed() {{
        if (new URLSearchParams(window.location.search).get('embed') === '1') {{
          document.body.classList.add('embedded');
          var shell = document.querySelector('.app-body');
          if (shell) shell.classList.add('embedded');
        }}
      }})();

      // -----------------------------------------------------------------
      // Mode sync: the belt-and-braces layer.
      //
      // The DMC colour scheme is switched by the Dash callback in
      // _register_mode_callbacks (that is what recolours every component).
      // This script covers the two cases a Dash callback does NOT:
      //
      //   1. a flip made in OpenAlgo's tab while this tab is hidden -- there
      //      is no callback trigger for "user came back", so we refresh on
      //      focus and on visibilitychange;
      //   2. the banner text, which must never be blank or stale, so we keep
      //      last-known-good on any fetch failure rather than clearing it.
      //
      // It writes data-mode on <body> only (a debug hook); it deliberately
      // does NOT touch the banner span, because Dash owns that node and two
      // writers on one node is how it ends up out of sync.
      // -----------------------------------------------------------------
      (function modeSync() {{
        var last = null;

        function apply(j) {{
          if (!j || j.mode === last) return;
          last = j.mode;
          document.body.setAttribute('data-mode', j.mode);
        }}

        function refresh() {{
          return fetch('/pov/mode', {{ cache: 'no-store' }})
            .then(function (r) {{ return r.ok ? r.json() : null; }})
            .then(function (j) {{ if (j) apply(j); }})
            .catch(function () {{ /* keep last known mode; never blank it */ }});
        }}

        refresh();                                  // immediate, on load
        window.addEventListener('focus', refresh);  // tab/window focus
        document.addEventListener('visibilitychange', function () {{
          if (!document.hidden) refresh();          // became visible
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
        #
        # FIXED 2026-10-08 ("MODE UNKNOWN" on every page): the gate used to be
        # ``os.getenv("OPENALGO_API_KEY")``, i.e. the PROCESS environment only.
        # Nothing ever exports the key into the dashboard's environment, so the
        # gate was always false and the banner read "MODE UNKNOWN" even with
        # OpenAlgo up and the key sitting in ``_restored/.env``. The rest of the
        # system resolves credentials through
        # ``economics.providers.secrets.get_secret`` (env -> _restored/.env ->
        # repo/.env), so the dashboard was the one surface that disagreed with
        # it. We now use the SAME resolver, which is the single place AGENTS.md
        # says credentials may be read, and hand the key to init_client
        # explicitly so the SDK does not re-derive it from a stale env.
        client = None
        try:
            from blue_engine.economics.providers.secrets import has_secret, get_secret
            from blue_engine.core.client import init_client

            if has_secret("OPENALGO_API_KEY"):
                client = init_client(api_key=get_secret("OPENALGO_API_KEY"))
                logger.info("mode probe: SDK client attached (key resolved)")
            else:
                logger.warning("mode probe: OPENALGO_API_KEY not found in env, "
                               "_restored/.env or .env; banner will read "
                               "'MODE UNKNOWN'")
        except Exception as exc:  # noqa: BLE001
            logger.warning("mode probe: no SDK client (%s); "
                           "banner will read 'MODE UNKNOWN'", exc)

        # Paper mode by default: no broker calls, but lets the terminal
        # show the state machines for the default watchlist.
        #
        # TWO AXES, and this line is where they are decided (see ui/mode.py):
        #   * OpenAlgo mode  = where ORDERS go (read-only, from the platform).
        #   * DATA_SOURCE    = where the PANELS' prices come from (this flag).
        #
        # DATA_SOURCE is derived, not guessed: the dashboard is config-only
        # (SOP 4) so it has NO order path at all, and its reads are stubbed
        # unless POV_DASH_LIVE=1 asks for real candles/quotes. Deriving it from
        # the same flag that builds the functions means the banner can never
        # claim a wiring the engine did not actually get.
        _live_reads = os.getenv("POV_DASH_LIVE", "") in {"1", "true", "yes"}
        _data_source = "live" if _live_reads else "paper"
        engine = LiveDashboard(
            DashboardConfig(),
            history_fn=_resolve_history_fn(_live_reads),
            quotes_fn=_resolve_quotes_fn(_live_reads),
            client=client,
        )
        # Publish the wiring so the banner and /pov/mode agree with what the
        # engine was ACTUALLY built with. Set only after the engine exists, so
        # a failure here leaves the safe "paper" default in place.
        _STATE["data_source"] = _data_source
        from blue_engine.core.mode import read_mode

        logger.info("attached in-process engine (panels=%s): %d symbols, "
                    "openalgo_mode=%s",
                    _data_source, len(engine._machines),  # noqa: SLF001
                    read_mode(client))
    except Exception as exc:  # pragma: no cover - optional
        logger.info("running without a live engine (config+ledger only): %s", exc)

    app = create_app(engine=engine)
    logger.info("Dash terminal on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
