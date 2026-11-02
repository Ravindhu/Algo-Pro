"""
screener.py - Page: Screener.in saved-screen ingestion and fundamentals.

WHAT THIS PAGE IS FOR
---------------------
For the primary saved-screen source, Screener.in owns stock selection and this
page shows its screened names and fetched fundamentals. The engine does not
apply a second market-cap/debt-equity filter.

READ-ONLY, AND PROUD OF IT
--------------------------
Nothing here places an order, writes the config, or triggers a screen. The page
renders (a) the shipped config, (b) the most recent result artifact if one
exists. Running a sweep is a deliberate act from the CLI/engine, because a
dashboard button that fires hundreds of requests at a third-party site is not a
button this terminal should offer.

DATA HONESTY
------------
Every table is labelled with its data kind. The values are *fundamental
snapshots*, not live prices. The KPI strip says so explicitly rather than hoping
the operator remembers.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import dash
from dash import dcc, html

from blue_engine.dashboard.pages._ai_insight import (
    _GREY as _AI_GREY,
    _GREEN as _AI_GREEN,
    _RED as _AI_RED,
    ai_insight_card,
)
from blue_engine.dashboard.ui import service_control  # noqa: F401
from blue_engine.dashboard.ui.kpi import (
    KpiCard,
    KpiRow,
    NEGATIVE,
    POSITIVE,
    pill,
)
from blue_engine.dashboard.ui.panel import Panel
from blue_engine.dashboard.ui.table import DataTable

dash.register_page(__name__, path="/screener", name="Screener", order=9)

#: Config shipped with the engine. Read-only here.
_CONFIG_PATH = (Path(__file__).resolve().parents[2] / "input" / "screener_engine.json")

#: Where a screen result is dropped for the dashboard to read. The engine writes
#: it when run with ``--save``; its absence is a normal state ("never run yet"),
#: not an error, so the page renders an empty state rather than a failure.
_RESULT_PATH = (Path(__file__).resolve().parents[2] / "screener" / "last_screen.json")

#: Run report written by ``scripts/run_screener_bridge.py --report``. Bridge
#: state only - it is a snapshot persisted at exit, not a live feed.
_BRIDGE_REPORT_PATH = (Path(__file__).resolve().parents[3]
                       / "data" / "screener_bridge_report.json")

#: Protective-level mirror written by ``core/protective_orders.py`` as the POV
#: slicer registers each execution's SL/TP. Refreshed on every register/update.
_PROTECTIVE_PATH = (Path(__file__).resolve().parents[3]
                    / "data" / "protective_orders.json")


# --------------------------------------------------------------------------- #
# Readers (never raise)
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """Read a JSON object, returning None on ANY problem.

    Deliberately broad: this is a dashboard reader on a file another process
    writes. A half-written file, a locked file, or a stale schema must degrade
    to "no data", never to a blank page with a traceback in the log.
    """
    try:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - see docstring
        return None


def _read_json_file(path: Path) -> Any:
    """Read a JSON *value* (object or array) or None on any problem.

    ``protective_orders.json`` is a dict keyed by ``execution_id|symbol``,
    while the bridge report is a plain object; both must degrade to None.
    """
    try:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return None
        return json.loads(raw)
    except Exception:  # noqa: BLE001 - see docstring
        return None


def _config_screen() -> Dict[str, Any]:
    cfg = _read_json(_CONFIG_PATH) or {}
    return ((cfg.get("screener") or {}).get("screen") or {})


def _rule_rows(screen: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for r in (screen.get("rules") or []):
        rows.append({
            "metric": str(r.get("metric", "")),
            "condition": f"{r.get('op', '')} {r.get('threshold', '')}".strip(),
            "on_missing": str(r.get("on_missing", "fail")),
        })
    return rows


# --------------------------------------------------------------------------- #
# Table renderers
# --------------------------------------------------------------------------- #
def _render_outcome(_value, row):
    """Three-valued badge: pass / fail / no-data.

    The middle state is rendered as an explicit amber-ish "NO DATA" rather
    than being folded into FAIL, because "we could not measure it" and "it
    breached the limit" lead to different follow-up actions.
    """
    outcome = str(row.get("outcome", ""))
    if outcome == "pass":
        return pill("PASS", ok=True)
    if outcome == "unknown":
        return pill("NO DATA", ok=None)
    return pill("FAIL", ok=False)


_RESULT_COLUMNS = [
    {"key": "symbol", "label": "Symbol", "bold": True},
    {"key": "verdict", "label": "Verdict"},
    {"key": "market_cap", "label": "Market Cap (Cr)", "mono": True},
    {"key": "debt_equity", "label": "Debt/Equity", "mono": True},
    {"key": "pe", "label": "P/E", "mono": True},
    {"key": "roe", "label": "ROE %", "mono": True},
    {"key": "reason", "label": "Reason", "mono": True},
]

_RULE_COLUMNS = [
    {"key": "metric", "label": "Metric", "bold": True},
    {"key": "condition", "label": "Condition", "mono": True},
    {"key": "on_missing", "label": "If missing"},
]


def _fmt(value: Any) -> str:
    """Format a metric for display, keeping 'missing' visually distinct."""
    if value is None or value == "":
        return "-"
    try:
        num = float(value)
        return f"{num:,.2f}"
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------- #
# Bridge status (run report + protective-level mirror)
# --------------------------------------------------------------------------- #
def _bridge_summary() -> Dict[str, Any]:
    """Merge the bridge report and the protective registry into primitives.

    Pure read: no writes, no callbacks, no engine import. A missing or
    half-written file yields zeros/empty lists, never an exception.
    """
    report = _read_json(_BRIDGE_REPORT_PATH)
    if not isinstance(report, dict):
        report = None
    levels_raw = _read_json_file(_PROTECTIVE_PATH)
    levels = list(levels_raw.values()) if isinstance(levels_raw, dict) else []

    executed = list((report or {}).get("executed") or [])
    disabled = dict((report or {}).get("disabled") or {})
    open_positions = dict((report or {}).get("open_positions") or {})
    blocks = [e for e in executed
              if str(e.get("status")) == "risk_gate_block"]
    errors = [e for e in executed
              if str(e.get("status", "")).startswith(("slicer error",
                                                       "risk_gate_block"))]
    return {
        "report": report,
        "as_of": str((report or {}).get("as_of") or "-"),
        "n_symbols": len((report or {}).get("symbols") or []),
        "n_active": len((report or {}).get("active") or []),
        "disabled": disabled,
        "open_positions": open_positions,
        "n_executed": len(executed),
        "n_blocked": len(blocks),
        "n_errors": len(errors),
        "levels": levels,
        "executed": executed,
        "errors": errors,
    }


def _level_rows(levels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for rec in levels:
        rows.append({
            "symbol": str(rec.get("symbol", "")),
            "exchange": str(rec.get("exchange", "")),
            "strategy": str(rec.get("strategy", "")),
            "exit_mode": str(rec.get("exit_mode") or "-"),
            "stop_loss": _fmt(rec.get("stop_loss")),
            "target": _fmt(rec.get("target")),
            "htf_stop": _fmt(rec.get("htf_stop_loss")),
            "updated": str(rec.get("updated_at") or rec.get("registered_at") or "-"),
        })
    rows.sort(key=lambda r: (r["symbol"], r["exchange"]))
    return rows


def _exec_row_category(row: Dict[str, Any]) -> str:
    """Plain category, so the reader does not have to parse the status string."""
    status = str(row.get("status", ""))
    if status == "closed":
        return "exit"
    if status.startswith("risk_gate_block"):
        return "risk gate block"
    if status.startswith("slicer error"):
        return "slicer error"
    return status or "-"


def _exec_rows(executed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for e in executed:
        summary = e.get("summary") or {}
        intent = e.get("intent") or {}
        rows.append({
            "ts": str(e.get("ts") or "-"),
            "symbol": str(e.get("symbol", "")),
            "exchange": str(e.get("exchange", "")),
            "category": _exec_row_category(e),
            "status": str(e.get("status", "-")),
            "action": str(intent.get("action", "-")),
            "quantity": _fmt(intent.get("quantity")),
            "filled": _fmt(summary.get("filled")),
            "sl": _fmt(intent.get("stop_loss")),
            "tp": _fmt(intent.get("target")),
        })
    return rows[-100:][::-1]     # newest first, capped


_PROTECTIVE_COLUMNS = [
    {"key": "symbol", "label": "Symbol", "bold": True},
    {"key": "exchange", "label": "Exch"},
    {"key": "strategy", "label": "Strategy", "mono": True},
    {"key": "exit_mode", "label": "Exit mode"},
    {"key": "stop_loss", "label": "Stop loss", "mono": True},
    {"key": "target", "label": "Target", "mono": True},
    {"key": "htf_stop", "label": "HTF stop", "mono": True},
    {"key": "updated", "label": "Updated", "mono": True},
]

_EXEC_COLUMNS = [
    {"key": "ts", "label": "Time", "mono": True},
    {"key": "symbol", "label": "Symbol", "bold": True},
    {"key": "exchange", "label": "Exch"},
    {"key": "category", "label": "Category"},
    {"key": "action", "label": "Side"},
    {"key": "quantity", "label": "Qty", "mono": True},
    {"key": "filled", "label": "Filled", "mono": True},
    {"key": "sl", "label": "SL", "mono": True},
    {"key": "tp", "label": "TP", "mono": True},
]


def _verdict_cell(row: Dict[str, Any]) -> str:
    return "SELECTED" if row.get("selected") else "rejected"


def _result_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten the engine payload into table rows, selected names first."""
    rows: List[Dict[str, Any]] = []
    from_saved_screen = bool(
        (result.get("screen") or {}).get("rules_applied_upstream"))
    for item in (result.get("results") or []):
        metrics = item.get("metrics") or {}
        rows.append({
            "symbol": item.get("symbol", ""),
            "selected": bool(item.get("selected")),
            "verdict": _verdict_cell(item),
            "market_cap": _fmt(metrics.get("market_cap")),
            "debt_equity": _fmt(metrics.get("debt_equity")),
            "pe": _fmt(metrics.get("pe")),
            "roe": _fmt(metrics.get("roe")),
            "reason": (
                "selected by Screener.in saved screen"
                if from_saved_screen and item.get("selected")
                else item.get("reason", "")),
        })
    # Selected first, then alphabetical - so the interesting rows are on top
    # without the ordering depending on the source's row order.
    rows.sort(key=lambda r: (not r["selected"], r["symbol"]))
    return rows


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
def body():
    """Screener page content (without the outer wrapper)."""
    screen = _config_screen()
    result = _read_json(_RESULT_PATH)

    source = str((_read_json(_CONFIG_PATH) or {}).get("screener", {}).get("source", "-"))
    upstream_screen = source.lower() == "screen"
    rules = [] if upstream_screen else _rule_rows(screen)
    n_rules = len(rules)
    excluded = [] if upstream_screen else (screen.get("exclude_sectors") or [])

    if result:
        counts = result.get("counts") or {}
        n_cand = int(counts.get("candidates") or 0)
        n_sel = int(counts.get("selected") or 0)
        n_missing = int(counts.get("missing_data") or 0)
        as_of = str(result.get("as_of") or "-")
        src_desc = str(result.get("source") or "-")
        rows = _result_rows(result)
        warnings = list(result.get("warnings") or [])
        derived = result.get("metrics_derived") or {}
    else:
        n_cand = n_sel = n_missing = 0
        as_of = "never run"
        src_desc = source
        rows = []
        warnings = []
        derived = {}

    ai_rows = [
        ("Selection source",
         "Screener.in" if upstream_screen else f"{n_rules} local rules",
         _AI_GREEN if upstream_screen or n_rules else _AI_RED),
        ("Candidates", str(n_cand), _AI_GREY),
        ("Selected", str(n_sel), _AI_GREEN if n_sel else _AI_GREY),
        ("Missing data", str(n_missing), _AI_RED if n_missing else _AI_GREEN),
        ("Data as of", as_of, _AI_GREY),
    ]

    bridge = _bridge_summary()
    ai_rows += [
        ("Bridge open positions", str(len(bridge["open_positions"])),
         _AI_GREEN if bridge["open_positions"] else _AI_GREY),
        ("Bridge blocked / errors",
         f"{bridge['n_blocked']} / {bridge['n_errors']}",
         _AI_RED if bridge["n_errors"] else _AI_GREEN),
    ]

    panels: List[Any] = [
        ai_insight_card(
            ("Screener.in saved-screen universe" if upstream_screen
             else "Screener Setting - fundamental universe"),
            ai_rows,
            note=(
                "Screener.in applies selection. Blue_engine fetches the screened "
                "companies and statements. NOT live prices - use OpenAlgo quotes()."
                if upstream_screen else
                "Fundamental snapshots from screener.in. NOT live prices - "
                "use OpenAlgo quotes() for live valuation."),
        ),
        html.H2("Screener.in Saved Screen" if upstream_screen
                else "Fundamental Screener", style={"marginTop": 0}),
        KpiRow([
            KpiCard("Candidates", n_cand, icon="mdi:database-search-outline"),
            KpiCard("Selected", n_sel,
                    colour=POSITIVE if n_sel else None,
                    icon="mdi:filter-check-outline"),
            KpiCard("Rejected", max(0, n_cand - n_sel), icon="mdi:filter-remove-outline"),
            KpiCard("Missing data", n_missing,
                    colour=NEGATIVE if n_missing else None,
                    icon="mdi:help-circle-outline"),
            KpiCard("Source", src_desc, icon="mdi:source-branch"),
        ]),
        Panel(
            "Selection rules" if upstream_screen else "Screen rules",
            html.P(
                "Managed and applied in the configured Screener.in saved screen."
                if upstream_screen else "Configured local rules.",
                style={"opacity": 0.8},
            ) if upstream_screen else DataTable(
                _RULE_COLUMNS, rules, empty="No rules configured.",
                highlight_on_hover=False),
            icon="mdi:filter-variant"),
    ]

    if excluded:
        panels.append(Panel(
            "Sector gate (applied before the debt rule)",
            html.Div([
                html.P(
                    "debt/equity is not a solvency measure for lenders - their "
                    "borrowings ARE their inventory. These sectors are excluded.",
                    style={"opacity": 0.8, "marginTop": 0}),
                html.Div(" ".join(str(s) for s in excluded),
                         style={"fontFamily": "monospace"}),
            ]),
            icon="mdi:bank-off-outline"))

    if derived:
        panels.append(Panel(
            "Derived metrics",
            html.Div([
                html.P(
                    "These values are computed by the engine, not scraped "
                    "verbatim. A vendor's number may differ.",
                    style={"opacity": 0.8, "marginTop": 0}),
                html.Ul([html.Li(f"{k}: {v}") for k, v in derived.items()],
                        style={"fontFamily": "monospace", "fontSize": 12}),
            ]),
            icon="mdi:calculator-variant-outline"))

    panels.append(Panel(
        "Screen results",
        DataTable(_RESULT_COLUMNS, rows,
                  empty="No screen has been run yet. Run "
                        "`python -m blue_engine.screener.screener_engine` "
                        "or use the engine API to produce a result.",
                  highlight_on_hover=True),
        icon="mdi:table-search"))

    if warnings:
        panels.append(Panel(
            "Warnings from the last run",
            html.Ul([html.Li(w) for w in warnings],
                    style={"fontFamily": "monospace", "fontSize": 12}),
            icon="mdi:alert-outline"))

    panels.extend(_bridge_panels(bridge))
    return panels


