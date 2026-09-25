"""
economic.py - Page 6: Economic (macro regime) engine.

The macro stage of the six-engine pipeline, rendered as a read-only terminal
page. It answers, visually, the one question the Economic engine exists to
answer:

    *What macro regime are we in, and how much gross risk should we carry?*

Charts (all Plotly, dark template, no external data - read from the persisted
run history and the macro config only):

  1. Regime gauge            - composite risk-on score in [-1, +1], banded
  2. Gross-exposure gauge    - the multiplier the Portfolio engine consumes
  3. Signal score bar        - every signal's signed contribution
  4. Signal radar            - the same, as a polar shape (one glance)
  5. Risk-on history line    - the composite path over time (from run history)
  6. Regime timeline         - categorical regime colour-band over time
  7. Multiplier history line - how the gross tilt moved
  8. Model posterior bar     - P(RISK_ON)/NEUTRAL/OFF when a model is active
  9. Event-risk panel        - scheduled event risk (uncertainty, not direction)

Every builder is defensive: missing data renders an annotated empty chart, not
an exception. The page never places an order and never touches a broker.

Data source
-----------
``blue_engine.economics.run_history`` (``data/economic_runs/YYYY-MM-DD.jsonl``),
written by the runner when ``engines.economic.persist = true``. If no history
exists yet, the page shows an instructive empty state.
"""
from __future__ import annotations

import dash
from dash import dcc, html

from blue_engine.dashboard.pages import _data

dash.register_page(__name__, path="/economic", name="Economic", order=5)


# --------------------------------------------------------------------------- #
# Palette / helpers
# --------------------------------------------------------------------------- #
_GREEN = "#22c55e"
_RED = "#ef4444"
_AMBER = "#f59e0b"
_SLATE = "#94a3b8"
_ACCENT = "#38bdf8"

# Band cut-points mirror blue_engine.economics.regime.REGIME_BANDS.
_REGIME_COLORS = {
    "RISK_ON": _GREEN,
    "MILD_RISK_ON": "#86efac",
    "NEUTRAL": _SLATE,
    "MILD_RISK_OFF": "#fca5a5",
    "RISK_OFF": _RED,
    "UNKNOWN": "#64748b",
}


def _score_color(score: float) -> str:
    if score >= 0.10:
        return _GREEN
    if score <= -0.10:
        return _RED
    return _SLATE


def _empty_fig(message: str, height: int = 300):
    """An annotated empty Plotly figure (never a blank panel).

    The message is wrapped to a fixed character width and softened, so a long
    hint (e.g. the regime-model instructions) never overflows the panel.
    """
    import plotly.graph_objects as go

    # Soft-wrap on word boundaries so the annotation stays inside the panel.
    wrapped = _wrap(message, width=46)
    fig = go.Figure()
    fig.add_annotation(text=wrapped, showarrow=False, align="center",
                       font=dict(color=_SLATE, size=13))
    fig.update_layout(template="plotly_dark", height=height,
                      margin=dict(l=40, r=40, t=30, b=40),
                      xaxis=dict(visible=False), yaxis=dict(visible=False))
    return fig


def _wrap(text: str, width: int = 46) -> str:
    """Word-wrap plain text to ``width`` chars, joined with Plotly <br>."""
    words = str(text).split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "<br>".join(lines)
    return fig


# --------------------------------------------------------------------------- #
# 1. Regime gauge (the headline number)
# --------------------------------------------------------------------------- #
def _regime_gauge(score: float, regime: str):
    import plotly.graph_objects as go

    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        number={"valueformat": "+.2f", "font": {"size": 34}},
        title={"text": f"Risk-on score<br><span style='font-size:14px;"
                       f"color:{_REGIME_COLORS.get(regime, _SLATE)}'>"
                       f"{regime}</span>"},
        gauge={
            "axis": {"range": [-1, 1], "tickvals": [-1, -0.35, -0.10,
                                                    0.10, 0.35, 1]},
            "bar": {"color": _score_color(score)},
            "steps": [
                {"range": [-1.0, -0.35], "color": "#3f1d1d"},
                {"range": [-0.35, -0.10], "color": "#3b2a1a"},
                {"range": [-0.10, 0.10], "color": "#2a2f3a"},
                {"range": [0.10, 0.35], "color": "#1f3b2a"},
                {"range": [0.35, 1.0], "color": "#14532d"},
            ],
            "threshold": {"line": {"color": "white", "width": 2},
                          "value": score},
        },
    ))
    fig.update_layout(template="plotly_dark", height=280,
                      margin=dict(l=30, r=30, t=50, b=20))
    return fig


