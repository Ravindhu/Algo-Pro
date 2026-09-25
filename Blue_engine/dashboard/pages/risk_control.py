"""
risk_control.py - Page 3: Risk Engine Matrix.

Surfaces the risk inputs plus live guard limits: circuit bands, stop-loss,
quote-failure threshold, the POV rate clamps, and the slippage guard. The
"matrix" is the grid of input x current status, so a user can see at a
glance which guard is active and what its threshold is.
"""
from __future__ import annotations

import dash
from dash import dcc, html

from blue_engine.dashboard.pages import _data

dash.register_page(__name__, path="/risk", name="Risk Control", order=2)


def _status_pill(ok: bool, label: str) -> html.Span:
    return html.Span(
        label,
        className="pill",
        style={
            "background": "#14532d" if ok else "#7f1d1d",
            "color": "#dcfce7" if ok else "#fee2e2",
        },
    )


def _matrix_rows(cfg: dict) -> list:
    """Build the risk matrix from config inputs (best-effort)."""
    pov = cfg.get("pov") if isinstance(cfg.get("pov"), dict) else cfg
    rows = []

    def add(guard, threshold, active, note=""):
        rows.append({"guard": guard, "threshold": threshold, "active": active, "note": note})

    add(
        "VWAP band filter",
        f"on={pov.get('vwap_filter_enabled', True)}",
        bool(pov.get("vwap_filter_enabled", True)),
        f"ceiling +{pov.get('vwap_ceiling_pct', 0.5)}% / floor -{pov.get('vwap_floor_pct', 3.0)}%",
    )
    add(
        "POV rate clamp",
        f"{pov.get('min_pov_pct', 5.0)}% .. {pov.get('max_pov_pct', 25.0)}%",
        True,
        "interpolated rate bounds",
    )
    add(
        "Slippage guard",
        f"{pov.get('max_slippage_bps', 50.0)} bps",
        bool(pov.get("slippage_guard_enabled", True)),
        f"penalty x{pov.get('slippage_penalty_factor', 0.5)}",
    )
    add(
        "Circuit band",
        f"{cfg.get('lower_circuit', '-')} .. {cfg.get('upper_circuit', '-')}",
        bool(cfg.get("upper_circuit") or cfg.get("lower_circuit")),
        "exchange price limits",
    )
    add(
        "Stop loss",
        _data.fmt(cfg.get("stop_loss_price")),
        bool(cfg.get("stop_loss_price")),
        "exit phase trigger",
    )
    add(
        "Ceiling price (LIMIT)",
        _data.fmt(cfg.get("ceiling_price")),
        bool(cfg.get("ceiling_price")),
        "skip chase above",
    )
    add(
        "Quote failure threshold",
        str(cfg.get("quote_failure_threshold", 3)),
        True,
        "pause feed after N misses",
    )
    return rows


def body():
    # Risk page content (without the outer wrapper).
    snap = _data.gather()
    rows = _matrix_rows(snap.config)
    active_count = sum(1 for r in rows if r["active"])

    body = []
    for r in rows:
        body.append(
            html.Tr(
                [
                    html.Td(r["guard"], style={"fontWeight": 600}),
                    html.Td(r["threshold"], className="mono"),
                    html.Td(_status_pill(r["active"], "ACTIVE" if r["active"] else "OFF")),
                    html.Td(r["note"], style={"color": "#94a3b8"}),
                ]
            )
        )

    return [
            html.H2("Risk Control - Engine Matrix"),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("Guards active", className="kpi-label"),
                            html.Div(f"{active_count}/{len(rows)}", className="kpi-value"),
                        ],
                        className="kpi-card",
                    ),
                    html.Div(
                        [
                            html.Div("Executions", className="kpi-label"),
                            html.Div(str(len(snap.executions)), className="kpi-value"),
                        ],
                        className="kpi-card",
                    ),
                    html.Div(
                        [
                            html.Div("Max slippage (bps)", className="kpi-label"),
                            html.Div(
                                _data.fmt(
                                    snap.pov().get("max_slippage_bps", 50.0)
                                ),
                                className="kpi-value",
                                style={"color": "#f59e0b"},
                            ),
                        ],
                        className="kpi-card",
                    ),
                ],
                className="kpi-row",
            ),
            html.Div(
                [
                    html.H3("Guard matrix"),
                    html.Table(
                        [
                            html.Thead(
                                html.Tr(
                                    [
                                        html.Th("Guard"),
                                        html.Th("Threshold"),
                                        html.Th("Status"),
                                        html.Th("Notes"),
                                    ]
                                )
                            ),
                            html.Tbody(body or [html.Tr(html.Td("no guards", colSpan=4))]),
                        ],
                        className="data-table",
                    ),
                ],
                className="panel",
            ),
            html.Div(
                [
                    html.H3("Risk inputs (from config)"),
                    _risk_inputs(snap.inputs, {"risk", "slippage"}),
                ],
                className="panel",
            ),
    ]


def layout(**_kwargs):
    # Full page: the refreshable body plus its polling Interval.
    return html.Div(
        [
            html.Div(id="risk-refresh-out", children=body()),
            dcc.Interval(id="risk-refresh", interval=5000, n_intervals=0),
        ],
        className="page",
    )


def _risk_inputs(rows, groups) -> html.Table:
    body = []
    for r in rows:
        if r.group not in groups:
            continue
        body.append(
            html.Tr(
                [
                    html.Td(r.key, className="mono"),
                    html.Td(_data.fmt(r.value), className="mono"),
                    html.Td(r.group),
                ]
            )
        )
    return html.Table(
        [
            html.Thead(html.Tr([html.Th("Input"), html.Th("Value"), html.Th("Group")])),
            html.Tbody(body or [html.Tr(html.Td("no risk inputs found", colSpan=3))]),
        ],
        className="data-table",
    )
