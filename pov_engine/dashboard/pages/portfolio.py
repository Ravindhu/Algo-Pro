"""
portfolio.py - Page 2: Live OMS/EMS Positions.

Renders open positions, fills and realized/unrealized P&L. Two sources,
merged:
  * a live :class:`~pov_engine.dashboard.app.LiveDashboard` (paper ledger),
    when the Dash UI is hosted in the same process;
  * otherwise, the empty state.
"""
from __future__ import annotations

import time

import dash
from dash import dcc, html

from pov_engine.dashboard.pages import _data

dash.register_page(__name__, path="/portfolio", name="Portfolio", order=1)


def _pnl_color(v: float) -> str:
    if v > 0:
        return "#22c55e"
    if v < 0:
        return "#ef4444"
    return "#94a3b8"


def _positions_table(rows) -> html.Table:
    if not rows:
        return html.Div("No open positions.", className="empty")
    body = []
    for r in rows:
        qty = int(r.get("position") or 0)
        avg = r.get("avg_price")
        upnl = float(r.get("unrealized_pnl") or 0)
        body.append(
            html.Tr(
                [
                    html.Td(f"{r.get('symbol','-')}/{r.get('exchange','-')}", className="mono"),
                    html.Td(r.get("state", "-")),
                    html.Td(_data.fmt(r.get("ltp"))),
                    html.Td(qty),
                    html.Td(_data.fmt(avg)),
                    html.Td(
                        _data.fmt(upnl),
                        style={"color": _pnl_color(upnl), "fontWeight": 600},
                    ),
                ]
            )
        )
    return html.Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th("Instrument"),
                        html.Th("State"),
                        html.Th("LTP"),
                        html.Th("Qty"),
                        html.Th("Avg price"),
                        html.Th("Unrealized P&L"),
                    ]
                )
            ),
            html.Tbody(body),
        ],
        className="data-table",
    )


def _fills_table(trades) -> html.Table:
    if not trades:
        return html.Div("No fills recorded yet.", className="empty")
    body = []
    for t in reversed(trades[-100:]):
        ts = t.get("ts") or 0
        when = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "-"
        side = t.get("side", "-")
        color = "#22c55e" if side == "BUY" else "#ef4444"
        body.append(
            html.Tr(
                [
                    html.Td(when, className="mono"),
                    html.Td(t.get("symbol", "-"), className="mono"),
                    html.Td(side, style={"color": color, "fontWeight": 600}),
                    html.Td(_data.fmt(t.get("qty"))),
                    html.Td(_data.fmt(t.get("price"))),
                    html.Td(t.get("state", "-")),
                    html.Td(_data.fmt(t.get("pov_pct"))),
                ]
            )
        )
    return html.Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th("Time"),
                        html.Th("Symbol"),
                        html.Th("Side"),
                        html.Th("Qty"),
                        html.Th("Price"),
                        html.Th("State"),
                        html.Th("POV %"),
                    ]
                )
            ),
            html.Tbody(body),
        ],
        className="data-table",
    )


def body():
    # Portfolio page content (without the outer wrapper).
    snap = _data.gather()
    rows = snap.positions
    trades = snap.trades

    total_upnl = sum(float(r.get("unrealized_pnl") or 0) for r in rows)
    total_realized = 0.0
    engine = snap.live_engine
    if engine is not None:
        try:
            total_realized = float(engine._positions.total_realized_pnl())  # noqa: SLF001
        except Exception:
            total_realized = 0.0
    open_positions = sum(1 for r in rows if int(r.get("position") or 0) != 0)

    return [
            html.H2("Portfolio - Live OMS / EMS"),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("Open positions", className="kpi-label"),
                            html.Div(str(open_positions), className="kpi-value"),
                        ],
                        className="kpi-card",
                    ),
                    html.Div(
                        [
                            html.Div("Realized P&L", className="kpi-label"),
                            html.Div(
                                _data.fmt(total_realized),
                                className="kpi-value",
                                style={"color": _pnl_color(total_realized)},
                            ),
                        ],
                        className="kpi-card",
                    ),
                    html.Div(
                        [
                            html.Div("Unrealized P&L", className="kpi-label"),
                            html.Div(
                                _data.fmt(total_upnl),
                                className="kpi-value",
                                style={"color": _pnl_color(total_upnl)},
                            ),
                        ],
                        className="kpi-card",
                    ),
                    html.Div(
                        [
                            html.Div("Fills", className="kpi-label"),
                            html.Div(str(len(trades)), className="kpi-value"),
                        ],
                        className="kpi-card",
                    ),
                ],
                className="kpi-row",
            ),
            html.Div([html.H3("Positions"), _positions_table(rows)], className="panel"),
            html.Div([html.H3("Recent fills"), _fills_table(trades)], className="panel"),
    ]


def layout(**_kwargs):
    # Full page: the refreshable body plus its polling Interval.
    return html.Div(
        [
            html.Div(id="portfolio-refresh-out", children=body()),
            dcc.Interval(id="portfolio-refresh", interval=5000, n_intervals=0),
        ],
        className="page",
    )
