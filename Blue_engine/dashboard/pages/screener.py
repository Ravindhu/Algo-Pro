"""
screener.py - Page: Fundamental Screener (market cap, debt/equity, universe).

WHAT THIS PAGE IS FOR
---------------------
The screener engine answers one question: *which Indian names deserve a place in
the tradable universe right now, on fundamentals alone?* This page shows the
answer, the reason behind every rejection, and - critically - the difference
between a name that FAILED a rule and a name whose data could not be measured.

That last distinction is the whole reason the page exists. A screener UI that
shows only "42 selected" hides the fact that 17 names were dropped because their
debt/equity was blank. Those two things mean opposite things to a risk manager.

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


def _verdict_cell(row: Dict[str, Any]) -> str:
    return "SELECTED" if row.get("selected") else "rejected"


def _result_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten the engine payload into table rows, selected names first."""
    rows: List[Dict[str, Any]] = []
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
            "reason": item.get("reason", ""),
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

    rules = _rule_rows(screen)
    n_rules = len(rules)
    excluded = screen.get("exclude_sectors") or []
    source = str((_read_json(_CONFIG_PATH) or {}).get("screener", {}).get("source", "-"))

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
        ("Rules active", str(n_rules), _AI_GREEN if n_rules else _AI_RED),
        ("Candidates", str(n_cand), _AI_GREY),
        ("Selected", str(n_sel), _AI_GREEN if n_sel else _AI_GREY),
        ("Missing data", str(n_missing), _AI_RED if n_missing else _AI_GREEN),
        ("Data as of", as_of, _AI_GREY),
        ("Sector gate", f"{len(excluded)} excluded" if excluded else "off",
         _AI_GREEN if excluded else _AI_RED),
    ]

    panels: List[Any] = [
        ai_insight_card(
            "Screener Setting - fundamental universe",
            ai_rows,
            note="Fundamental snapshots from screener.in. NOT live prices - "
                 "use OpenAlgo quotes() for live valuation.",
        ),
        html.H2("Fundamental Screener", style={"marginTop": 0}),
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
        Panel("Screen rules",
              DataTable(_RULE_COLUMNS, rules,
                        empty="No rules configured.",
                        highlight_on_hover=False),
              icon="mdi:filter-variant"),
    ]

    # The sector gate deserves its own panel: it is the rule most likely to be
    # mistaken for "the screen is broken" when a bank does not appear.
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
