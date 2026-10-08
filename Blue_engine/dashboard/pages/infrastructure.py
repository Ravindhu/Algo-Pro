"""
infrastructure.py - Page 5: System Health & Raw Logs.

The operational view: is the config readable, is the ledger available,
how many symbols is the engine watching, when did the last quote arrive,
and the raw alert/log tail. No trading logic lives here.

Migrated to the shared DMC kit: ``ui.log_view`` owns the log/tail rendering
(so it follows the mode theme) and ``DataTable`` owns the health grid.
Read-only.
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
from blue_engine.dashboard.ui.kpi import KpiCard, KpiRow, NEGATIVE, POSITIVE, pill
from blue_engine.dashboard.ui.log_view import LogView, format_alerts
from blue_engine.dashboard.ui.panel import Panel
from blue_engine.dashboard.ui.table import DataTable
from blue_engine.dashboard.ui import service_control

dash.register_page(__name__, path="/infra", name="Infrastructure", order=4)


def _render_health(_value, row):
    """A real badge instead of a coloured <span>."""
    return pill("OK" if row.get("ok") else "DOWN", ok=bool(row.get("ok")))


#: Declared AFTER _render_health so the ``render`` callable exists at class
#: definition time. A forward reference here would raise NameError on import.
_HEALTH_COLUMNS = [
    {"key": "component", "label": "Component", "bold": True},
    {"key": "status", "label": "Status", "render": _render_health},
    {"key": "detail", "label": "Detail", "mono": True},
]


def _health_rows(cfg_ok, cfg_path, ledger_ok, engine, n_symbols, last_quote):
    return [
        {"component": "Config file", "ok": cfg_ok, "detail": str(cfg_path)},
        {"component": "Ledger", "ok": ledger_ok,
         "detail": "blue_engine.core.ledger"},
        {"component": "Live engine", "ok": engine is not None,
         "detail": (f"{n_symbols} symbols watched" if engine is not None
                    else "not attached")},
        {"component": "Last quote", "ok": last_quote != "-",
         "detail": f"at {last_quote}"},
    ]


def body():
    """Infrastructure page content (without the outer wrapper)."""
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

    health = _health_rows(cfg_ok, cfg_path, snap.ledger_ok, engine,
                          n_symbols, last_quote)

    # --- AI Insight header (read-only summary card) ------------------------ #
    n_alerts = len(snap.alerts)
    n_crit = sum(1 for a in snap.alerts
                 if str(a.get("severity", "")).lower() in ("critical", "error"))
    ai_rows = [
        ("Config", "loaded" if cfg_ok else "missing",
         _AI_GREEN if cfg_ok else _AI_RED),
        ("Ledger", "available" if snap.ledger_ok else "offline",
         _AI_GREEN if snap.ledger_ok else _AI_RED),
        ("Engine attached", "yes" if engine is not None else "no",
         _AI_GREEN if engine is not None else _AI_GREY),
        ("Symbols watched", str(n_symbols), _AI_GREY),
        ("Last quote", last_quote, _AI_GREY),
        ("Alerts (total / critical)", f"{n_alerts} / {n_crit}",
         _AI_RED if n_crit else _AI_GREEN),
    ]

    return [
        ai_insight_card(
            "Infrastructure Setting - system health",
            ai_rows,
            note="Operational view only. One failed component degrades a "
                 "panel, never the page.",
        ),
        html.H2("Infrastructure - System Health & Logs", style={"marginTop": 0}),
        KpiRow([
            KpiCard("Config", "loaded" if cfg_ok else "missing",
                    colour=POSITIVE if cfg_ok else NEGATIVE,
                    icon="mdi:file-cog-outline"),
            KpiCard("Ledger", "available" if snap.ledger_ok else "offline",
                    colour=POSITIVE if snap.ledger_ok else NEGATIVE,
                    icon="mdi:database-outline"),
            KpiCard("Symbols", n_symbols, icon="mdi:format-list-numbered"),
            KpiCard("Snapshot age",
                    f"{max(0.0, time.time() - snap.fetched_at):.1f}s",
                    icon="mdi:timer-outline"),
        ]),
        Panel("Health checks",
              DataTable(_HEALTH_COLUMNS, health,
                        empty="No health checks.",
                        highlight_on_hover=False),
              icon="mdi:heart-pulse"),
        # Service control: read-only probes + a guarded OpenAlgo START.
        # Rendered top-level (not inside Panel) because it brings its own card
        # chrome and its own 5 s poll interval. See ui/service_control.py for
        # why there is no Stop button and no Blue Engine button.
        service_control.control_panel(),
        Panel("Raw log tail",
              LogView(format_alerts(snap.alerts), empty="No log/alert entries yet."),
              icon="mdi:text-box-outline"),
        Panel("Config (raw)",
              LogView(_render_raw_config(snap.config), empty="(no config)"),
              icon="mdi:code-json"),
    ]


def layout(**_kwargs):
    """Full page: the refreshable body plus its polling Interval."""
    return html.Div(
        [
            html.Div(id="infra-refresh-out", children=body()),
            dcc.Interval(id="infra-refresh", interval=5000, n_intervals=0),
        ],
        className="page",
    )


def _render_raw_config(cfg: dict) -> str:
    """Pretty-print the config with secrets masked.

    Kept last so the masking rule sits next to nothing else -- it is the one
    function here that must not be copy-pasted without its mask loop.
    """
    import json
    import re

    if not cfg:
        return "(no config)"
    try:
        text = json.dumps(cfg, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(cfg)
    for secret in ("api_key", "apikey", "secret", "password", "token"):
        if secret in text.lower():
            text = re.sub(
                rf'("{secret}"\s*:\s*)"[^"]*"', r'\1"********"',
                text, flags=re.IGNORECASE,
            )
    return text
