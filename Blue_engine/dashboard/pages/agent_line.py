"""
agent_line.py - Page 11: the AGENT LINE (factory view of the stage skills).

WHY THIS PAGE EXISTS
====================
The pipeline publishes a shift ledger (``blue_engine.core.agent_team``): one row
per station, in machine order, saying who worked, with what evidence, what it
produced, where its JSON landed, and what material is still missing.

This page is the operator's view of that line:

  * the roster, so it is obvious which employee is posted where;
  * per-station state - worked / idle / blocked / failed / skipped;
  * the stop point, if the line stopped, and WHY;
  * the material request list the next shift needs.

The ROSTER is always shown (it is static config). The SHIFT comes from the last
run this process performed; if this process has not run the pipeline, the page
says so instead of inventing a shift.

Read-only. This page never writes, never starts a run and never touches an
order. The AI Insight header is a pure function of its inputs, per the
dashboard contract in ``_ai_insight.py``.
"""
from __future__ import annotations

import dash
from dash import dcc, html

from blue_engine.dashboard.pages._ai_insight import (
    _GREEN as _AI_GREEN,
    _GREY as _AI_GREY,
    _RED as _AI_RED,
    _AMBER as _AI_AMBER,
    ai_insight_card,
)
from blue_engine.dashboard.ui.kpi import WARNING, KpiCard, KpiRow, pill
from blue_engine.dashboard.ui.panel import Panel
from blue_engine.dashboard.ui.table import DataTable

dash.register_page(__name__, path="/agent-line", name="Agent Line", order=11)

#: station state -> (label, colour token)
_STATE_STYLE = {
    "worked": ("WORKED", _AI_GREEN),
    "idle": ("IDLE", _AI_GREY),
    "blocked": ("BLOCKED", _AI_RED),
    "failed": ("FAILED", _AI_AMBER),
    "skipped": ("SKIPPED", _AI_GREY),
}


# --------------------------------------------------------------------------- #
# Data - honest, never fabricated
# --------------------------------------------------------------------------- #
def _roster() -> list:
    """The static roster. Always available, straight from the skill registry."""
    try:
        from blue_engine.core.agent_team import AgentTeam
        from blue_engine.core.stage_skills import load_skills

        return AgentTeam(load_skills(None)).roster()
    except Exception:  # noqa: BLE001 - the page must render even if imports move
        return []


def _last_shift() -> dict:
    """The last REAL shift, or {} when no run has happened.

    The runner publishes the ledger through ``dashboard.state`` (in-process
    mirror + ``data/last_shift.json``), so the dashboard never re-runs the
    pipeline to draw a picture. No run => no shift: a fabricated shift would be
    worse than an empty page.
    """
    try:
        from blue_engine.dashboard import state as _state

        return _state.read_shift()
    except Exception:  # noqa: BLE001 - the page must render even if it moves
        return {}


def _render_state(_value, row):
    label, _colour = _STATE_STYLE.get(row.get("state", ""), ("UNKNOWN", _AI_GREY))
    return pill(label, ok=(row.get("state") == "worked"))


def _station_columns():
    return [
        {"key": "order", "label": "#", "mono": True},
        {"key": "title", "label": "Station", "bold": True},
        {"key": "employee", "label": "Employee", "mono": True},
        {"key": "state", "label": "State", "render": _render_state},
        {"key": "produces", "label": "Produces"},
        {"key": "missing", "label": "Missing evidence", "mono": True},
    ]


def _station_rows(shift: dict) -> list:
    rows = []
    for station in shift.get("stations") or []:
        rows.append({
            "order": station.get("order", ""),
            "title": station.get("title", station.get("stage", "")),
            "employee": station.get("employee", ""),
            "state": station.get("state", ""),
            "produces": station.get("produced", ""),
            "missing": ", ".join(station.get("missing_evidence") or []) or "-",
        })
    return rows


def _roster_rows(roster: list) -> list:
    return [{
        "order": row.get("order", ""),
        "title": row.get("title", ""),
        "employee": row.get("employee", ""),
        "state": "not run" if not row.get("has_skill") else "posted",
        "produces": row.get("produces", ""),
        "missing": "-",
    } for row in roster]


def _request_rows(shift: dict) -> list:
    return [{"stage": stage, "material": ", ".join(items)}
            for stage, items in (shift.get("material_requests") or {}).items()]


