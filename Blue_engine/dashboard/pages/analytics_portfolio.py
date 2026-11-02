"""
analytics_portfolio.py - Phase 6: the Portfolio Analytics console.

What this page is
-----------------
The existing ``pages/portfolio.py`` is the **live OMS/EMS view**: what we hold
right now, today's fills, today's P&L. This page is the *management* view that
sits beside it: how the book has behaved over time, where its risk actually is,
where the P&L came from, and whether the desk is currently allowed to trade.

Six panels, each fed by one Phase from the portfolio module:

    Equity curve + underwater   <- Phase 1  (_data.equity_curve)
    Risk & performance          <- Phase 2  (_data.risk_report)
    Exposure & concentration    <- Phase 3  (_data.exposure_snapshot)
    P&L attribution             <- Phase 3  (_data.attribution_snapshot)
    Policy / kill-switch        <- Phase 4  (_data.policy_snapshot)
    Views (stated vs allowed)   <- Phase 5  (_data.views_snapshot)

Design rules
------------
* **Read-only.** Every builder reads a persisted artefact. Nothing here can
  place an order or move the book, and no builder triggers an optimiser run —
  a UI poll must never be able to change a position.
* **Annotated empty states, never blank panels.** A chart with no data explains
  *why* ("no NAV rows yet - run the daily job") rather than rendering an empty
  frame the operator has to interpret.
* **Defensive.** Every builder is wrapped so one bad panel cannot blank the page.
  A dashboard that dies because a JSONL line is torn is worse than one with a
  gap, and the desk will stop trusting it.
* **Plain English over jargon.** Where a number is easy to misread (HHI,
  effective bets, CVaR), the panel says what it means in one line. The desk is
  the audience, not the author.

All charts use the same dark Plotly template as the Economic page so the
terminal looks like one product.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import dash
import dash_mantine_components as dmc
from dash import dcc, html

from blue_engine.dashboard.pages import _data
from blue_engine.dashboard.pages._ai_insight import ai_insight_card
from blue_engine.dashboard.ui.kpi import KpiCard
from blue_engine.dashboard.ui.plotly_theme import dashboard_dark_template
from blue_engine.core.results import safe_float

logger = logging.getLogger(__name__)

dash.register_page(__name__, path="/analytics-portfolio",
                   name="Portfolio Analytics", order=7)

# Shared palette (kept consistent with pages/economic.py).
_GREEN = "#40C057"
_RED = "#FA5252"
_AMBER = "#FCC419"
_SLATE = "#909296"
_ACCENT = "#4DABF7"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _pnl_color(v: Optional[float]) -> str:
    if v is None:
        return _SLATE
    if v > 0:
        return _GREEN
    if v < 0:
        return _RED
    return _SLATE


def _wrap(text: str, width: int = 58) -> str:
    """Word-wrap to ``width`` chars, joined with Plotly's ``<br>``."""
    words = str(text).split()
    lines: List[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "<br>".join(lines)


def _empty_fig(message: str, height: int = 300):
    """An annotated empty figure — never a blank frame."""
    import plotly.graph_objects as go

    fig = go.Figure()
    fig.add_annotation(text=_wrap(message), showarrow=False, align="center",
                       font=dict(color=_SLATE, size=13))
    fig.update_layout(template=dashboard_dark_template(), height=height,
                      margin=dict(l=30, r=30, t=30, b=30),
                      xaxis=dict(visible=False), yaxis=dict(visible=False))
    return fig


def _kpi(label: str, value: str, color: Optional[str] = None,
         note: Optional[str] = None) -> html.Div:
    """Thin backward-compatible wrapper over ``ui.kpi.KpiCard``.

    Kept as a local shim (rather than editing ~20 call sites in this 537-line
    page) so the migration is reviewable: the visual change comes from the kit,
    and the page's own structure is untouched. New pages should call KpiCard
    directly -- this wrapper exists only for this file.
    """
    return KpiCard(label, value, colour=color, note=note)


def _pct(v: Optional[float], digits: int = 2) -> str:
    return "-" if v is None else f"{v * 100:.{digits}f}%"


def _num(v: Optional[float], digits: int = 2) -> str:
    return "-" if v is None else f"{v:,.{digits}f}"


def _warnings_block(warnings: Optional[List[str]]) -> Optional[Any]:
    """Render warnings visibly -- a silently-degraded panel is a trap.

    Uses a DMC Alert so a warning is styled as a warning (the native build's
    ``.warn-line`` class had no CSS left once _THEME_CSS was trimmed, which
    would have made these render as plain body text).
    """
    if not warnings:
        return None
    return dmc.Alert(
        dmc.List([dmc.ListItem(str(w)) for w in warnings], size="xs",
                 spacing=2),
        title="Degraded",
        color="yellow",
        variant="light",
        mb="sm",
    )


# --------------------------------------------------------------------------- #
# 1. Equity curve + underwater
# --------------------------------------------------------------------------- #

def _equity_figure(curve: Dict[str, Any]):
    import plotly.graph_objects as go

    dates = [d for d in (curve.get("dates") or [])]
    navs = [n for n in (curve.get("nav") or [])]
    if len(dates) < 2 or len(navs) < 2:
        return _empty_fig("No equity curve yet. The daily post-close job writes "
                          "one NAV row per session (data/portfolio_nav/). "
                          "Run: python -m blue_engine.jobs.daily_portfolio "
                          "--force")

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dates, y=navs, mode="lines", name="NAV",
                             line=dict(color=_ACCENT, width=2)))
    fig.update_layout(template=dashboard_dark_template(), height=300,
                      margin=dict(l=50, r=20, t=30, b=40),
                      yaxis_title="NAV", showlegend=False)
    return fig