# --------------------------------------------------------------------------- #
# 2. Gross-exposure gauge
# --------------------------------------------------------------------------- #
def _multiplier_gauge(mult: float, path: str):
    import plotly.graph_objects as go

    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=mult,
        number={"valueformat": ".2f", "suffix": "x", "font": {"size": 34}},
        title={"text": f"Gross-exposure multiplier<br>"
                       f"<span style='font-size:13px;color:{_SLATE}'>"
                       f"path: {path}</span>"},
        gauge={
            "axis": {"range": [0.25, 1.5], "tickvals": [0.25, 0.5, 1.0,
                                                        1.25, 1.5]},
            "bar": {"color": _ACCENT},
            "steps": [
                {"range": [0.25, 0.5], "color": "#3f1d1d"},
                {"range": [0.5, 0.75], "color": "#3b2a1a"},
                {"range": [0.75, 1.0], "color": "#2a2f3a"},
                {"range": [1.0, 1.25], "color": "#1f3b2a"},
                {"range": [1.25, 1.5], "color": "#14532d"},
            ],
            "threshold": {"line": {"color": "white", "width": 2},
                          "value": 1.0},
        },
    ))
    fig.update_layout(template="plotly_dark", height=280,
                      margin=dict(l=30, r=30, t=50, b=20))
    return fig


# --------------------------------------------------------------------------- #
# 3. Signal score bar
# --------------------------------------------------------------------------- #
def _signal_bar(signals: dict):
    import plotly.graph_objects as go

    if not signals:
        return _empty_fig("No signal contributions in the latest run.")
    names = list(signals.keys())
    scores = [float((signals[n] or {}).get("score") or 0.0) for n in names]
    labels = [(signals[n] or {}).get("label", n) for n in names]
    fig = go.Figure(go.Bar(
        x=scores, y=labels, orientation="h",
        marker_color=[_score_color(s) for s in scores],
        text=[f"{s:+.2f}" for s in scores], textposition="outside",
        customdata=[[(signals[n] or {}).get("weight"),
                     (signals[n] or {}).get("views_used")] for n in names],
        hovertemplate="%{y}<br>score=%{x:+.3f}<br>weight=%{customdata[0]}"
                      "<br>views=%{customdata[1]}<extra></extra>",
    ))
    fig.add_vline(x=0, line_color=_SLATE, line_width=1)
    fig.update_layout(template="plotly_dark", height=max(260, 34 * len(names)),
                      margin=dict(l=10, r=40, t=30, b=30),
                      xaxis=dict(range=[-1.05, 1.05], title="signed contribution"))
    return fig


# --------------------------------------------------------------------------- #
# 4. Signal radar
# --------------------------------------------------------------------------- #
def _signal_radar(signals: dict):
    import plotly.graph_objects as go

    if not signals:
        return _empty_fig("No signals to plot.")
    labels = [(signals[n] or {}).get("label", n) for n in signals]
    scores = [float((signals[n] or {}).get("score") or 0.0)
              for n in signals]
    # Close the loop for a filled radar shape.
    theta = labels + labels[:1]
    r = scores + scores[:1]
    fig = go.Figure(go.Scatterpolar(r=r, theta=theta, fill="toself",
                                    line_color=_ACCENT,
                                    name="signed score"))
    fig.update_layout(template="plotly_dark", height=360,
                      polar=dict(radialaxis=dict(visible=True,
                                                 range=[-1, 1])),
                      margin=dict(l=40, r=40, t=40, b=30),
                      showlegend=False)
    return fig


