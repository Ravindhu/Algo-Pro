"""
terminal.py - Page 1: Runtime, POV band, and the config-only controls.

WHAT CHANGED FROM THE NATIVE BUILD
----------------------------------
The old page rendered every input as a read-only table row::

    html.Td(r.key), html.Td(_data.fmt(r.value)), html.Td(r.group)

so the operator could see the parameters but not touch them. This version
renders the same parameters as real Dash Mantine controls (``NumberInput``,
``Switch``, ``Select``) grouped into the same panels, and adds an explicit
Apply/Revert row.

THE SAFETY CONTRACT (option A - config-only)
--------------------------------------------
Pressing "Apply to config" writes to ``blue_engine/input/master.json``. The
running engine re-reads that file on its next cycle. **No order is placed,
modified or cancelled from this page** -- see ``ui/inputs.py`` for the full
statement, and ``ui/inputs.write_values`` for the atomic write.

The write is deliberate, not on-change: these are trading parameters, and a
stray keystroke in a number field must not silently move a risk limit. The
operator commits on purpose and gets a confirmation naming the file.
"""
from __future__ import annotations

import dash
import dash_mantine_components as dmc
from dash import Input, Output, State, callback, dcc, html
from dash_iconify import DashIconify

from blue_engine.dashboard.pages import _data
from blue_engine.dashboard.pages._ai_insight import (
    _GREY as _AI_GREY,
    _GREEN as _AI_GREEN,
    ai_insight_card,
)
from blue_engine.dashboard.pages.theme import theme
from blue_engine.dashboard.ui import inputs as ui_inputs
from blue_engine.dashboard.ui.kpi import (
    INFO,
    NEGATIVE,
    POSITIVE,
    WARNING,
    KpiCard,
    KpiRow,
)
from blue_engine.dashboard.ui.panel import Panel

dash.register_page(__name__, path="/", name="Terminal", order=0)


def body():
    """Terminal page content (without the outer wrapper)."""
    snap = _data.gather()
    pal = theme(snap.mode)
    pov = snap.pov()

    vwap = float(pov.get("_last_vwap") or 0) if pov.get("_last_vwap") else 0.0
    band = (_data.band_from_inputs(snap.config, vwap) if vwap
            else {"ceiling": 0.0, "floor": 0.0})

    engine = snap.live_engine
    rate_txt = "-"
    watch_txt = "-"
    if engine is not None:
        try:
            rate_txt = f"{engine.config.pov_min_pct:.1f}-{engine.config.pov_max_pct:.1f}%"
            watch_txt = f"{len(engine._machines)} symbols"  # noqa: SLF001
        except Exception:
            pass

    # Values for the controls come from the FILE, so the controls always show
    # what the engine will actually read -- not what the user last typed.
    values = ui_inputs.current_values()

    panels = []
    for group, title in ui_inputs.GROUP_TITLES.items():
        panels.append(
            Panel(
                title,
                ui_inputs.controls_for_group(group, values),
                icon=ui_inputs.GROUP_ICONS.get(group),
            )
        )

    # --- AI Insight header (read-only summary card) ------------------------ #
    ai_rows = [
        ("Mode", snap.mode, _AI_GREEN if snap.mode == "live" else _AI_GREY),
        ("Buyer VWAP", _data.fmt(vwap or None), "#e2e8f0"),
        ("POV ceiling / floor",
         f"{_data.fmt(band.get('ceiling') or None)} / {band.get('floor') or '-'}",
         "#e2e8f0"),
        ("POV rate band", rate_txt, _AI_GREY),
        ("Watching", watch_txt, _AI_GREY),
        ("Ledger", "available" if snap.ledger_ok else "offline",
         _AI_GREEN if snap.ledger_ok else _AI_GREY),
    ]

    return [
        ai_insight_card(
            "Terminal Setting - runtime & order entries",
            ai_rows,
            note="Config-only controls. Values are written to master.json "
                 "and the engine re-reads them; this page never places an order.",
        ),
        html.H2("Terminal - Runtime & Order Entries", style={"marginTop": 0}),
        dmc.Alert(
            "Config-only controls: values are written to blue_engine/input/"
            "master.json and the engine re-reads them. This dashboard never "
            "places an order.",
            title="Read-only execution surface",
            color="blue",
            variant="light",
            icon=DashIconify(icon="mdi:information-outline"),
            mb="md",
        ),
        KpiRow([
            KpiCard("Buyer VWAP", _data.fmt(vwap or None), colour=INFO,
                    icon="mdi:chart-line"),
            KpiCard("Ceiling (+%)", _data.fmt(band.get("ceiling") or None),
                    colour=NEGATIVE, icon="mdi:arrow-up-bold"),
            KpiCard("Floor (-%)", _data.fmt(band.get("floor") or None),
                    colour=POSITIVE, icon="mdi:arrow-down-bold"),
            KpiCard("POV rate band", rate_txt, colour=WARNING,
                    icon="mdi:speedometer"),
            KpiCard("Watching", watch_txt, icon="mdi:eye-outline"),
        ]),
        html.Div(id="cfg-feedback-out", children=[]),
        Panel("Band visualiser", dcc.Graph(
            id="band-graph",
            figure=_band_figure(vwap, band, pov, pal),
            config={"displayModeBar": False},
        )),
        *panels,
        ui_inputs.save_button(),
    ]


