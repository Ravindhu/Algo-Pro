"""
terminal.py - Page 1: Main Runtime & Order Entries.

Shows the live runtime state and the *user inputs* that drive entry:
the POV band (buyer-initiated VWAP, ceiling, floor), the current rate,
and the runtime knobs (mode, symbol, quantity, interval, duration).
Read-only.
"""
from __future__ import annotations

import dash
from dash import dcc, html

from pov_engine.dashboard.pages import _data

dash.register_page(__name__, path="/", name="Terminal", order=0)


def _kpi(label: str, value: str, accent: str = "#3b82f6") -> html.Div:
    return html.Div(
        [
            html.Div(label, className="kpi-label"),
            html.Div(value, className="kpi-value", style={"color": accent}),
        ],
        className="kpi-card",
    )


def _input_table(rows, groups) -> html.Table:
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
            html.Tbody(body or [html.Tr(html.Td("no inputs found", colSpan=3))]),
        ],
        className="data-table",
    )


def body():
    # Terminal page content (without the outer wrapper).
    from pov_engine.dashboard.pages.theme import theme
    snap = _data.gather()
    pal = theme(snap.mode)
    pov = snap.pov()

    vwap = float(pov.get("_last_vwap") or 0) if pov.get("_last_vwap") else 0.0
    band = _data.band_from_inputs(snap.config, vwap) if vwap else {"ceiling": 0.0, "floor": 0.0}

    engine = snap.live_engine
    rate_txt = "-"
    reason = "-"
    if engine is not None:
        try:
            rate_txt = f"{engine.config.pov_min_pct:.1f}-{engine.config.pov_max_pct:.1f}%"
            reason = f"{len(engine._machines)} symbols watched"  # noqa: SLF001
        except Exception:
            pass
    return [
            html.H2("Terminal - Runtime & Order Entries"),
            html.Div(
                [
                    _kpi("Buyer VWAP", _data.fmt(vwap or None)),
                    _kpi("Ceiling (+%)", _data.fmt(band.get("ceiling") or None), "#ef4444"),
                    _kpi("Floor (-%)", _data.fmt(band.get("floor") or None), "#22c55e"),
                    _kpi("POV Rate band", rate_txt, "#a855f7"),
                    _kpi("Runtime", reason),
                ],
                className="kpi-row",
            ),
            html.Div(
                [
                    html.H3("Runtime inputs"),
                    _input_table(snap.inputs, {"runtime"}),
                ],
                className="panel",
            ),
            html.Div(
                [
                    html.H3("POV band inputs"),
                    _input_table(snap.inputs, {"pov_band"}),
                ],
                className="panel",
            ),
            html.Div(
                [
                    html.H3("Band visualiser"),
                    dcc.Graph(
                        id="band-graph",
                        figure=_band_figure(vwap, band, pov, pal),
                        config={"displayModeBar": False},
                    ),
                ],
                className="panel",
            ),
    ]


def layout(**_kwargs):
    # Full page: the refreshable body plus its polling Interval.
    return html.Div(
        [
            html.Div(id="terminal-refresh-out", children=body()),
            dcc.Interval(id="terminal-refresh", interval=5000, n_intervals=0),
        ],
        className="page",
    )

def _band_figure(vwap: float, band: dict, pov: dict, pal: dict | None = None):
    """A simple horizontal band diagram (no external state needed)."""
    import plotly.graph_objects as go
    from pov_engine.dashboard.pages.theme import theme
    if pal is None:
        pal = theme("analyzer")
    is_light = pal.get("key") == "live"

    floor = band.get("floor", 0.0)
    ceiling = band.get("ceiling", 0.0)
    min_pov = float(pov.get("min_pov_pct", 5.0))
    max_pov = float(pov.get("max_pov_pct", 25.0))

    fig = go.Figure()
    if vwap > 0 and ceiling > floor:
        fig.add_shape(type="rect", x0=0, x1=1, y0=floor, y1=ceiling,
                      fillcolor="#e2e8f0" if is_light else "#1e293b",
                      line=dict(color="#94a3b8" if is_light else "#334155"),
                      layer="below")
        fig.add_hline(y=vwap, line=dict(color="#3b82f6", width=2),
                      annotation_text="buyer VWAP")
        fig.add_hline(y=ceiling, line=dict(color="#ef4444", dash="dash"),
                      annotation_text=f"ceiling +{band.get('ceiling_pct', 0)}%")
        fig.add_hline(y=floor, line=dict(color="#22c55e", dash="dash"),
                      annotation_text=f"floor -{band.get('floor_pct', 0)}%")
    else:
        fig.add_annotation(text="No buyer-initiated VWAP yet (waiting for first poll)",
                           showarrow=False, font=dict(color=pal["muted_fg"]))
    fig.update_layout(
        template="plotly_white" if is_light else "plotly_dark",
        paper_bgcolor=pal["paper"],
        plot_bgcolor=pal["paper"],
        font=dict(color=pal["body_fg"]),
        height=260,
        margin=dict(l=40, r=40, t=30, b=30),
        yaxis_title="Price",
        xaxis=dict(visible=False, range=[0, 1]),
        annotations=list(fig.layout.annotations) + [
            dict(x=0.5, y=1.08, xref="paper", yref="paper", showarrow=False,
                 text=f"rate lerp({min_pov:g}%, {max_pov:g}%)",
                 font=dict(color=pal["muted_fg"]))
        ],
    )
    return fig
