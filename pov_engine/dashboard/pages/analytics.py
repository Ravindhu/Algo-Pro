"""
analytics.py - Page 4: Backtesting & Alpha Analytics.

Execution-quality statistics that OpenAlgo's own dashboard does not show:
realized slippage (rolling mean in bps), session VWAP per execution,
participation rate bounds, and the fill-progress curve. Everything is
derived from the ledger + live engine; nothing is recomputed from market
data here (no SDK dependency).
"""
from __future__ import annotations

import dash
from dash import dcc, html

from pov_engine.dashboard.pages import _data

dash.register_page(__name__, path="/analytics", name="Analytics", order=3)


def _execution_table(executions) -> html.Table:
    if not executions:
        return html.Div("No executions recorded in the ledger.", className="empty")
    body = []
    for e in executions:
        target = float(e.get("target_qty") or 0)
        filled = float(e.get("filled_qty") or 0)
        pct = (filled / target * 100.0) if target else 0.0
        slip = float(e.get("slippage_bps") or 0.0)
        body.append(
            html.Tr(
                [
                    html.Td(str(e.get("execution_id", "-"))[:18], className="mono"),
                    html.Td(f"{e.get('symbol','-')}/{e.get('exchange','-')}", className="mono"),
                    html.Td(e.get("action", "-")),
                    html.Td(_data.fmt(e.get("vwap"))),
                    html.Td(
                        _data.fmt(slip),
                        style={"color": "#ef4444" if slip > 0 else "#22c55e"},
                    ),
                    html.Td(f"{pct:.1f}%"),
                    html.Td(e.get("status", "-")),
                ]
            )
        )
    return html.Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th("Execution"),
                        html.Th("Instrument"),
                        html.Th("Action"),
                        html.Th("Session VWAP"),
                        html.Th("Slippage (bps)"),
                        html.Th("Filled"),
                        html.Th("Status"),
                    ]
                )
            ),
            html.Tbody(body),
        ],
        className="data-table",
    )


def _slippage_figure(executions):
    import plotly.graph_objects as go

    ids = [str(e.get("execution_id", ""))[:10] for e in executions]
    slip = [float(e.get("slippage_bps") or 0.0) for e in executions]
    fig = go.Figure()
    if ids:
        fig.add_trace(
            go.Bar(
                x=ids,
                y=slip,
                marker_color=["#ef4444" if s > 0 else "#22c55e" for s in slip],
                name="Slippage (bps)",
            )
        )
    else:
        fig.add_annotation(
            text="No slippage samples yet", showarrow=False, font=dict(color="#94a3b8")
        )
    fig.update_layout(
        template="plotly_dark",
        height=300,
        margin=dict(l=40, r=20, t=30, b=60),
        yaxis_title="bps",
        xaxis_title="execution",
    )
    return fig


def body():
    # Analytics page content (without the outer wrapper).
    snap = _data.gather()
    execs = snap.executions
    slips = [float(e.get("slippage_bps") or 0.0) for e in execs]
    mean_slip = sum(slips) / len(slips) if slips else 0.0
    worst = max(slips) if slips else 0.0

    pov = snap.pov()

    return [
            html.H2("Analytics - Execution Quality"),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("Executions", className="kpi-label"),
                            html.Div(str(len(execs)), className="kpi-value"),
                        ],
                        className="kpi-card",
                    ),
                    html.Div(
                        [
                            html.Div("Mean slippage (bps)", className="kpi-label"),
                            html.Div(
                                _data.fmt(mean_slip),
                                className="kpi-value",
                                style={"color": "#ef4444" if mean_slip > 0 else "#22c55e"},
                            ),
                        ],
                        className="kpi-card",
                    ),
                    html.Div(
                        [
                            html.Div("Worst slippage (bps)", className="kpi-label"),
                            html.Div(
                                _data.fmt(worst),
                                className="kpi-value",
                                style={"color": "#f59e0b"},
                            ),
                        ],
                        className="kpi-card",
                    ),
                    html.Div(
                        [
                            html.Div("POV window", className="kpi-label"),
                            html.Div(
                                f"{pov.get('min_pov_pct', 5.0):g}-{pov.get('max_pov_pct', 25.0):g}%",
                                className="kpi-value",
                            ),
                        ],
                        className="kpi-card",
                    ),
                ],
                className="kpi-row",
            ),
            html.Div(
                [
                    html.H3("Slippage by execution"),
                    dcc.Graph(id="slippage-graph", figure=_slippage_figure(execs),
                              config={"displayModeBar": False}),
                ],
                className="panel",
            ),
            html.Div([html.H3("Execution ledger"), _execution_table(execs)], className="panel"),
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
