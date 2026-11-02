"""Read-only dashboard for sector relative strength and allocation limits."""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional

import dash
import plotly.graph_objects as go
from dash import Input, Output, dcc, html

from blue_engine.dashboard.pages._ai_insight import (
    _AMBER, _GREEN, _GREY, ai_insight_card,
)
from blue_engine.dashboard.ui.kpi import KpiCard, KpiRow
from blue_engine.dashboard.ui.panel import Panel
from blue_engine.dashboard.ui.plotly_theme import dashboard_dark_template
from blue_engine.sectors.sector_engine import _DEFAULT_SNAPSHOT_PATH

logger = logging.getLogger(__name__)

dash.register_page(__name__, path="/sectors", name="Sectors", order=6)


def _read_snapshot(path: Optional[Path] = None) -> tuple[Dict[str, Any], str]:
    snapshot_path = Path(
        path or os.getenv("SECTOR_SNAPSHOT_PATH", _DEFAULT_SNAPSHOT_PATH))
    try:
        with snapshot_path.open("r", encoding="utf-8") as stream:
            snapshot = json.load(stream)
    except FileNotFoundError:
        return {}, "No sector-engine snapshot yet. Run the engine with a market-data snapshot."
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("sector dashboard snapshot unavailable (%s)", type(exc).__name__)
        return {}, "Sector snapshot could not be read; check the engine output path and JSON."
    if not isinstance(snapshot, dict):
        return {}, "Sector snapshot has an invalid structure."
    return snapshot, ""


def _sector_figure(snapshot: Dict[str, Any]) -> go.Figure:
    matrix = snapshot.get("sector_matrix")
    fig = go.Figure()
    if isinstance(matrix, list):
        ranked = []
        for row in matrix:
            if not isinstance(row, dict) or not isinstance(row.get("history"), list):
                continue
            try:
                score = float(row.get("relative_strength_score"))
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(score):
                ranked.append((score, row))
        ranked = [row for _, row in sorted(
            ranked, key=lambda item: item[0], reverse=True)[:5]]
        for row in ranked:
            try:
                history = [float(value) for value in row["history"]]
            except (TypeError, ValueError, OverflowError):
                continue
            if not all(math.isfinite(value) for value in history):
                continue
            if history:
                fig.add_trace(go.Scatter(
                    x=list(range(1, len(history) + 1)),
                    y=history,
                    mode="lines",
                    name=str(row.get("sector", "Unknown")),
                ))
    if not fig.data:
        fig.add_annotation(
            text="No sector-index history available",
            showarrow=False,
            font={"color": _GREY},
        )
    fig.update_layout(
        template=dashboard_dark_template(),
        height=360,
        margin={"l": 45, "r": 20, "t": 20, "b": 45},
        xaxis_title="Observation (oldest to newest)",
        yaxis_title="Sector / benchmark close ratio",
        legend={"orientation": "h", "y": -0.2},
        hovermode="x unified",
    )
    return fig


def body() -> list:
    snapshot, warning = _read_snapshot()
    matrix = snapshot.get("sector_matrix") or []
    limits = snapshot.get("limits") or {}
    if not isinstance(matrix, list):
        matrix = []
    if not isinstance(limits, dict):
        limits = {}
    sectors_count = len(matrix)
    invest = limits.get("invest_more_sectors") or []
    wait = limits.get("wait_and_see_sectors") or []
    method = str(snapshot.get("advisor_method", "unavailable"))
    classification = str(snapshot.get("data_classification", "unknown")).upper()
    timestamp = str(snapshot.get("generated_at", "not available"))
    status_colour = _GREEN if method in ("openrouter", "ollama") else _AMBER

    components = [
        ai_insight_card(
            "Sector Engine - Mansfield relative strength",
            [
                ("Input classification", classification, _GREY),
                ("Advisor", method, status_colour),
                ("Sectors evaluated", str(sectors_count), _GREY),
                ("Invest more / wait", f"{len(invest)} / {len(wait)}",
                 status_colour if invest else _AMBER),
                ("Lookback window",
                 str(limits.get("dynamic_lookback_window", 14)) + " sessions",
                 _GREY),
            ],
            note=(
                "Read-only sector research view. Recommendations are not orders "
                "or verified live data. Generated: " + timestamp
            ),
            status="loaded" if snapshot else "awaiting data",
            status_color=status_colour if snapshot else _GREY,
        ),
        html.H2("Sector Relative Strength", style={"marginTop": 0}),
    ]
    if warning:
        components.append(html.Div(
            warning, role="status",
            style={"color": _AMBER, "marginBottom": "12px"},
        ))
    components.extend([
        KpiRow([
            KpiCard("Benchmark", str(snapshot.get("benchmark", "-"))),
            KpiCard("Data classification", classification),
            KpiCard("Advisor", method),
            KpiCard("Lookback", f"{limits.get('dynamic_lookback_window', 14)} sessions"),
        ]),
        Panel(
            "Top five sector indices under review",
            dcc.Graph(
                id="sector-relative-strength-chart",
                figure=_sector_figure(snapshot),
                config={"displayModeBar": False},
            ),
            icon="mdi:chart-line",
            description="Mansfield relative-strength ratio histories, ranked by current score.",
        ),
    ])
    snapshot_warnings = snapshot.get("warnings")
    if isinstance(snapshot_warnings, list) and snapshot_warnings:
        components.append(Panel(
            "Engine warnings",
            html.Ul([html.Li(str(item)) for item in snapshot_warnings]),
            icon="mdi:alert-outline",
        ))
    return components


def layout(**_kwargs):
    return html.Div(
        [
            html.Div(id="sector-engine-content", children=body()),
            dcc.Interval(id="sector-engine-refresh", interval=15000, n_intervals=0),
        ],
        className="page",
    )


@dash.callback(
    Output("sector-engine-content", "children"),
    Input("sector-engine-refresh", "n_intervals"),
)
def _refresh(_n_intervals: int):
    return body()