def _underwater_figure(curve: Dict[str, Any], perf: Dict[str, Any]):
    import plotly.graph_objects as go

    uw = (perf or {}).get("performance", {}).get("underwater") or {}
    dates = uw.get("dates") or []
    dd = uw.get("drawdown") or []
    if len(dates) < 2 or len(dd) < 2:
        return _empty_fig("No drawdown history yet — this chart needs at least "
                          "two NAV rows.", height=260)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=dates, y=[v * 100.0 for v in dd], mode="lines", name="Drawdown",
        fill="tozeroy", line=dict(color=_RED, width=1)))
    fig.update_layout(template=dashboard_dark_template(), height=260,
                      margin=dict(l=50, r=20, t=30, b=40),
                      yaxis_title="drawdown %", showlegend=False)
    return fig


# --------------------------------------------------------------------------- #
# 2. Risk & performance
# --------------------------------------------------------------------------- #

def _risk_kpis(rep: Dict[str, Any]) -> List[html.Div]:
    perf = (rep.get("performance") or {})
    s = perf.get("summary") or {}
    risk = (rep.get("risk") or {})
    dd = perf.get("drawdown") or {}

    net_cap = rep.get("capital")
    var_amt = (risk.get("amounts") or {}).get("var_cf")
    cvar_amt = (risk.get("amounts") or {}).get("cvar")

    return [
        _kpi("Sharpe", _num(s.get("sharpe"), 2),
             note="return per unit of total risk"),
        _kpi("Sortino", _num(s.get("sortino"), 2),
             note="return per unit of DOWNSIDE risk"),
        _kpi("TWRR (manager)", _pct(s.get("twrr")),
             note="time-weighted - the fair number vs the benchmark"),
        _kpi("XIRR (your money)", _pct(s.get("xirr")),
             note="money-weighted - what the capital actually earned"),
        _kpi("Max drawdown", _pct(s.get("max_drawdown")),
             color=_RED if (s.get("max_drawdown") or 0) > 0.10 else None,
             note="deepest fall from a high"),
        _kpi("Under water", (
            "still down" if dd.get("recovered") is False else
            ("-") if dd.get("recovered") is None else
            f"{dd.get('underwater_length')} days"
        ), note="time to recover the worst fall"),
        _kpi("VaR 95% (day)", _pct(risk.get("var", {}).get("cornish_fisher")),
             color=_AMBER,
             note=(f"about Rs {_num(var_amt, 0)}" if var_amt
                   else "fat-tail estimate")),
        _kpi("CVaR 95% (day)", _pct(risk.get("cvar")), color=_RED,
             note=(f"average bad-day loss"
                   + (f" ~Rs {_num(cvar_amt, 0)}" if cvar_amt else ""))),
    ]


