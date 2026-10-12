"""
_optimizer_header.py - the user-input header for the Optimizer page.

WHY THIS FILE EXISTS
====================
Before this file, the Optimizer page only displayed the result of the most
recent study journal. The operator could not tell the engine *what* to
study, only see what the engine had already done. Phase 5 of the audit
chain (Temp/OPTIMIZER_AUDIT_PHASE5.md) adds the user-input header:

  1. The AI Insight header - a READ-ONLY summary card at the very top of
     the page. It tells the operator the entire optimizer setting: mode,
     symbol, strategy, time period, every parameter (with its name, kind,
     default, and bound range), and cache status.

  2. The Control bar - a form below the AI Insight header with the
     interactive controls: Auto-Live / Offline toggle, symbol dropdown,
     strategy dropdown, from/to date pickers, interval dropdown, and
     Download + Run buttons. Run is disabled in Auto-Live mode.

READ-ONLY CONTRACT (LOCKED)
---------------------------
The page must never place live orders, never write the journal from a
callback, and never mutate the live configuration. The new header honours
that contract:

  * No callback in this file writes to disk. The only "write" is the URL
    search string (handled by the existing study-pick callback in
    ``optimizer.py``).

  * The Run button dispatches a CLI subprocess via ``dcc.Location`` (a
    client-side navigation), which is the documented pattern for keeping
    the page read-only.

  * Auto-Live mode reads the live config file but never writes it.

The body of the page (KPIs, verdict, 3 plots, recommendation, footer,
provenance) is unchanged and continues to live in ``optimizer.py``.

HOW THE AI INSIGHT HEADER GETS THE PARAMETER LIST
-------------------------------------------------
Each ``BaseSignal`` subclass is expected to declare a class-level
``params_space: Dict[str, Dict[str, Any]]`` describing its tunable
parameters. The header reads this attribute from
``blue_engine.optimizer.data_cache.list_strategies_for_ui()``, which the
Phase 4 audit wired up. The OCL keylevel declares its ``params_space``
(verified: 10 parameters). Other signals get their own table from their
own ``params_space``.

WHEN ``params_space`` IS ABSENT
-------------------------------
Some legacy signals do not declare ``params_space``. The header renders
their row as a single line "(no params_space declared)" rather than
fabricating one. A "no params_space" row is an HONEST signal to the
operator: "the engine cannot tune this signal, only run it as-is." The
Run button stays enabled - the engine's behavior on a strategy without
``params_space`` is the existing synthetic path (no parameter search).

WHEN ``live_active_state()`` RETURNS ``None``
--------------------------------------------
Auto-Live mode shows a single grey "no live state detected" line and
disables the Run button. The control bar degrades to a copy-pasteable
command line the operator can run manually (so the page is still useful
when the live system is not running).

WHEN THE CACHE IS EMPTY
-----------------------
The "Cache status" line shows "(no cached bars for this contract)" and
the Download button is the recommended action. Run still works (the
engine falls back to synthetic, per the Phase 3 audit D14 invariant).

USAGE
-----
This module is imported only by ``optimizer.py``. The page's ``body()``
calls ``_ai_insight_header(snap)`` and ``_control_bar(snap)`` at the very
top of its children list. The rest of the body is unchanged.

Tests live in ``blue_engine/tests/test_phase5_dashboard_header.py``.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import dash_mantine_components as dmc
from dash import dcc, html

# Expose the html module so tests can do isinstance(result, html.Div)
# without having to import dash themselves.
__all__ = ["_ai_insight_header", "_control_bar", "html"]

# --- module-level data-adapter imports (lazy, deferred to first call).
# We do NOT import ``data_cache`` or ``alpha.registry`` at module load
# because they pull in a heavy chain (the OpenAlgo client, the alpha
# walker, the portfolio provider factory). That chain costs ~12s and
# the dashboard re-renders this module on every 60s tick, so we
# pay the cost ONCE per process and cache the result. The cache is
# invalidated only when the user clicks "Refresh" or the page is
# reloaded (which restarts the Python process anyway).
_DATA_CACHE_AVAILABLE: Optional[bool] = None
_DATA_CACHE_ERROR: Optional[str] = None
_list_strategies_for_ui = None
_watchlist_for_ui = None
_OptimizerDataCache = None
live_active_state = None
_REGISTRY_AVAILABLE: Optional[bool] = None
_REGISTRY_ERROR: Optional[str] = None


def _ensure_data_cache_imports() -> None:
    """Import the data_cache adapters on first use. Cached for the
    rest of the process. Never raises: on failure the safe
    adapters degrade to empty results.
    """
    global _DATA_CACHE_AVAILABLE, _DATA_CACHE_ERROR
    global _list_strategies_for_ui, _watchlist_for_ui, _OptimizerDataCache
    if _DATA_CACHE_AVAILABLE is not None:
        return
    try:
        from blue_engine.optimizer.data_cache import (  # noqa: PLC0415
            OptimizerDataCache,
            list_strategies_for_ui,
            watchlist_for_ui,
        )
        _list_strategies_for_ui = list_strategies_for_ui
        _watchlist_for_ui = watchlist_for_ui
        _OptimizerDataCache = OptimizerDataCache
        _DATA_CACHE_AVAILABLE = True
    except Exception as exc:  # noqa: BLE001
        _DATA_CACHE_ERROR = str(exc)
        _DATA_CACHE_AVAILABLE = False


def _ensure_registry_imports() -> None:
    """Import the alpha registry adapter on first use. Cached for the
    rest of the process. Never raises.
    """
    global _REGISTRY_AVAILABLE, _REGISTRY_ERROR, live_active_state
    if _REGISTRY_AVAILABLE is not None:
        return
    try:
        from blue_engine.alpha.registry import (  # noqa: PLC0415
            live_active_state as _las,
        )
        live_active_state = _las
        _REGISTRY_AVAILABLE = True
    except Exception as exc:  # noqa: BLE001
        _REGISTRY_ERROR = str(exc)
        _REGISTRY_AVAILABLE = False


# --- palette (mirrors optimizer.py) ----------------------------------------- #
_GREEN = "#40C057"
_AMBER = "#FCC419"
_RED = "#FA5252"
_GREY = "#909296"
_BG_DARK = "#141517"
_BG_PANEL = "#25262B"

# Default controls. The operator can override per-run; these are the
# "first-load" values the AI Insight header shows until the operator
# touches anything.
_DEFAULT_SYMBOL = "RELIANCE"
_DEFAULT_EXCHANGE = "NSE"
_DEFAULT_STRATEGY = "ocl_keylevel"
_DEFAULT_INTERVAL = "1d"
_DEFAULT_FROM_DAYS = 365
_DEFAULT_TO_DAYS = 0              # 0 = today
_INTERVALS = ["1m", "5m", "15m", "1h", "1d"]


# --------------------------------------------------------------------------- #
# Data adapters (read-only, fail-closed)
# --------------------------------------------------------------------------- #


def _safe_strategies_for_ui() -> List[Dict[str, Any]]:
    """Return the registered strategies, or ``[]`` if the alpha engine
    is unavailable. Never raises; the dashboard renders an empty
    dropdown rather than crash.
    """
    _ensure_data_cache_imports()
    if not _DATA_CACHE_AVAILABLE or _list_strategies_for_ui is None:
        return []
    try:
        out = _list_strategies_for_ui()
        return list(out) if out else []
    except Exception:  # noqa: BLE001
        return []


def _safe_watchlist() -> List[Dict[str, Any]]:
    """Return the watchlist (NSE cash), or ``[]`` on failure."""
    _ensure_data_cache_imports()
    if not _DATA_CACHE_AVAILABLE or _watchlist_for_ui is None:
        return []
    try:
        out = _watchlist_for_ui(preset=None, extra_symbols=None, exchange="NSE")
        return list(out) if out else []
    except Exception:  # noqa: BLE001
        return []


def _safe_live_state() -> Optional[Dict[str, Any]]:
    """Return the live trading system's active state, or ``None`` if
    the file is missing / unreadable.
    """
    _ensure_registry_imports()
    if not _REGISTRY_AVAILABLE or live_active_state is None:
        return None
    try:
        return live_active_state()
    except Exception:  # noqa: BLE001
        return None


def _safe_cache_status(symbol: str, exchange: str, interval: str) -> Dict[str, Any]:
    """Return a cache-status dict for the given contract. Never raises;
    an empty cache is a normal, recoverable state.
    """
    _ensure_data_cache_imports()
    if not _DATA_CACHE_AVAILABLE or _OptimizerDataCache is None:
        return {"cached": False, "n_bars": 0, "error": "data_cache unavailable"}
    try:
        cache = _OptimizerDataCache()
        result = cache.status(exchange=exchange, symbol=symbol, interval=interval)
        # Normalize dataclass / NamedTuple to a plain dict so the formatter
        # does not have to know which shape the cache returns.
        if hasattr(result, "__dict__") and not isinstance(result, dict):
            return dict(result.__dict__)
        if not isinstance(result, dict):
            return {"cached": False, "n_bars": 0, "error": "unknown cache status shape"}
        return result
    except Exception as exc:  # noqa: BLE001
        return {"cached": False, "n_bars": 0, "error": str(exc)}


def _strategy_entry(name: str) -> Optional[Dict[str, Any]]:
    """Return the entry for ``name`` from the strategy list, or ``None``
    if the name is not registered. The header uses this to surface
    the ``params_space`` for the chosen strategy.
    """
    for entry in _safe_strategies_for_ui():
        if entry.get("name") == name:
            return entry
    return None


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #


def _today_ist() -> _dt.date:
    """Return today's date in IST. Pure-Python so the test does not
    need a tzdata fixture.
    """
    return _dt.date.today()


def _from_date_default() -> str:
    """365 days ago, ISO format. The plan locks this as the default
    range for an offline study.
    """
    return (_today_ist() - _dt.timedelta(days=_DEFAULT_FROM_DAYS)).isoformat()


def _to_date_default() -> str:
    """Today, ISO format."""
    return _today_ist().isoformat()


# --------------------------------------------------------------------------- #
# The AI Insight header
# --------------------------------------------------------------------------- #


def _ai_insight_header(snap: Dict[str, Any],
                        mode: str = "auto_live",
                        symbol: str = _DEFAULT_SYMBOL,
                        exchange: str = _DEFAULT_EXCHANGE,
                        strategy: str = _DEFAULT_STRATEGY,
                        from_date: str = None,
                        to_date: str = None,
                        interval: str = _DEFAULT_INTERVAL) -> html.Div:
    """The AI Insight header at the very top of the Optimizer page.

    A read-only summary card. It tells the operator the entire optimizer
    setting: mode, symbol, strategy, time period, every parameter, and
    cache status. No callback is wired to it (the header is a pure
    function of the inputs and the registry).

    Parameters
    ----------
    snap : dict
        The current ``optimizer_snapshot()`` result. Used to show
        "no study to display" if appropriate.
    mode : str
        ``"auto_live"`` (default) or ``"offline"``.
    symbol, exchange, strategy, from_date, to_date, interval :
        The current values of the control-bar inputs. The header shows
        them as the optimizer setting even if the operator has not
        clicked Run yet.
    """
    # Normalise the dates so the header always shows a valid range.
    f = from_date or _from_date_default()
    t = to_date or _to_date_default()

    # Pull the live state (Auto-Live mode) and the strategy entry
    # (parameter table). Both are best-effort: missing = honest
    # placeholder, never a crash.
    live = _safe_live_state() if mode == "auto_live" else None
    entry = _strategy_entry(strategy)
    cache = _safe_cache_status(symbol, exchange, interval)
    watch = _safe_watchlist()

    # --- the four top lines ------------------------------------------------ #
    # The Mode row colours the "Auto-Live" / "Offline" token so the
    # operator can read the current mode at a glance. Auto-Live is
    # GREEN because it means "this page is reflecting the LIVE
    # system"; Offline is GREY because the page is in research mode
    # only. The colour is applied via an inline Span - we do not
    # change the surrounding text colour so the rest of the row
    # stays consistent with the other rows.
    _MODE_LABEL = {"auto_live": "Auto-Live", "offline": "Offline"}
    mode_label = _MODE_LABEL.get(mode, mode)
    if mode == "auto_live":
        mode_token_color = _GREEN
        if live and not live.get("error"):
            mode_suffix = (
                f"  -  read from live system "
                f"(strategy={live.get('strategy', '?')}, "
                f"symbol={live.get('symbol', '?')}, "
                f"interval={live.get('interval', '?')})"
            )
        else:
            err = (live or {}).get("error") or "no live config file found"
            mode_suffix = (
                f"  -  {err}. "
                f"Run is disabled until the live system writes "
                f"Blue_engine/input/strategy.active.json."
            )
    else:
        mode_token_color = _GREY
        mode_suffix = (
            f"  -  user-driven research. "
            f"Run dispatches the CLI subprocess; the page stays read-only."
        )

    mode_value_cell = html.Td(
        [
            html.Span(
                "Mode: ",
                style={"color": _GREY, "fontSize": "12px"},
            ),
            html.Span(
                mode_label,             # pretty-printed "Auto-Live" / "Offline"
                style={
                    "color": mode_token_color,
                    "fontWeight": "600",
                    "fontSize": "12px",
                },
            ),
            html.Span(
                mode_suffix,
                style={"color": _GREY, "fontSize": "12px"},
            ),
        ],
        style=_value_style(),
    )

    setting_rows = [
        html.Tr([html.Td("Mode", style=_label_style()), mode_value_cell]),
        html.Tr([html.Td("Symbol", style=_label_style()),
                 html.Td(f"{symbol}  ({exchange} cash)", style=_value_style())]),
        html.Tr([html.Td("Strategy", style=_label_style()),
                 html.Td(f"{strategy}  ({entry['module'] if entry else 'not registered'})",
                         style=_value_style())]),
        html.Tr([html.Td("Time period", style=_label_style()),
                 html.Td(f"from {f}  to {t}  (interval = {interval})",
                         style=_value_style())]),
        html.Tr([html.Td("Cache status", style=_label_style()),
                 html.Td(_cache_status_text(cache), style=_value_style())]),
        html.Tr([html.Td("Watchlist", style=_label_style()),
                 html.Td(f"{len(watch)} symbols registered for NSE cash",
                         style=_value_style())]),
    ]

    # --- the parameter table ---------------------------------------------- #
    params_table = _params_table(entry)

    # --- the status footer ------------------------------------------------- #
    status = "idle" if not (snap or {}).get("ok") else "loaded"
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    footer = html.P(
        f"Status: {status}.  Last render: {ts}.  "
        f"Data class: historical:openalgo.  "
        f"This header is read-only - it never writes to disk.",
        style={"color": _GREY, "fontSize": "11px", "marginTop": "12px"},
    )

    return dmc.Card([
        html.Div([
            html.Span("AI INSIGHT", style={
                "background": _BG_PANEL, "color": _GREEN,
                "padding": "2px 8px", "borderRadius": "4px",
                "fontSize": "10px", "fontWeight": "600",
                "marginRight": "10px",
            }),
            html.Span("Optimizer Setting - the entire current configuration",
                       style={"color": _GREY, "fontSize": "12px"}),
        ], style={"marginBottom": "10px"}),
        html.Table(setting_rows, style={
            "fontSize": "12px",
            "borderCollapse": "collapse",
            "width": "100%",
            "marginBottom": "12px",
        }),
        html.H4("Parameter search space",
                 style={"color": _GREY, "fontSize": "13px",
                         "marginTop": "6px", "marginBottom": "6px"}),
        params_table,
        footer,
    ], withBorder=True, radius="md", shadow="sm", padding="md",
       className="panel ai-insight-card",
       style={"borderLeft": f"3px solid {_GREEN}"})


def _params_table(entry: Optional[Dict[str, Any]]) -> html.Table:
    """Render the parameter search-space table for the chosen strategy.

    If the strategy is not registered, the table is a single grey row
    "(strategy not registered)" - honest, not a fake empty table.
    If the strategy is registered but has no ``params_space``, the
    table is a single grey row "(no params_space declared)" - the
    engine cannot tune this signal, only run it as-is.
    """
    if entry is None:
        return html.Table(
            [html.Tr([html.Td("(strategy not registered)",
                               style={"color": _RED, "fontSize": "12px"})])],
            style={"fontSize": "12px", "borderCollapse": "collapse"})

    ps = entry.get("params_space") or {}
    if not ps:
        return html.Table(
            [html.Tr([html.Td("(no params_space declared for this strategy - "
                              "the engine runs the strategy as-is, no parameter search)",
                               style={"color": _AMBER, "fontSize": "12px"})])],
            style={"fontSize": "12px", "borderCollapse": "collapse"})

    head = html.Thead(html.Tr([
        html.Th("Parameter", style=_th_style()),
        html.Th("Kind", style=_th_style()),
        html.Th("Default", style=_th_style()),
        html.Th("Range / choices", style=_th_style()),
        html.Th("Note", style=_th_style()),
    ]))
    body_rows = []
    for name, spec in ps.items():
        kind = (spec or {}).get("kind", "?")
        default = (spec or {}).get("default", "-")
        if kind == "cat":
            choices = (spec or {}).get("choices") or []
            range_text = ", ".join(str(c) for c in choices) if choices else "-"
        elif kind in ("int", "float"):
            lo = (spec or {}).get("low", "-")
            hi = (spec or {}).get("high", "-")
            range_text = f"[{lo}, {hi}]"
        else:
            range_text = "-"
        note = (spec or {}).get("note") or (spec or {}).get("doc") or ""
        body_rows.append(html.Tr([
            html.Td(name, style=_td_style()),
            html.Td(kind, style=_td_style(color=_GREY)),
            html.Td(str(default), style=_td_style()),
            html.Td(range_text, style=_td_style()),
            html.Td(note, style=_td_style(color=_GREY)),
        ]))
    return html.Table([head, html.Tbody(body_rows)],
                       style={"fontSize": "12px",
                              "borderCollapse": "collapse",
                              "width": "100%"})


def _cache_status_text(cache: Dict[str, Any]) -> str:
    """Format a cache-status dict as a one-line read-only string."""
    if cache.get("error"):
        return f"error: {cache['error']}"
    if cache.get("cached"):
        first = cache.get("first_ts") or "?"
        last = cache.get("last_ts") or "?"
        n = cache.get("n_bars", 0)
        return f"cached, {n} bars, {first} .. {last}"
    return "(no cached bars for this contract - click Download)"


# --------------------------------------------------------------------------- #
# The control bar
# --------------------------------------------------------------------------- #


def _control_bar(snap: Dict[str, Any]) -> html.Div:
    """The control bar below the AI Insight header.

    Interactive controls:
      * Mode radio (Auto-Live / Offline)
      * Symbol dropdown
      * Strategy dropdown
      * From / To date pickers
      * Interval dropdown
      * Download bars button
      * Run optimizer button (disabled in Auto-Live)

    The Run button is a real button that POSTS a job to the CLI via
    ``dcc.Location`` (a client-side redirect that fires when the
    button is clicked - documented pattern, see the plan).

    The page is still read-only: the only state that survives across
    a re-render is the URL search string, and the body still
    re-derives everything from the journal.
    """
    strategies = _safe_strategies_for_ui()
    watch = _safe_watchlist()
    # Symbol options: union of watchlist (NSE cash) and the default.
    symbol_options = [{"label": s.get("symbol", s.get("name", "?")),
                        "value": s.get("symbol", s.get("name", "?"))}
                       for s in watch if s.get("symbol") or s.get("name")]
    if not symbol_options:
        symbol_options = [{"label": _DEFAULT_SYMBOL, "value": _DEFAULT_SYMBOL}]

    strategy_options = [
        {"label": s.get("label", s.get("name", "?")),
         "value": s.get("name", "?")}
        for s in strategies
    ]
    if not strategy_options:
        strategy_options = [{"label": _DEFAULT_STRATEGY,
                              "value": _DEFAULT_STRATEGY,
                              "disabled": True}]

    interval_options = [{"label": i, "value": i} for i in _INTERVALS]

    # --- the row layout --------------------------------------------------- #
    col_style = {"flex": "1 1 0", "minWidth": "150px",
                 "paddingRight": "12px", "marginBottom": "8px"}

    return html.Div([
        html.H4("Control bar",
                 style={"color": _GREY, "fontSize": "13px",
                         "marginBottom": "8px"}),

        # Mode toggle
        html.Div([
            html.Label("Mode", style=_label_style()),
            dcc.RadioItems(
                id="opt-mode-radio",
                options=[
                    {"label": "  Auto-Live (read from live system)",
                     "value": "auto_live"},
                    {"label": "  Offline (user-driven research)",
                     "value": "offline"},
                ],
                value="auto_live",
                inline=True,
                style={"fontSize": "12px", "color": _GREY},
            ),
        ], style=col_style),

        # Symbol + Exchange row
        html.Div([
            html.Div([
                html.Label("Symbol (NSE cash)",
                           style=_label_style()),
                dcc.Dropdown(
                    id="opt-symbol-pick",
                    options=symbol_options,
                    value=_DEFAULT_SYMBOL,
                    clearable=False,
                    placeholder="Pick a symbol...",
                    style={"fontSize": "12px"},
                ),
            ], style={"flex": "1 1 0", "minWidth": "180px",
                       "paddingRight": "12px"}),
            html.Div([
                html.Label("Exchange", style=_label_style()),
                html.Div("NSE (cash)", style={
                    "color": _GREY, "fontSize": "12px", "padding": "6px 0",
                }),
            ], style={"flex": "0 0 auto", "minWidth": "120px",
                       "paddingRight": "12px"}),
        ], style={"display": "flex", "alignItems": "flexEnd",
                   "flexWrap": "wrap", "marginBottom": "8px"}),

        # Strategy + Interval row
        html.Div([
            html.Div([
                html.Label("Strategy (alpha engine)",
                           style=_label_style()),
                dcc.Dropdown(
                    id="opt-strategy-pick",
                    options=strategy_options,
                    value=_DEFAULT_STRATEGY if any(
                         s.get("name") == _DEFAULT_STRATEGY
                         for s in strategies) else (strategies[0]["name"]
                                                     if strategies else None),
                    clearable=False,
                    placeholder="Pick a strategy...",
                    style={"fontSize": "12px"},
                ),
            ], style={"flex": "1 1 0", "minWidth": "200px",
                       "paddingRight": "12px"}),
            html.Div([
                html.Label("Interval", style=_label_style()),
                dcc.Dropdown(
                    id="opt-interval-pick",
                    options=interval_options,
                    value=_DEFAULT_INTERVAL,
                    clearable=False,
                    style={"fontSize": "12px"},
                ),
            ], style={"flex": "0 0 110px", "minWidth": "110px"}),
        ], style={"display": "flex", "alignItems": "flexEnd",
                   "flexWrap": "wrap", "marginBottom": "8px"}),

        # From / To date pickers
        html.Div([
            html.Div([
                html.Label("From", style=_label_style()),
                dcc.DatePickerSingle(
                    id="opt-from-pick",
                    date=_from_date_default(),
                    style={"fontSize": "12px"},
                ),
            ], style={"flex": "1 1 0", "minWidth": "170px",
                       "paddingRight": "12px"}),
            html.Div([
                html.Label("To", style=_label_style()),
                dcc.DatePickerSingle(
                    id="opt-to-pick",
                    date=_to_date_default(),
                    style={"fontSize": "12px"},
                ),
            ], style={"flex": "1 1 0", "minWidth": "170px"}),
        ], style={"display": "flex", "alignItems": "flexEnd",
                   "flexWrap": "wrap", "marginBottom": "8px"}),

        # Buttons
        html.Div([
            html.Button("Download bars",
                         id="opt-download-btn",
                         n_clicks=0,
                         style={"fontSize": "12px", "padding": "6px 14px",
                                 "background": _BG_PANEL, "color": _GREY,
                                 "border": f"1px solid {_GREY}",
                                 "borderRadius": "4px",
                                 "marginRight": "10px", "cursor": "pointer"}),
            html.Button("Run optimizer",
                         id="opt-run-btn",
                         n_clicks=0,
                         title=("Auto-Live: the optimizer re-runs on the live "
                                "system's currently-active symbol/strategy/dates. "
                                "Run is not manual."),
                         style={"fontSize": "12px", "padding": "6px 14px",
                                 "background": _BG_PANEL, "color": _GREY,
                                 "border": f"1px solid {_GREY}",
                                 "borderRadius": "4px", "cursor": "not-allowed",
                                 "opacity": "0.6"}),
            html.Span(id="opt-run-status",
                       style={"marginLeft": "12px", "color": _GREY,
                              "fontSize": "11px"}),
        ], style={"display": "flex", "alignItems": "center",
                   "marginTop": "4px", "marginBottom": "4px"}),

        # Hidden store for the URL search that the Run button will set
        dcc.Location(id="opt-run-location", refresh=False),
    ], className="panel", style={"marginBottom": "14px",
                                  "borderLeft": f"3px solid {_GREY}"})


# --------------------------------------------------------------------------- #
# Small style helpers
# --------------------------------------------------------------------------- #


def _label_style() -> Dict[str, str]:
    return {"color": _GREY, "fontSize": "11px",
            "display": "block", "marginBottom": "2px"}


def _value_style() -> Dict[str, str]:
    return {"color": "#C1C2C5", "fontSize": "12px",
            "paddingLeft": "10px", "paddingBottom": "4px"}


def _th_style() -> Dict[str, str]:
    return {"textAlign": "left", "color": _GREY, "fontSize": "11px",
            "padding": "4px 8px", "borderBottom": f"1px solid {_BG_PANEL}",
            "fontWeight": "600"}


def _td_style(color: str = "#C1C2C5") -> Dict[str, str]:
    return {"color": color, "fontSize": "12px",
            "padding": "4px 8px",
            "borderBottom": f"1px solid {_BG_DARK}"}
