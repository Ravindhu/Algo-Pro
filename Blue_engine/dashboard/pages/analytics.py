"""
analytics.py - Page 4: Backtesting & Alpha Analytics.

Execution-quality statistics that OpenAlgo's own dashboard does not show:
realized slippage (rolling mean in bps), session VWAP per execution,
participation rate bounds, and the fill-progress curve. Everything is
derived from the ledger + live engine; nothing is recomputed from market
data here (no SDK dependency).

Migrated to the shared DMC kit (``ui.kpi``, ``ui.panel``, ``ui.table``).
Read-only.
"""
from __future__ import annotations

import dash
from dash import dcc, html

from blue_engine.dashboard.pages import _data
from blue_engine.dashboard.pages._ai_insight import (
    _AMBER as _AI_AMBER,
    _GREY as _AI_GREY,
    _GREEN as _AI_GREEN,
    ai_insight_card,
)
from blue_engine.dashboard.pages.theme import theme
from blue_engine.dashboard.ui.kpi import NEGATIVE, POSITIVE, WARNING, KpiCard, KpiRow
from blue_engine.dashboard.ui.panel import Panel
from blue_engine.dashboard.ui.plotly_theme import dashboard_dark_template
from blue_engine.dashboard.ui.table import DataTable

dash.register_page(__name__, path="/analytics", name="Analytics", order=3)


_EXECUTION_COLUMNS = [
    {"key": "execution_id", "label": "Execution", "mono": True},
    {"key": "instrument", "label": "Instrument", "mono": True},
    {"key": "action", "label": "Action"},
    {"key": "vwap", "label": "Session VWAP", "mono": True, "type": "float",
     "align": "right"},
    {"key": "_slip_colour", "label": "Slippage (bps)", "mono": True,
     "type": "float", "align": "right"},
    {"key": "filled", "label": "Filled", "align": "right"},
    {"key": "status", "label": "Status"},
]


def _execution_rows(executions):
    """Shape ledger rows for display, materialising the slip colour."""
    out = []
    for e in executions:
        target = float(e.get("target_qty") or 0)
        filled = float(e.get("filled_qty") or 0)
        pct = (filled / target * 100.0) if target else 0.0
        slip = float(e.get("slippage_bps") or 0.0)
        out.append({
            "execution_id": str(e.get("execution_id", "-"))[:18],
            "instrument": f"{e.get('symbol', '-')}/{e.get('exchange', '-')}",
            "action": e.get("action", "-"),
            "vwap": e.get("vwap"),
            "slippage_bps": slip,
            "_slip_colour": NEGATIVE if slip > 0 else POSITIVE,
            "filled": f"{pct:.1f}%",
            "status": e.get("status", "-"),
        })
    return out


def _slippage_figure(executions, pal=None):
    import plotly.graph_objects as go

    if pal is None:
        pal = theme("analyzer")
    ids = [str(e.get("execution_id", ""))[:10] for e in executions]
    slip = [float(e.get("slippage_bps") or 0.0) for e in executions]
    fig = go.Figure()
    if ids:
        fig.add_trace(
            go.Bar(
                x=ids,
                y=slip,
                marker_color=["#FA5252" if s > 0 else "#40C057" for s in slip],
                name="Slippage (bps)",
            )
        )
    else:
        fig.add_annotation(
            text="No slippage samples yet", showarrow=False,
            font=dict(color=pal["muted_fg"]),
        )
    fig.update_layout(
        # Keep charts transparent and legible against the shared dark shell.
        template=dashboard_dark_template(),
        paper_bgcolor=pal["paper"],
        plot_bgcolor=pal["paper"],
        font=dict(color=pal["body_fg"]),
        height=300,
        margin=dict(l=40, r=20, t=30, b=60),
        yaxis_title="bps",
        xaxis_title="execution",
    )
    return fig


def body():
    """Analytics page content (without the outer wrapper)."""
    snap = _data.gather()
    pal = theme(snap.mode)
    execs = snap.executions
    slips = [float(e.get("slippage_bps") or 0.0) for e in execs]
    mean_slip = sum(slips) / len(slips) if slips else 0.0
    worst = max(slips) if slips else 0.0

    pov = snap.pov()

    # --- AI Insight header (read-only summary card) ------------------------ #
    ai_rows = [
        ("Executions", str(len(execs)), _AI_GREY),
        ("Mean slippage", f"{mean_slip:.2f} bps",
         _AI_GREEN if mean_slip <= 0 else _AI_AMBER),
        ("Worst slippage", f"{worst:.2f} bps",
         _AI_AMBER if worst > 0 else _AI_GREEN),
        ("POV window",
         f"{pov.get('min_pov_pct', 5.0):g}-{pov.get('max_pov_pct', 25.0):g}%",
         _AI_GREY),
        ("Ledger", "available" if snap.ledger_ok else "offline",
         _AI_GREEN if snap.ledger_ok else _AI_GREY),
    ]

    return [
        ai_insight_card(
            "Analytics Setting - execution quality",
            ai_rows,
            note="Every number is read straight from the execution ledger; "
                 "this page recomputes nothing and places no order.",
        ),
        html.H2("Analytics - Execution Quality", style={"marginTop": 0}),
        KpiRow([
            KpiCard("Executions", len(execs), icon="mdi:playlist-play"),
            KpiCard("Mean slippage", _data.fmt(mean_slip),
                    colour=NEGATIVE if mean_slip > 0 else POSITIVE,
                    icon="mdi:chart-timeline-variant", note="bps"),
            KpiCard("Worst slippage", _data.fmt(worst), colour=WARNING,
                    icon="mdi:alert-outline", note="bps"),
            KpiCard("POV window",
                    f"{pov.get('min_pov_pct', 5.0):g}-{pov.get('max_pov_pct', 25.0):g}%",
                    icon="mdi:speedometer"),
        ]),
        Panel("Slippage by execution", dcc.Graph(
            id="slippage-graph", figure=_slippage_figure(execs, pal),
            config={"displayModeBar": False},
        ), icon="mdi:chart-bar"),
        Panel("Execution ledger",
              DataTable(_EXECUTION_COLUMNS, _execution_rows(execs),
                        empty="No executions recorded in the ledger."),
              icon="mdi:format-list-bulleted"),
    ]


def layout(**_kwargs):
    # Full page: the refreshable body plus its polling Interval.
    return html.Div(
        [
            html.Div(id="analytics-refresh-out", children=body()),
            dcc.Interval(id="analytics-refresh", interval=10000, n_intervals=0),
        ],
        className="page",
    )