def _stress_table(rep: Dict[str, Any]) -> html.Table:
    rows = (rep.get("risk") or {}).get("stress") or []
    if not rows:
        return html.Div("No stress scenarios available.", className="empty")
    body = []
    amounts = ((rep.get("risk") or {}).get("amounts") or {}).get("stress") or {}
    for r in rows:
        pnl = float(r.get("pnl_pct") or 0.0)
        body.append(html.Tr([
            html.Td(r.get("label", r.get("scenario", "-"))),
            html.Td(_pct(pnl), style={"color": _pnl_color(pnl),
                                      "fontWeight": 600}),
            html.Td(_num(amounts.get(r.get("scenario")), 0)),
        ]))
    return html.Table([
        html.Thead(html.Tr([html.Th("Scenario"), html.Th("Book impact"),
                            html.Th("Amount")])),
        html.Tbody(body),
    ], className="data-table")


# --------------------------------------------------------------------------- #
# 3. Exposure
# --------------------------------------------------------------------------- #

def _sector_figure(exp: Dict[str, Any]):
    import plotly.graph_objects as go

    sectors = ((exp.get("by_sector") or {}).get("sectors") or {})
    if not sectors:
        return _empty_fig("No exposure yet — the engine has no target book. "
                          "Run the pipeline, or check that the provider "
                          "returned a universe.")

    names = list(sectors)
    weights = [abs(float((sectors[n] or {}).get("abs_weight") or 0.0)) * 100.0
               for n in names]
    fig = go.Figure(go.Pie(labels=names, values=weights, hole=0.55,
                           textinfo="label+percent",
                           marker=dict(line=dict(color="#1A1B1E", width=1))))
    fig.update_layout(template=dashboard_dark_template(), height=340,
                      margin=dict(l=20, r=20, t=30, b=20),
                      showlegend=False)
    return fig


def _exposure_kpis(exp: Dict[str, Any]) -> List[html.Div]:
    ls = exp.get("long_short") or {}
    beta = exp.get("beta") or {}
    con = exp.get("concentration") or {}
    return [
        _kpi("Gross exposure", _pct(ls.get("gross")),
             note="long + short, as a share of capital"),
        _kpi("Net exposure", _pct(ls.get("net")),
             note="long minus short - the directional bet"),
        _kpi("Portfolio beta", _num(beta.get("beta"), 2),
             note=(f"{beta.get('n_measured', 0)} measured / "
                   f"{beta.get('n_assumed', 0)} assumed")),
        _kpi("Effective bets", _num(con.get("effective_bets"), 2),
             color=_AMBER if (con.get("effective_bets") or 9) < 3 else None,
             note="1/HHI - how many INDEPENDENT bets the book holds"),
        _kpi("Largest sector", _pct(con.get("largest_sector_share")),
             color=_AMBER
             if (con.get("largest_sector_share") or 0) > 0.40 else None,
             note=str(con.get("largest_sector") or "-")),
        _kpi("Largest sleeve", _pct(con.get("largest_sleeve_share")),
             note=str(con.get("largest_sleeve") or "-")),
    ]


# --------------------------------------------------------------------------- #
# 4. P&L attribution
# --------------------------------------------------------------------------- #

def _attribution_table(attr: Dict[str, Any]) -> html.Table:
    groups = (attr.get("by_sleeve") or {}).get("groups") or {}
    if not groups:
        return html.Div("No realised P&L yet — attribution needs closed trades.",
                        className="empty")
    body = []
    for name, g in groups.items():
        gross = float(g.get("realized_pnl") or 0.0)
        net = float(g.get("net_pnl") or 0.0)
        body.append(html.Tr([
            html.Td(name, className="mono"),
            html.Td(_num(gross, 2), style={"color": _pnl_color(gross)}),
            html.Td(_num(g.get("fees"), 2), style={"color": _SLATE}),
            html.Td(_num(net, 2), style={"color": _pnl_color(net),
                                         "fontWeight": 600}),
            html.Td(str(g.get("fills", 0))),
        ]))
    return html.Table([
        html.Thead(html.Tr([html.Th("Sleeve"), html.Th("Gross P&L"),
                            html.Th("Costs"), html.Th("Net P&L"),
                            html.Th("Fills")])),
        html.Tbody(body),
    ], className="data-table")


