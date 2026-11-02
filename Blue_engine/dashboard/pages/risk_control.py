"""
risk_control.py - Page 3: Risk Engine Matrix.

Surfaces the risk inputs plus live guard limits: circuit bands, stop-loss,
quote-failure threshold, the POV rate clamps, and the slippage guard. The
"matrix" is the grid of input x current status, so a user can see at a
glance which guard is active and what its threshold is.
Migrated to the shared DMC kit: status pills are now ``dmc.Badge`` (so an
"unknown" state reads grey, not green), and the tables are ``DataTable``.
Read-only.
"""
from __future__ import annotations

import dash
from dash import dcc, html

from blue_engine.dashboard.pages import _data
from blue_engine.dashboard.pages._ai_insight import (
    _GREY as _AI_GREY,
    _GREEN as _AI_GREEN,
    _RED as _AI_RED,
    ai_insight_card,
)
from blue_engine.dashboard.ui.kpi import WARNING, KpiCard, KpiRow, pill
from blue_engine.dashboard.ui.panel import Panel
from blue_engine.dashboard.ui.table import DataTable

dash.register_page(__name__, path="/risk", name="Risk Control", order=2)


def _render_status(_value, row):
    """A real badge instead of a coloured <span>.

    ``ok=row['active']`` gives green/red. There are deliberately only two
    states: a guard is either enforcing or it is not.
    """
    return pill("ACTIVE" if row.get("active") else "OFF",
                ok=bool(row.get("active")))


def _matrix_columns():
    return [
        {"key": "guard", "label": "Guard", "bold": True},
        {"key": "threshold", "label": "Threshold", "mono": True},
        {"key": "status", "label": "Status", "render": _render_status},
        {"key": "note", "label": "Notes"},
    ]


def _labelled_columns():
    return [
        {"key": "key", "label": "Input", "mono": True},
        {"key": "value", "label": "Value", "mono": True},
        {"key": "group", "label": "Group"},
    ]


def _input_rows(rows, groups):
    return [
        {"key": r.key, "value": _data.fmt(r.value), "group": r.group}
        for r in rows if r.group in groups
    ]


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
    rows.extend(_capital_guard_rows(cfg))
    return rows


#: The money caps the runner enforces BEFORE any child order
#: (blue_engine/core/risk_gate.py). Absence of a key = that cap is NOT
#: enforced, which the matrix must show as OFF rather than hide.
_MONEY_CAPS = (
    ("max_order_quantity", "Max order quantity", "shares"),
    ("max_order_notional", "Max order notional", "INR"),
    ("max_daily_loss", "Max daily loss", "INR"),
    ("max_open_exposure", "Max open exposure", "INR"),
)


def _supervisor_rows() -> list:
    """Supervisor health as matrix rows (read-only, from its journal).

    ``read_status`` returns ``unknown`` when there is no journal, so an
    unsupervised engine can never look healthy by omission.
    """
    from blue_engine.core.supervisor import read_status

    status = read_status()
    healthy = status.get("state") in ("RUNNING", "STOPPED", "IDLE")
    return [{
        "guard": "Supervisor",
        "threshold": str(status.get("state") or "unknown"),
        "active": healthy,
        "note": (status.get("halt_reason")
                 or f"restarts={status.get('restarts', 0)}; "
                    f"last event {status.get('last_event_at') or '-'}"),
    }]


def _capital_guard_rows(cfg: dict) -> list:
    """Risk-gate caps as matrix rows (read-only; never writes a limit)."""
    block = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
    rows = []
    for key, label, unit in _MONEY_CAPS:
        value = block.get(key)
        configured = value is not None
        rows.append({
            "guard": label,
            "threshold": f"{_data.fmt(value)} {unit}" if configured else "not set",
            "active": configured,
            "note": "pre-trade cap" if configured else "UNLIMITED - not enforced",
        })
    rows.append({
        "guard": "Kill switch (risk.blocked)",
        "threshold": "BLOCKED" if block.get("blocked") else "open",
        "active": bool(block.get("blocked")),
        "note": ("all orders rejected" if block.get("blocked")
                 else "orders permitted"),
    })
    rows.append({
        "guard": "Live acknowledgement",
        "threshold": str(bool(block.get("require_live_ack"))),
        "active": bool(block.get("require_live_ack")),
        "note": ("a live run without money caps is rejected"
                 if block.get("require_live_ack")
                 else "live run may run uncapped"),
    })
    rows.extend(_supervisor_rows())
    return rows


def body():
    """Risk page content (without the outer wrapper)."""
    snap = _data.gather()
    rows = _matrix_rows(snap.config)
    active_count = sum(1 for r in rows if r["active"])

    # --- AI Insight header (read-only summary card) ------------------------ #
    guards_ok = active_count == len(rows)
    pov = snap.pov()
    ai_rows = [
        ("Guards active", f"{active_count}/{len(rows)}",
         _AI_GREEN if guards_ok else _AI_RED),
        ("Max slippage", f"{_data.fmt(pov.get('max_slippage_bps', 50.0))} bps",
         _AI_GREY),
        ("Quote failure threshold", str(pov.get("quote_failure_threshold", 3)),
         _AI_GREY),
        ("Executions", str(len(snap.executions)), _AI_GREY),
        ("Ledger", "available" if snap.ledger_ok else "offline",
         _AI_GREEN if snap.ledger_ok else _AI_GREY),
    ]

    return [
        ai_insight_card(
            "Risk Setting - engine guard matrix",
            ai_rows,
            note="Risk guards override strategy signals when a limit is "
                 "breached. This page is read-only and never writes a limit.",
        ),
        html.H2("Risk Control - Engine Matrix", style={"marginTop": 0}),
        KpiRow([
            KpiCard("Guards active", f"{active_count}/{len(rows)}",
                    icon="mdi:shield-check-outline",
                    colour=_AI_GREEN if active_count == len(rows) else WARNING),
            KpiCard("Executions", len(snap.executions),
                    icon="mdi:playlist-play"),
            KpiCard("Max slippage",
                    _data.fmt(snap.pov().get("max_slippage_bps", 50.0)),
                    colour=WARNING, icon="mdi:speedometer-slow", note="bps"),
        ]),
        Panel("Guard matrix",
              DataTable(_matrix_columns(), rows, empty="No guards found.",
                        highlight_on_hover=False),
              icon="mdi:shield-alert-outline"),
        Panel("Risk inputs (from config)",
              DataTable(_labelled_columns(),
                        _input_rows(snap.inputs, {"risk", "slippage"}),
                        empty="No risk inputs found."),
              icon="mdi:tune-variant"),
    ]


def layout(**_kwargs):
    """Full page: the refreshable body plus its polling Interval."""
    return html.Div(
        [
            html.Div(id="risk-refresh-out", children=body()),
            dcc.Interval(id="risk-refresh", interval=5000, n_intervals=0),
        ],
        className="page",
    )