def layout(**_kwargs):
    """Full page: the refreshable body plus its polling Interval."""
    return html.Div(
        [
            html.Div(id="terminal-refresh-out", children=body()),
            dcc.Interval(id="terminal-refresh", interval=5000, n_intervals=0),
        ],
        className="page",
    )


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


@callback(
    Output("cfg-feedback-out", "children"),
    Output("terminal-refresh-out", "children"),
    Input("cfg-apply", "n_clicks"),
    Input("cfg-revert", "n_clicks"),
    # pattern-matching: every control in every group, whatever the group count
    State({"type": "cfg-input", "key": dash.ALL}, "value"),
    State({"type": "cfg-input", "key": dash.ALL}, "checked"),
    State({"type": "cfg-input", "key": dash.ALL}, "id"),
    prevent_initial_call=True,
)
def _handle_config_buttons(apply_clicks, revert_clicks, values, checked, ids):
    """Apply the typed values to the config file, or revert the UI to it.

    Revert is intentionally "re-read the file and re-render", not "undo in
    memory": the file is the source of truth, so reverting must show what the
    engine will actually read.
    """
    triggered = dash.ctx.triggered_id
    if triggered == "cfg-revert":
        return (dmc.Notification(
            title="Reverted",
            message="Controls reloaded from blue_engine/input/master.json.",
            color="blue", action="show", autoClose=4000,
        ), body())

    if triggered != "cfg-apply":
        return dash.no_update, dash.no_update

    # Merge the two state lists back into one {key: value} dict. Switches carry
    # their state in `checked`, everything else in `value`.
    updates = {}
    for cid, val, chk in zip(ids or [], values or [], checked or []):
        key = cid.get("key")
        spec = ui_inputs.SPEC_BY_KEY.get(key)
        if spec is None:
            continue
        updates[key] = chk if spec.kind == "switch" else val

    ok, messages = ui_inputs.write_values(updates)
    return (
        dmc.Notification(
            title="Config saved" if ok else "Save failed",
            message=" ".join(messages) or "No changes.",
            color="green" if ok else "red",
            action="show",
            autoClose=6000 if ok else False,
        ),
        body(),   # rebuild so the controls reflect the file's new state
    )


def _band_figure(vwap: float, band: dict, pov: dict, pal: dict | None = None):
    """A simple horizontal band diagram (no external state needed)."""
    import plotly.graph_objects as go

    if pal is None:
        pal = theme("analyzer")
    is_light = pal.get("color_scheme") == "light"

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
