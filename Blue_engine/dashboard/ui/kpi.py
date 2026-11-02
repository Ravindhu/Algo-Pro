"""
kpi.py - the KPI tile, in one place.

Replaces the eight hand-written copies of "a card with a label and a value"
that lived in terminal, portfolio, risk_control, analytics, infrastructure,
ml_models, optimizer and analytics_portfolio. Each of those built the same
three nested ``html.Div``s with the same class names, which meant changing the
look of a KPI required editing eight files -- and in practice they had already
drifted apart.
"""
from __future__ import annotations

from typing import Any, Optional

import dash_mantine_components as dmc
from dash import html
from dash_iconify import DashIconify

#: Semantic colours. Kept as plain hex (not DMC theme tokens) because the
#: pages pass P&L sign colours that must not shift with the active accent.
POSITIVE = "#40C057"
NEGATIVE = "#FA5252"
NEUTRAL = "#909296"
WARNING = "#FCC419"
INFO = "#4DABF7"
ACCENT = "#a855f7"


def pnl_colour(value: Optional[float]) -> str:
    """Green for profit, red for loss, neutral for zero/None.

    Lives here (not in each page) because portfolio, analytics_portfolio and
    portfolio-analytics all render P&L and must agree on the sign convention.
    """
    if value is None:
        return NEUTRAL
    try:
        v = float(value)
    except (TypeError, ValueError):
        return NEUTRAL
    if v > 0:
        return POSITIVE
    if v < 0:
        return NEGATIVE
    return NEUTRAL


def KpiCard(label: str, value: Any, *, colour: Optional[str] = None,
            note: Optional[str] = None, icon: Optional[str] = None) -> dmc.Card:
    """A single KPI tile.

    Args:
        label: the small uppercase caption.
        value: already-formatted string (callers use ``_data.fmt`` so numbers
            format consistently with the tables).
        colour: override the value colour; default follows the KPI palette.
        note: optional muted sub-line (ml_models / analytics_portfolio use it
            to show a gate reason under the number).
        icon: optional MDI icon name.
    """
    heading = dmc.Group(
        [
            DashIconify(icon=icon, width=15, color="dimmed") if icon else None,
            dmc.Text(label, size="xs", c="dimmed", tt="uppercase", fw=600,
                     style={"letterSpacing": ".05em"}),
        ],
        gap=6,
    )
    body = [
        dmc.Text(str(value), fz=22, fw=700,
                 c=colour or None,
                 style={"marginTop": 2}),
    ]
    if note:
        body.append(dmc.Text(note, size="xs", c="dimmed"))
    return dmc.Card(
        [heading, *body],
        withBorder=True,
        radius="md",
        shadow="sm",
        padding="sm",
        className="kpi-card",
        style={"minWidth": 150},
    )


def KpiRow(cards: list) -> dmc.SimpleGrid:
    """A responsive row of KPI cards.

    SimpleGrid (not Group) so the tiles reflow on a narrow window instead of
    overflowing -- the old flex row with ``flex-wrap`` did reflow, but the
    fixed ``min-width: 150px`` in CSS made the last card clip at some widths.
    """
    return dmc.SimpleGrid(
        cols={"base": 1, "sm": 2, "md": 3, "lg": 4},
        spacing="md",
        children=[c for c in cards if c is not None],
        className="kpi-row",
    )


def pill(text: str, *, ok: Optional[bool] = None,
         colour: Optional[str] = None) -> dmc.Badge:
    """A small status badge.

    ``ok=True`` -> green, ``ok=False`` -> red, ``ok=None`` -> neutral grey
    (used for "unknown", which must not look like a pass).
    """
    if colour is None:
        if ok is True:
            colour = "green"
        elif ok is False:
            colour = "red"
        else:
            colour = "gray"
    return dmc.Badge(str(text), color=colour, variant="dot", size="sm",
                     radius="xl", className="pill")


__all__ = [
    "ACCENT", "INFO", "KpiCard", "KpiRow", "NEGATIVE", "NEUTRAL",
    "POSITIVE", "WARNING", "pill", "pnl_colour",
]