# --------------------------------------------------------------------------- #
# Body
# --------------------------------------------------------------------------- #
def body():
    shift = _last_shift()
    roster = _roster()
    ran = bool(shift)

    summary = (shift.get("summary") or {}) if ran else {}
    blocked = summary.get("blocked") or []
    worked = summary.get("worked") or []
    stopped_at = shift.get("stopped_at") if ran else None

    if not ran:
        status, colour = "not run", _AI_GREY
        line_state = "no shift in this process"
    elif stopped_at:
        status, colour = "stopped", _AI_RED
        line_state = f"stopped at {stopped_at}"
    else:
        status, colour = "clear", _AI_GREEN
        line_state = "line clear"

    ai_rows = [
        ("Product", shift.get("product", "one risk-checked trading decision"),
         _AI_GREY),
        ("Line", line_state, colour),
        ("Stations worked", f"{len(worked)}" if ran else "-",
         _AI_GREEN if worked else _AI_GREY),
        ("Stations blocked", f"{len(blocked)}" if ran else "-",
         _AI_RED if blocked else _AI_GREY),
        ("Ready to trade", str(shift.get("ready_to_trade")) if ran else "-",
         _AI_GREY),
        ("Writes", "JSON only (no code, no orders)", _AI_GREY),
    ]

    children = [
        ai_insight_card(
            "Agent Line - synchronized stage skills on one product",
            ai_rows,
            note="Each station is one small skill feeding one deterministic "
                 "machine. The line only stops for MISSING DATA, never for a "
                 "missing opinion. Read-only page: it never writes a limit and "
                 "never starts a run.",
            status=status, status_color=colour,
        ),
        html.H2("Agent Line - Factory Floor", style={"marginTop": 0}),
        KpiRow([
            KpiCard("Stations", len(roster), icon="mdi:factory"),
            KpiCard("Worked", len(worked) if ran else 0,
                    icon="mdi:account-check-outline",
                    colour=_AI_GREEN if worked else None),
            KpiCard("Blocked", len(blocked) if ran else 0,
                    icon="mdi:alert-octagon-outline",
                    colour=_AI_RED if blocked else None),
            KpiCard("Stop point", stopped_at or "-",
                    icon="mdi:hand-back-right-outline",
                    colour=WARNING if stopped_at else None),
        ]),
    ]

    if stopped_at:
        children.append(Panel(
            "Line stopped",
            html.Div([
                html.Div(f"Stopped at station: {stopped_at}",
                         style={"color": _AI_RED, "fontWeight": 600}),
                html.Div(shift.get("stop_reason", ""),
                         style={"color": _AI_GREY, "fontSize": "12px"}),
            ]),
            icon="mdi:hand-back-right-outline"))

    children.append(Panel(
        "This shift" if ran else "Roster (no shift yet)",
        DataTable(_station_columns(),
                  _station_rows(shift) if ran else _roster_rows(roster),
                  empty="No stations recorded.", highlight_on_hover=False),
        icon="mdi:account-group-outline"))

    if ran and (shift.get("material_requests") or {}):
        children.append(Panel(
            "Material requests for the next shift",
            DataTable([
                {"key": "stage", "label": "Station", "bold": True},
                {"key": "material", "label": "Missing material", "mono": True},
            ], _request_rows(shift), empty="Nothing missing."),
            icon="mdi:clipboard-list-outline"))

    children.append(Panel(
        "Contract",
        html.Ul([
            html.Li("Employees write JSON only - never code, never config."),
            html.Li("Execution is logic only: nothing on this page can place, "
                    "size or cancel an order."),
            html.Li("A blocked station stops the line; downstream stations are "
                    "not staffed on a defective part."),
            html.Li("A missing opinion is 'idle': the machine runs on its "
                    "deterministic defaults."),
        ], style={"color": _AI_GREY, "fontSize": "12px", "marginBottom": 0}),
        icon="mdi:file-document-outline"))

    return children


def layout(**_kwargs):
    """Static body plus a slow poll so a new shift appears without a reload."""
    return html.Div(
        [
            html.Div(id="agent-line-body", children=body()),
            dcc.Interval(id="agent-line-refresh", interval=10000, n_intervals=0),
        ],
        className="page",
    )