def _bridge_panels(bridge: Dict[str, Any]) -> List[Any]:
    """Read-only bridge tiles: run state, protective levels, execution log.

    Data honesty: the run report is a snapshot written when the bridge exits,
    the protective mirror is written by the POV slicer as levels are
    registered. Neither is a live order feed - the OpenAlgo order book is.
    """
    report = bridge["report"]
    if report is None:
        return [Panel(
            "Screener - OCL - POV bridge",
            html.Div([
                html.P(
                    "No bridge run report yet. Start the bridge from the CLI: ",
                    style={"opacity": 0.8, "marginTop": 0}),
                html.Code(
                    "scripts/run_screener_bridge.py --csv <export.csv> "
                    "--max-active 5 --max-concurrent 1",
                    style={"fontSize": 12}),
            ]),
            icon="mdi:source-branch")]

    disabled_rows = [
        {"symbol": key.split("|")[0],"exchange": (key.split("|") + [""])[1],
         "reason": str(reason)}
        for key, reason in sorted(bridge["disabled"].items())
    ]
    open_rows = [
        {"symbol": key.split("|")[0],"exchange": (key.split("|") + [""])[1],
         "quantity": _fmt(qty)}
        for key, qty in sorted(bridge["open_positions"].items())
    ]

    kpis = [
        KpiCard("Symbols in run", bridge["n_symbols"], icon="mdi:format-list-numbered"),
        KpiCard("Active workers", bridge["n_active"], icon="mdi:play-circle-outline"),
        KpiCard("Executions", bridge["n_executed"], icon="mdi:swap-horizontal"),
        KpiCard("Risk-gate blocks", bridge["n_blocked"],
                colour=NEGATIVE if bridge["n_blocked"] else None,
                icon="mdi:shield-alert-outline"),
        KpiCard("Disabled symbols", len(bridge["disabled"]),
                colour=NEGATIVE if bridge["disabled"] else None,
                icon="mdi:alert-octagon-outline"),
    ]

    panels: List[Any] = [
        html.H2("Bridge - screener to execution", style={"marginTop": 20}),
        html.P(
            f"Report snapshot as of {bridge['as_of']}. State is persisted at "
            "bridge exit; protective levels below are the live slicer "
            "registry. Verify fills in the OpenAlgo order book.",
            style={"opacity": 0.75, "marginTop": 0}),
        KpiRow(kpis),
        Panel("Run state", DataTable(
            [{"key": "symbol", "label": "Symbol", "bold": True},
             {"key": "exchange", "label": "Exch"},
             {"key": "quantity", "label": "Tracked qty", "mono": True}],
            open_rows, empty="No tracked open positions in the last run.",
            highlight_on_hover=False),
            icon="mdi:briefcase-outline"),
    ]

    if disabled_rows:
        panels.append(Panel("Disabled symbols (fail-closed)", DataTable(
            [{"key": "symbol", "label": "Symbol", "bold": True},
             {"key": "exchange", "label": "Exch"},
             {"key": "reason", "label": "Reason", "mono": True}],
            disabled_rows, highlight_on_hover=False),
            icon="mdi:alert-octagon-outline"))

    panels.append(Panel(
        "Protective levels (live SL/TP registry)",
        DataTable(_PROTECTIVE_COLUMNS, _level_rows(bridge["levels"]),
                  empty="No protective levels registered in this process. "
                        "The mirror file is written by the POV slicer; an "
                        "empty registry is normal when nothing is filled.",
                  highlight_on_hover=True),
        icon="mdi:shield-check-outline"))

    panels.append(Panel(
        "Execution log (last 100)",
        DataTable(_EXEC_COLUMNS, _exec_rows(bridge["executed"]),
                  empty="No executions recorded in the last bridge run.",
                  highlight_on_hover=True),
        icon="mdi:history"))
    return panels


def layout(**_kwargs):
    """Full page: the refreshable body plus its polling Interval."""
    return html.Div(
        [
            html.Div(id="screener-refresh-out", children=body()),
            dcc.Interval(id="screener-refresh", interval=15000, n_intervals=0),
        ],
        className="page",
    )