def _bucket_figure(attr: Dict[str, Any]):
    import plotly.graph_objects as go

    buckets = (attr.get("by_bucket") or {}).get("buckets") or {}
    if not buckets:
        return _empty_fig("No session-bucket attribution yet — it needs closed "
                          "trades with timestamps.", height=300)

    names = list(buckets)
    vals = [float((buckets[n] or {}).get("net_pnl") or 0.0) for n in names]
    fig = go.Figure(go.Bar(x=names, y=vals,
                           marker_color=[_pnl_color(v) for v in vals],
                           text=[_num(v, 0) for v in vals],
                           textposition="outside"))
    fig.add_hline(y=0, line_color=_SLATE, line_width=1)
    fig.update_layout(template=dashboard_dark_template(), height=300,
                      margin=dict(l=50, r=20, t=30, b=40),
                      yaxis_title="net P&L", showlegend=False)
    return fig


def _reconciliation_note(attr: Dict[str, Any]) -> Optional[html.Div]:
    """Surface a non-reconciling attribution loudly.

    An attribution table that does not add up is worse than no table, because
    someone will act on the gap.
    """
    checks = attr.get("reconciliation") or {}
    bad = [k for k, v in checks.items() if not v.get("ok")]
    if not bad:
        return None
    return html.Div(
        [html.Div(f"ATTRIBUTION OUT OF BALANCE: {', '.join(bad)} — "
                  f"do not trade on these splits until resolved.",
                  className="warn-line")],
        className="warn-block")


# --------------------------------------------------------------------------- #
# 5. Policy / kill-switch
# --------------------------------------------------------------------------- #

def _policy_panel(pol: Dict[str, Any]) -> List[Any]:
    action = str(pol.get("action") or "UNKNOWN")
    trade = pol.get("trade")
    kill = pol.get("kill") or {}
    tripped = bool(kill.get("tripped"))

    colour = {"OPTIMISE": _GREEN, "HALT": _AMBER,
              "EXIT_TO_CASH": _RED}.get(action, _SLATE)
    cards = [
        _kpi("Desk state", action, color=colour,
             note=("Trading permitted" if trade
                   else "New risk is blocked")),
        _kpi("Optimiser", str(pol.get("optimiser") or "-"),
             note="chosen by the current regime"),
        _kpi("Kill-switch", "TRIPPED" if tripped else "clear",
             color=_RED if tripped else _GREEN,
             note=str(kill.get("reason") or "within limits")),
        _kpi("Drawdown used", _pct(pol.get("drawdown")),
             color=_RED if (pol.get("drawdown") or 0) >= 0.15 else None,
             note="read from the stored equity curve"),
    ]
    reasons = pol.get("reasons") or []
    body: List[Any] = [html.Div(cards, className="kpi-row")]
    if reasons:
        body.append(html.Div([
            html.H3("Why this decision"),
            html.Ul([html.Li(str(r)) for r in reasons]),
        ], className="panel"))
    warn = _warnings_block(pol.get("warnings"))
    if warn is not None:
        body.append(html.Div([html.H3("Warnings"), warn], className="panel"))
    return body


# --------------------------------------------------------------------------- #
# 6. Views (stated vs allowed)
# --------------------------------------------------------------------------- #

def _views_table(views: Dict[str, Any]) -> html.Table:
    rows = views.get("views") or []
    if not rows:
        return html.Div(
            "No views supplied. Views are opt-in: set views_enabled=true and "
            "provide headlines / returns / text to the provider.",
            className="empty")
    body = []
    for v in rows:
        stated = v.get("stated_confidence")
        used = v.get("used_confidence")
        body.append(html.Tr([
            html.Td(", ".join(v.get("assets") or []), className="mono"),
            html.Td(v.get("source", "-")),
            html.Td(_pct(v.get("return"), 2),
                    style={"color": _pnl_color(v.get("return"))}),
            html.Td(_pct(stated) if stated is not None else "-"),
            html.Td(_pct(used), style={"fontWeight": 600, "color": _ACCENT}),
        ]))
    return html.Table([
        html.Thead(html.Tr([html.Th("Sleeve(s)"), html.Th("Source"),
                            html.Th("View"), html.Th("Stated conf."),
                            html.Th("Allowed conf.")])),
        html.Tbody(body),
    ], className="data-table")