# --------------------------------------------------------------------------- #
# 5/6/7. History charts
# --------------------------------------------------------------------------- #
def _x_axis(history: list) -> dict:
    """Shared, crowding-proof x-axis config for the history charts.

    We plot on a REAL datetime axis (not string categories) so ticks are placed
    by time, then control density explicitly:

      * ``tickformat`` shows HH:MM only (short labels never overlap),
      * ``nticks`` caps how many ticks Plotly may draw,
      * ``tickangle`` tilts the last resort,
      * the full date+time is still available in the hover via ``hoverformat``.
    """
    return dict(
        type="date",
        tickformat="%H:%M",       # short - no overlap
        hoverformat="%d-%b %H:%M",
        nticks=8,                  # cap tick count on a crowded axis
        tickangle=-30,
        automargin=True,
        title="run time (HH:MM)",
    )


def _datetimes(history: list) -> list:
    """Parse the run timestamps into a real datetime list (tz-agnostic local)."""
    from datetime import datetime
    out = []
    for h in history:
        try:
            out.append(datetime.fromtimestamp(float(h.get("ts") or 0)))
        except (TypeError, ValueError, OSError):
            out.append(None)
    return out


def _history_line(history: list):
    import plotly.graph_objects as go

    if not history:
        return _empty_fig("No run history yet - enable "
                          "engines.economic.persist = true and run the engine.")
    xs = _datetimes(history)
    ys = [float(h.get("risk_on_score") or 0.0) for h in history]
    regimes = [h.get("regime", "UNKNOWN") for h in history]
    fig = go.Figure(go.Scatter(
        x=xs, y=ys, mode="lines+markers",
        line=dict(color=_ACCENT, width=2), marker=dict(size=5),
        customdata=[[r] for r in regimes],
        hovertemplate="%{x|%d-%b %H:%M}<br>score %{y:+.3f}"
                      "<br>%{customdata[0]}<extra></extra>",
    ))
    for level, color in ((0.35, "#14532d"), (0.10, "#1f3b2a"),
                         (-0.10, "#3b2a1a"), (-0.35, "#3f1d1d")):
        fig.add_hline(y=level, line_color=color, line_dash="dot")
    fig.update_layout(template="plotly_dark", height=300,
                      margin=dict(l=50, r=20, t=30, b=50),
                      yaxis=dict(range=[-1, 1], title="risk-on score"),
                      xaxis=_x_axis(history))
    return fig


def _regime_timeline(history: list):
    import plotly.graph_objects as go

    if not history:
        return _empty_fig("No regime history yet.")
    xs = _datetimes(history)
    regimes = [h.get("regime", "UNKNOWN") for h in history]
    fig = go.Figure(go.Bar(
        x=xs, y=[1] * len(xs),
        marker_color=[_REGIME_COLORS.get(r, _SLATE) for r in regimes],
        customdata=[[r] for r in regimes],
        hovertemplate="%{x|%d-%b %H:%M}<br>%{customdata[0]}<extra></extra>",
    ))
    fig.update_layout(template="plotly_dark", height=160, barmode="overlay",
                      margin=dict(l=50, r=20, t=20, b=50),
                      yaxis=dict(visible=False, range=[0, 1]),
                      xaxis=_x_axis(history))
    return fig


def _multiplier_history(history: list):
    import plotly.graph_objects as go

    if not history:
        return _empty_fig("No multiplier history yet.")
    xs = _datetimes(history)
    ys = [float(h.get("gross_exposure_multiplier") or 1.0) for h in history]
    fig = go.Figure(go.Scatter(
        x=xs, y=ys, mode="lines+markers", fill="tozeroy",
        line=dict(color=_AMBER, width=2), marker=dict(size=5),
        fillcolor="rgba(245,158,11,0.15)",
        hovertemplate="%{x|%d-%b %H:%M}<br>gross %{y:.2f}x<extra></extra>",
    ))
    fig.add_hline(y=1.0, line_color=_SLATE, line_dash="dash",
                  annotation_text="neutral 1.0x")
    fig.update_layout(template="plotly_dark", height=280,
                      margin=dict(l=50, r=20, t=30, b=50),
                      yaxis=dict(range=[0, 1.6], title="gross x"),
                      xaxis=_x_axis(history))
    return fig


