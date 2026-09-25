"""
infrastructure.py - Page 5: System Health & Raw Logs.

The operational view: is the config readable, is the ledger available,
how many symbols is the engine watching, when did the last quote arrive,
and the raw alert/log tail. No trading logic lives here.
"""
from __future__ import annotations

import time

import dash
from dash import dcc, html

from blue_engine.dashboard.pages import _data

dash.register_page(__name__, path="/infra", name="Infrastructure", order=4)


def _health_row(name: str, ok: bool, detail: str) -> html.Tr:
    return html.Tr(
        [
            html.Td(name, style={"fontWeight": 600}),
            html.Td(
                html.Span(
                    "OK" if ok else "DOWN",
                    className="pill",
                    style={
                        "background": "#14532d" if ok else "#7f1d1d",
                        "color": "#dcfce7" if ok else "#fee2e2",
                    },
                )
            ),
            html.Td(detail, className="mono", style={"color": "#94a3b8"}),
        ]
    )


def _log_lines(alerts) -> html.Pre:
    if not alerts:
        return html.Div("No log/alert entries yet.", className="empty")
    lines = []
    for a in alerts[-200:]:
        ts = a.get("ts") or 0
        when = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
        sev = str(a.get("severity", "info")).upper()
        lines.append(f"{when}  {sev:<5}  {a.get('message','')}")
    return html.Pre("\n".join(lines), className="log-pre")


def body():
    # Infrastructure page content (without the outer wrapper).
    snap = _data.gather()
    engine = snap.live_engine

    cfg_path = _data.config_path()
    cfg_ok = bool(snap.config)
    last_quote = "-"
    n_symbols = 0
    if engine is not None:
        try:
            n_symbols = len(engine._machines)  # noqa: SLF001
            lq = engine._last_quote_at  # noqa: SLF001
            if lq:
                last_quote = time.strftime("%H:%M:%S", time.localtime(lq))
        except Exception:
            pass

    health = [
        _health_row("Config file", cfg_ok, str(cfg_path)),
        _health_row("Ledger", snap.ledger_ok, "blue_engine.core.ledger"),
        _health_row("Live engine", engine is not None,
                    f"{n_symbols} symbols watched" if engine is not None else "not attached"),
        _health_row("Last quote", last_quote != "-", f"at {last_quote}"),
    ]

    return [
            html.H2("Infrastructure - System Health & Logs"),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("Config", className="kpi-label"),
                            html.Div("loaded" if cfg_ok else "missing", className="kpi-value",
                                     style={"color": "#22c55e" if cfg_ok else "#ef4444"}),
                        ],
                        className="kpi-card",
                    ),
                    html.Div(
                        [
                            html.Div("Ledger", className="kpi-label"),
                            html.Div("available" if snap.ledger_ok else "offline", className="kpi-value",
                                     style={"color": "#22c55e" if snap.ledger_ok else "#ef4444"}),
                        ],
                        className="kpi-card",
                    ),
                    html.Div(
                        [
                            html.Div("Symbols", className="kpi-label"),
                            html.Div(str(n_symbols), className="kpi-value"),
                        ],
                        className="kpi-card",
                    ),
                    html.Div(
                        [
                            html.Div("Snapshot age", className="kpi-label"),
                            html.Div(f"{max(0.0, time.time() - snap.fetched_at):.1f}s",
                                     className="kpi-value"),
                        ],
                        className="kpi-card",
                    ),
                ],
                className="kpi-row",
            ),
            html.Div(
                [
                    html.H3("Health checks"),
                    html.Table(
                        [
                            html.Thead(html.Tr([html.Th("Component"), html.Th("Status"), html.Th("Detail")])),
                            html.Tbody(health),
                        ],
                        className="data-table",
                    ),
                ],
                className="panel",
            ),
            html.Div([html.H3("Raw log tail"), _log_lines(snap.alerts)], className="panel"),
            html.Div(
                [
                    html.H3("Config (raw)"),
                    html.Pre(
                        _render_raw_config(snap.config),
                        className="log-pre",
                    ),
                ],
                className="panel",
            ),
    ]


def layout(**_kwargs):
    # Full page: the refreshable body plus its polling Interval.
    return html.Div(
        [
            html.Div(id="infra-refresh-out", children=body()),
            dcc.Interval(id="infra-refresh", interval=5000, n_intervals=0),
        ],
        className="page",
    )


def _render_raw_config(cfg: dict) -> str:
    import json

    if not cfg:
        return "(no config)"
    try:
        text = json.dumps(cfg, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(cfg)
    # Mask obvious secrets before printing.
    for secret in ("api_key", "apikey", "secret", "password", "token"):
        if secret in text.lower():
            # crude but effective: replace the value following the key
            import re

            text = re.sub(
                rf'("{secret}"\s*:\s*)"[^"]*"', r'\1"********"', text, flags=re.IGNORECASE
            )
    return text