# --------------------------------------------------------------------------- #
# 6b. Cash flows (deposits / withdrawals) and the return basis
# --------------------------------------------------------------------------- #

def _cash_flow_panel(curve: Dict[str, Any], rep: Dict[str, Any]) -> html.Div:
    """Deposits/withdrawals ledger plus the two return definitions.

    This panel exists to make XIRR and TWRR *self-explaining*: a reader who sees
    'XIRR 12% / TWRR 9%' next to a flow list can tell which number answers which
    question. It is also the audit trail for the return: without the flows, a
    NAV jump is ambiguous between a deposit and a gain.
    """
    rows = curve.get("rows") or []
    summary = (rep.get("performance") or {}).get("summary") or {}

    # Flow rows only - a normal session has flow == 0 and is not worth a line.
    flow_rows = [r for r in rows if (safe_float(r.get("flow")) or 0.0) != 0.0]

    header = html.Tr([html.Th("Date"), html.Th("Flow"), html.Th("NAV"),
                      html.Th("Contributions"), html.Th("Source")])
    if flow_rows:
        body = []
        for r in flow_rows[-60:]:          # cap the table; newest 60 flows
            f = safe_float(r.get("flow")) or 0.0
            body.append(html.Tr([
                html.Td(str(r.get("date") or "-")),
                html.Td(_num(f, 0), style={"color": _pnl_color(f),
                                          "fontWeight": 600}),
                html.Td(_num(r.get("nav"), 0)),
                html.Td(_num(r.get("contributions"), 0)),
                html.Td(str(r.get("flow_source") or "-")),
            ]))
        table: Any = html.Table([html.Thead(header), html.Tbody(body)],
                                className="data-table")
    else:
        table = html.Div(
            "No external deposits or withdrawals recorded. Every rupee of change "
            "in NAV is trading P&L, so XIRR and the simple return agree.",
            className="empty")

    twrr = summary.get("twrr")
    xirr = summary.get("xirr")
    net_dep = sum((safe_float(r.get("flow")) or 0.0) for r in rows)

    return html.Div([
        html.H3("Cash flows (deposits / withdrawals)"),
        html.Div("XIRR answers 'what did MY money earn?' (it weights the timing of "
                 "every deposit); TWRR answers 'how good was the manager?' (it "
                 "neutralises them). Compare TWRR to the benchmark; read XIRR as "
                 "the realised experience.",
                 style={"color": _SLATE, "fontSize": "12px"}),
        html.Div([
            _kpi("Net contributed", _num(net_dep, 0),
                 color=_GREEN if net_dep > 0 else (_RED if net_dep < 0 else None),
                 note="deposits minus withdrawals over the whole window"),
            _kpi("TWRR (manager)", _pct(twrr),
                 note="time-weighted - the fair benchmark-relative number"),
            _kpi("XIRR (your money)", _pct(xirr),
                 note="money-weighted - what the capital actually earned"),
        ], className="kpi-row"),
        table,
    ])


# --------------------------------------------------------------------------- #