# --------------------------------------------------------------------------- #
# 8. Model posterior
# --------------------------------------------------------------------------- #
def _posterior_bar(model: dict):
    import plotly.graph_objects as go

    probs = (model or {}).get("probabilities") or {}
    if not probs:
        return _empty_fig("No regime model active "
                          "(set regime_model = 'hmm' and model_weight > 0).")
    labels = list(probs.keys())
    vals = [float(probs[k]) for k in labels]
    fig = go.Figure(go.Bar(
        x=labels, y=vals,
        marker_color=[_REGIME_COLORS.get(k, _SLATE) for k in labels],
        text=[f"{v:.0%}" for v in vals], textposition="outside",
    ))
    fig.update_layout(template="plotly_dark", height=280,
                      margin=dict(l=40, r=20, t=30, b=40),
                      yaxis=dict(range=[0, 1.15], title="probability"),
                      xaxis_title="regime state")
    return fig


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
def _signal_table(signals: dict) -> html.Table:
    if not signals:
        return html.Div("No signals in the latest run.", className="empty")
    body = []
    for name, s in signals.items():
        s = s or {}
        score = float(s.get("score") or 0.0)
        views = s.get("views") or {}
        body.append(html.Tr([
            html.Td(s.get("label", name), className="mono"),
            html.Td(name, className="mono"),
            html.Td(_data.fmt(s.get("value"))),
            html.Td(_data.fmt(s.get("weight"))),
            html.Td(_data.fmt(score),
                    style={"color": _score_color(score), "fontWeight": 600}),
            html.Td(f"L={views.get('level', '-')} D={views.get('delevel', '-')} "
                    f"S={views.get('surprise', '-')}", className="mono"),
            html.Td(s.get("surprise_method", "-"), className="mono"),
        ]))
    return html.Table([
        html.Thead(html.Tr([html.Th("Signal"), html.Th("Key"), html.Th("Value"),
                            html.Th("Weight"), html.Th("Score"),
                            html.Th("Views (level/delevel/surprise)"),
                            html.Th("Surprise src")])),
        html.Tbody(body),
    ], className="data-table")


def _event_panel(event) -> html.Div:
    if not event or not event.get("events"):
        return html.Div("No scheduled events in the window.", className="empty")
    rows = []
    for e in event.get("events", []):
        rows.append(html.Tr([
            html.Td(e.get("date", "-"), className="mono"),
            html.Td(e.get("name", "-")),
            html.Td(f"{e.get('in_days', '-')}d"),
            html.Td("major" if e.get("major") else "minor"),
        ]))
    return html.Table([
        html.Thead(html.Tr([html.Th("Date"), html.Th("Event"),
                            html.Th("In"), html.Th("Class")])),
        html.Tbody(rows),
    ], className="data-table")


