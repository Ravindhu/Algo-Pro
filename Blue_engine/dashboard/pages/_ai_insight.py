"""
_ai_insight.py - the shared AI Insight header for ALL dashboard pages.

WHY THIS FILE EXISTS
====================
The audit chain (AGENTS.md, "AI Insight header in each module") requires
that every engine page carries a READ-ONLY AI Insight summary card at the
very top. Phase 5 shipped this for the Optimizer page
(``_optimizer_header.py``). This file generalises that pattern so every
page gets the same card without duplicating the helper functions.

WHAT IT SHOWS
-------------
Each page provides a list of (label, value, colour) rows. This module
wraps them in the standard card:

  * The green "AI INSIGHT" badge (identifiable in screenshots).
  * The page title line.
  * A table of the page-specific rows.
  * An honest grey footer: read-only, never writes to disk.

READ-ONLY CONTRACT (LOCKED)
---------------------------
Same contract as the Optimizer header:

  * No callback in this file. The header is a pure function of its
    inputs.
  * No "write" anywhere in this file - no disk, no config, no journal.
  * Missing data = honest placeholder, never a crash, never a fake
    number.

HOW PAGES USE IT
----------------
``from .pages._ai_insight import ai_insight_card`` then call it with the
page's own rows. The card has a green left border (mirrors the Optimizer
header's ``borderLeft: 3px solid _GREEN``) so the operator recognises it
as an AI Insight region at a glance.

Tests live in ``blue_engine/tests/test_ai_insight_all_pages.py``.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional, Tuple

from dash import html

__all__ = ["ai_insight_card", "html"]

# --- palette (mirrors _optimizer_header.py / theme.py) ---------------------- #
_GREEN = "#22c55e"
_AMBER = "#f59e0b"
_RED = "#ef4444"
_GREY = "#94a3b8"
_BG_DARK = "#0f172a"
_BG_PANEL = "#1e293b"

# --- row-style helpers ------------------------------------------------------ #


def _label_style() -> Dict[str, Any]:
    return {"color": _GREY, "fontSize": "12px", "padding": "2px 10px 2px 0",
            "whiteSpace": "nowrap", "verticalAlign": "top"}


def _value_style(color: str = "#e2e8f0") -> Dict[str, Any]:
    return {"color": color, "fontSize": "12px", "padding": "2px 0",
            "verticalAlign": "top"}


def _th_style() -> Dict[str, Any]:
    return {"color": _GREY, "fontSize": "11px", "padding": "2px 10px 2px 0",
            "textAlign": "left", "whiteSpace": "nowrap",
            "borderBottom": "1px solid " + _BG_PANEL}


def _td_style(color: str = "#e2e8f0") -> Dict[str, Any]:
    return {"color": color, "fontSize": "12px", "padding": "2px 10px 2px 0",
            "verticalAlign": "top"}


# --------------------------------------------------------------------------- #
# The shared AI Insight card
# --------------------------------------------------------------------------- #


def ai_insight_card(page_title: str,
                    rows: List[Tuple[str, str, str]],
                    note: str = "",
                    status: str = "idle",
                    status_color: str = _GREY,
                    extra: Optional[html.Div] = None) -> html.Div:
    """The AI Insight card shared by every page.

    A READ-ONLY summary card at the top of a page. The card wraps the
    page's own (label, value, colour) rows in the standard AI Insight
    frame. The card has no callbacks - it is a pure function of its
    inputs.

    Parameters
    ----------
    page_title : str
        The one-line description of what this card summarises. Shown
        next to the green "AI INSIGHT" badge.
    rows : list of (label, value, colour)
        The page-specific rows. The colour is the token colour for
        the *value* - the label is always grey so the operator's eye
        reads label -> value -> colour at a glance.
    note : str
        Optional extra context line, rendered in grey below the table.
    status : str
        The one-word status shown in the footer (e.g. "idle",
        "loaded", "ok"). Honest - never fabricated.
    status_colour : str
        The token colour for the status word.
    extra : html.Div
        Optional extra content (e.g. the Optimizer page's parameter
        table), rendered below the table.
    """
    table_rows = []
    for label, value, colour in rows:
        table_rows.append(html.Tr([
            html.Td(label, style=_label_style()),
            html.Td(str(value), style=_value_style(colour)),
        ]))

    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    footer = html.P(
        f"Status: {status}.  Last render: {ts}.  "
        f"This card is read-only - it never writes to disk.",
        style={"color": _GREY, "fontSize": "11px", "marginTop": "12px",
               "marginBottom": "0"},
    )

    children = [
        html.Div([
            html.Span("AI INSIGHT", style={
                "background": _BG_PANEL, "color": _GREEN,
                "padding": "2px 8px", "borderRadius": "4px",
                "fontSize": "10px", "fontWeight": "600",
                "marginRight": "10px",
            }),
            html.Span(page_title, style={"color": _GREY, "fontSize": "12px"}),
        ], style={"marginBottom": "10px"}),
        html.Table(table_rows, style={
            "fontSize": "12px",
            "borderCollapse": "collapse",
            "width": "100%",
            "marginBottom": "12px",
        }),
    ]

    if note:
        children.append(html.P(
            note, style={"color": _GREY, "fontSize": "11px",
                         "marginBottom": "0"}))

    if extra is not None:
        children.append(extra)

    children.append(footer)

    return html.Div(children, className="panel",
                    style={"borderLeft": f"3px solid {_GREEN}"})


def rows_from_dict(data: Dict[str, Any],
                   status_colour: str = _GREY) -> List[Tuple[str, str, str]]:
    """Convert a flat dict to (label, value, colour) rows. The colour is
    applied to every value (the caller can pass a single colour for the
    whole dict - e.g. green for "ok", grey for "idle").
    """
    return [(k, str(v), status_colour) for k, v in (data or {}).items()]