def body() -> List[Any]:
    """The page content. Every panel is defensive: one failure cannot blank all."""
    panels: List[Any] = [
        html.H2("Portfolio Analytics"),
        html.Div(
            "Time-series and risk view of the book. The live OMS/EMS view is on "
            "the Portfolio page; this page is read-only and never places an order.",
            style={"color": _SLATE, "fontSize": "13px",
                   "marginBottom": "12px"}),
    ]

    # -- Phase 1: equity ------------------------------------------------ #
    try:
        curve = _data.equity_curve()
    except Exception as exc:  # noqa: BLE001
        logger.debug("equity panel failed: %s", exc)
        curve = {"warnings": [f"equity panel failed: {exc}"]}

    # -- Phase 2: risk -------------------------------------------------- #
    try:
        rep = _data.risk_report()
    except Exception as exc:  # noqa: BLE001
        logger.debug("risk panel failed: %s", exc)
        rep = {"warnings": [f"risk panel failed: {exc}"]}

    panels.append(html.Div([
        html.H3("Equity curve"),
        dcc.Graph(id="pa-equity", figure=_equity_figure(curve),
                  config={"displayModeBar": False}),
        html.H3("Under water (drawdown)"),
        dcc.Graph(id="pa-underwater", figure=_underwater_figure(curve, rep),
                  config={"displayModeBar": False}),
        _warnings_block(curve.get("warnings")),
    ], className="panel"))

    panels.append(html.Div([
        html.H3("Risk & performance"),
        html.Div(_risk_kpis(rep), className="kpi-row"),
        html.H3("Stress scenarios"),
        _stress_table(rep),
        _warnings_block(rep.get("warnings")),
    ], className="panel"))

    # -- Cash flows: deposits/withdrawals + the return basis -------------- #
    panels.append(html.Div(
        _cash_flow_panel(curve, rep), className="panel"))

    # -- Phase 3: exposure ---------------------------------------------- #
    try:
        exp = _data.exposure_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("exposure panel failed: %s", exc)
        exp = {"warnings": [f"exposure panel failed: {exc}"]}

    panels.append(html.Div([
        html.H3("Exposure & concentration"),
        html.Div(_exposure_kpis(exp), className="kpi-row"),
        dcc.Graph(id="pa-sectors", figure=_sector_figure(exp),
                  config={"displayModeBar": False}),
        _warnings_block(exp.get("warnings")),
    ], className="panel"))

    # -- Phase 3: attribution ------------------------------------------- #
    try:
        attr = _data.attribution_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("attribution panel failed: %s", exc)
        attr = {"warnings": [f"attribution panel failed: {exc}"]}

    panels.append(html.Div([
        html.H3("P&L attribution — by sleeve"),
        _attribution_table(attr),
        html.H3("P&L by session (IST)"),
        dcc.Graph(id="pa-buckets", figure=_bucket_figure(attr),
                  config={"displayModeBar": False}),
        _reconciliation_note(attr),
        _warnings_block(attr.get("warnings")),
    ], className="panel"))

    # -- Phase 4: policy ------------------------------------------------ #
    try:
        pol = _data.policy_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("policy panel failed: %s", exc)
        pol = {"action": "UNKNOWN", "warnings": [f"policy panel failed: {exc}"]}
    panels.append(html.Div([html.H3("Policy & kill-switch")]
                           + _policy_panel(pol), className="panel"))

    # -- Phase 5: views ------------------------------------------------- #
    try:
        views = _data.views_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("views panel failed: %s", exc)
        views = {"views": [], "warnings": [f"views panel failed: {exc}"]}
    panels.append(html.Div([
        html.H3("Views — stated vs allowed"),
        html.Div("Views are advisory. The 'allowed' column is what survived "
                 "confidence discounting and the tilt cap; the gap between the "
                 "two columns is the risk layer doing its job.",
                 style={"color": _SLATE, "fontSize": "12px"}),
        _views_table(views),
        _warnings_block(views.get("warnings")),
    ], className="panel"))

    # -- AI Insight header (read-only summary card) ---------------------- #
    # Inserted at the top (after the H2 + intro paragraph) so the operator
    # sees the summary first. Built from the Phase-2 report + Phase-3
    # snapshots; anything missing degrades to a grey dash, never a crash.
    summary = (rep.get("performance") or {}).get("summary") or {}
    sharpe = summary.get("sharpe")
    max_dd = summary.get("max_drawdown")
    ai_rows = [
        ("Sharpe", "-" if sharpe is None else f"{sharpe:.2f}", _SLATE),
        ("Max drawdown",
         "-" if max_dd is None else f"{max_dd:.1%}",
         _RED if (max_dd or 0) > 0.15 else _SLATE),
        ("Sectors", str(len((exp or {}).get("sectors") or []))
         if isinstance(exp, dict) else "-", _SLATE),
        ("P&L sleeves", str(len((attr or {}).get("sleeves") or []))
         if isinstance(attr, dict) else "-", _SLATE),
    ]

    panels.insert(2, ai_insight_card(
        "Portfolio Analytics Setting - risk & performance",
        ai_rows,
        note="Read-only. Every number comes from the persisted NAV/report "
             "artefacts; nothing is recomputed and no order is placed.",
    ))

    return panels


def layout(**_kwargs):
    return html.Div(
        [
            html.Div(id="analytics-portfolio-out", children=body()),
            dcc.Interval(id="analytics-portfolio-refresh", interval=15000,
                         n_intervals=0),
        ],
        className="page",
    )