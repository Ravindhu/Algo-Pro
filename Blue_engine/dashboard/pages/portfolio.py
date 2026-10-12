"""
portfolio.py - Page 2: Live OMS/EMS Positions.

Renders open positions, fills and realized/unrealized P&L. Two sources,
merged:
  * a live :class:`~blue_engine.dashboard.app.LiveDashboard` (paper ledger),
    when the Dash UI is hosted in the same process;
  * otherwise, the empty state.

Migrated to the shared DMC kit: the hand-written table markup and the inline
``.kpi-card`` blocks are gone, replaced by ``ui.table.DataTable`` and
``ui.kpi``. Read-only.
"""
from __future__ import annotations

import time

import dash
from dash import dcc, html

from blue_engine.dashboard.pages import _data
from blue_engine.dashboard.pages._ai_insight import (
    _GREY as _AI_GREY,
    _GREEN as _AI_GREEN,
    _RED as _AI_RED,
    ai_insight_card,
)
from blue_engine.dashboard.ui.kpi import KpiCard, KpiRow, pnl_colour
from blue_engine.dashboard.ui.panel import Panel
from blue_engine.dashboard.ui.table import DataTable

dash.register_page(__name__, path="/portfolio", name="Portfolio", order=1)


_POSITION_COLUMNS = [
    {"key": "instrument", "label": "Instrument", "mono": True},
    {"key": "state", "label": "State"},
    {"key": "ltp", "label": "LTP", "mono": True, "type": "float", "align": "right"},
    {"key": "qty", "label": "Qty", "type": "int", "align": "right"},
    {"key": "avg_price", "label": "Avg price", "mono": True, "type": "float",
     "align": "right"},
    {"key": "_pnl_colour", "label": "Unrealized P&L", "mono": True,
     "type": "float", "align": "right", "bold": True},
]


def _position_rows(rows):
    """Shape live universe rows into display rows.

    ``_pnl_colour`` is materialised as a colour string so ``DataTable`` can
    colour the cell per row without a custom render callback.
    """
    out = []
    for r in rows:
        upnl = float(r.get("unrealized_pnl") or 0)
        out.append({
            "instrument": f"{r.get('symbol', '-')}/{r.get('exchange', '-')}",
            "state": r.get("state", "-"),
            "ltp": r.get("ltp"),
            "qty": int(r.get("position") or 0),
            "avg_price": r.get("avg_price"),
            "unrealized_pnl": upnl,
            "_pnl_colour": pnl_colour(upnl),
        })
    return out


_FILL_COLUMNS = [
    {"key": "when", "label": "Time", "mono": True},
    {"key": "symbol", "label": "Symbol", "mono": True},
    {"key": "side", "label": "Side", "bold": True},
    {"key": "qty", "label": "Qty", "type": "int", "align": "right"},
    {"key": "price", "label": "Price", "mono": True, "type": "float",
     "align": "right"},
    {"key": "state", "label": "State"},
    {"key": "pov_pct", "label": "POV %", "type": "float", "align": "right"},
]


def _fill_rows(trades):
    out = []
    for t in reversed(trades[-100:]):
        ts = t.get("ts") or 0
        side = t.get("side", "-")
        out.append({
            "when": time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "-",
            "symbol": t.get("symbol", "-"),
            "side": side,
            "_side_colour": _AI_GREEN if side == "BUY" else _AI_RED,
            "qty": t.get("qty"),
            "price": t.get("price"),
            "state": t.get("state", "-"),
            "pov_pct": t.get("pov_pct"),
        })
    return out


def body():
    """Portfolio page content (without the outer wrapper)."""
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

    # --- AI Insight header (read-only summary card) ------------------------ #
    ai_rows = [
        ("Open positions", str(open_positions),
         _AI_GREEN if open_positions else _AI_GREY),
        ("Realized P&L", _data.fmt(total_realized),
         pnl_colour(total_realized)),
        ("Unrealized P&L", _data.fmt(total_upnl), pnl_colour(total_upnl)),
        ("Fills", str(len(trades)), _AI_GREY),
        ("Ledger", "available" if snap.ledger_ok else "offline",
         _AI_GREEN if snap.ledger_ok else _AI_GREY),
    ]

    return [
        ai_insight_card(
            "Portfolio Setting - live OMS / EMS",
            ai_rows,
            note="Live positions and fills from the paper ledger; this page "
                 "never places an order.",
        ),
        html.H2("Portfolio - Live OMS / EMS", style={"marginTop": 0}),
        KpiRow([
            KpiCard("Open positions", open_positions, icon="mdi:briefcase-outline"),
            KpiCard("Realized P&L", _data.fmt(total_realized),
                    colour=pnl_colour(total_realized), icon="mdi:cash-check"),
            KpiCard("Unrealized P&L", _data.fmt(total_upnl),
                    colour=pnl_colour(total_upnl), icon="mdi:cash-clock"),
            KpiCard("Fills", len(trades), icon="mdi:swap-horizontal"),
        ]),
        Panel("Positions",
              DataTable(_POSITION_COLUMNS, _position_rows(rows),
                        empty="No open positions."),
              icon="mdi:briefcase-outline"),
        Panel("Recent fills",
              DataTable(_FILL_COLUMNS, _fill_rows(trades),
                        empty="No fills recorded yet."),
              icon="mdi:swap-horizontal"),
    ]


def layout(**_kwargs):
    """Full page: the refreshable body plus its polling Interval."""
    return html.Div(
        [
            html.Div(id="portfolio-refresh-out", children=body()),
            dcc.Interval(id="portfolio-refresh", interval=5000, n_intervals=0),
        ],
        className="page",
    )