# --------------------------------------------------------------------------- #
# Page body
# --------------------------------------------------------------------------- #
def body():
    snap = _data.economic_snapshot()
    latest = snap["latest"] or {}
    cfg = snap["config"] or {}
    econ_cfg = (cfg.get("engines", {}) or {}).get("economic", {}) or {}

    history = _data.economic_history(limit=400)

    regime = latest.get("regime", "UNKNOWN")
    score = float(latest.get("risk_on_score") or 0.0)
    mult = float(latest.get("gross_exposure_multiplier") or 1.0)
    md = latest.get("multiplier_detail") or {}
    model = latest.get("regime_model") or {}
    signals = latest.get("signals") or {}
    event = latest.get("event")
    n_signals = int(latest.get("signals_available") or len(signals))

    if not snap["has_history"]:
        header_note = html.Div(
            "No economic runs recorded yet. Enable persistence with "
            "{\"engines\": {\"economic\": {\"persist\": true}}} and run the "
            "engine (or the runner) at least once - charts populate from "
            "data/economic_runs/.",
            className="empty")
    else:
        header_note = html.Div(
            f"Regime: {regime}  ·  risk-on {score:+.2f}  ·  "
            f"gross {mult:.2f}x  ·  model: {model.get('method', 'none')}",
            className="kpi-label")

    return [
        html.H2("Economic - Macro Regime Engine"),
        header_note,

        # ---- KPI row ---------------------------------------------------- #
        html.Div([
            html.Div([html.Div("Regime", className="kpi-label"),
                      html.Div(regime, className="kpi-value",
                               style={"color": _REGIME_COLORS.get(regime, _SLATE)})],
                     className="kpi-card"),
            html.Div([html.Div("Risk-on score", className="kpi-label"),
                      html.Div(f"{score:+.2f}", className="kpi-value",
                               style={"color": _score_color(score)})],
                     className="kpi-card"),
            html.Div([html.Div("Gross multiplier", className="kpi-label"),
                      html.Div(f"{mult:.2f}x", className="kpi-value",
                               style={"color": _ACCENT})],
                     className="kpi-card"),
            html.Div([html.Div("P(RISK_ON)", className="kpi-label"),
                      html.Div(
                          "-" if model.get("p_risk_on") is None
                          else f"{float(model['p_risk_on']):.0%}",
                          className="kpi-value")],
                     className="kpi-card"),
            html.Div([html.Div("Signals", className="kpi-label"),
                      html.Div(str(n_signals), className="kpi-value")],
                     className="kpi-card"),
        ], className="kpi-row"),

        # ---- Gauges ----------------------------------------------------- #
        html.Div([
            html.Div([html.H3("Regime gauge"),
                      dcc.Graph(id="econ-regime-gauge",
                                figure=_regime_gauge(score, regime),
                                config={"displayModeBar": False})],
                     className="panel"),
            html.Div([html.H3("Exposure tilt"),
                      dcc.Graph(id="econ-mult-gauge",
                                figure=_multiplier_gauge(
                                    mult, md.get("path", "score")),
                                config={"displayModeBar": False})],
                     className="panel"),
        ], style={"display": "grid",
                  "gridTemplateColumns": "1fr 1fr", "gap": "16px"}),

        # ---- Signal bar + radar ---------------------------------------- #
        html.Div([
            html.Div([html.H3("Signal contributions"),
                      dcc.Graph(id="econ-signal-bar",
                                figure=_signal_bar(signals),
                                config={"displayModeBar": False})],
                     className="panel"),
            html.Div([html.H3("Signal shape (radar)"),
                      dcc.Graph(id="econ-signal-radar",
                                figure=_signal_radar(signals),
                                config={"displayModeBar": False})],
                     className="panel"),
        ], style={"display": "grid",
                  "gridTemplateColumns": "3fr 2fr", "gap": "16px"}),

        # ---- History ---------------------------------------------------- #
        html.Div([html.H3("Risk-on score - history"),
                  dcc.Graph(id="econ-history-line",
                            figure=_history_line(history),
                            config={"displayModeBar": False})],
                 className="panel"),
        html.Div([html.H3("Regime timeline"),
                  dcc.Graph(id="econ-regime-timeline",
                            figure=_regime_timeline(history),
                            config={"displayModeBar": False})],
                 className="panel"),
        html.Div([html.H3("Gross-exposure multiplier - history"),
                  dcc.Graph(id="econ-mult-history",
                            figure=_multiplier_history(history),
                            config={"displayModeBar": False})],
                 className="panel"),

        # ---- Model posterior + event risk ------------------------------ #
        html.Div([
            html.Div([html.H3("Regime model posterior"),
                      dcc.Graph(id="econ-posterior",
                                figure=_posterior_bar(model),
                                config={"displayModeBar": False})],
                     className="panel"),
            html.Div([html.H3("Event risk (uncertainty)"),
                      _event_panel(event)],
                     className="panel"),
        ], style={"display": "grid",
                  "gridTemplateColumns": "1fr 1fr", "gap": "16px"}),

        # ---- Signal table ---------------------------------------------- #
        html.Div([html.H3("Signal detail"), _signal_table(signals)],
                 className="panel"),

        # ---- Config echo ----------------------------------------------- #
        html.Div([
            html.H3("Engine configuration"),
            html.Div(
                f"regime_model={econ_cfg.get('regime_model', 'none')}  ·  "
                f"model_weight={econ_cfg.get('model_weight', 0.0)}  ·  "
                f"use_surprise={econ_cfg.get('use_surprise', True)}  ·  "
                f"use_delevel={econ_cfg.get('use_delevel', True)}  ·  "
                f"use_nlp={econ_cfg.get('use_nlp', True)}  ·  "
                f"event_trim={econ_cfg.get('event_trim', 0.0)}",
                className="mono"),
        ], className="panel"),
    ]


def layout(**_kwargs):
    return html.Div(
        [
            html.Div(id="economic-refresh-out", children=body()),
            dcc.Interval(id="economic-refresh", interval=10000, n_intervals=0),
        ],
        className="page",
    )