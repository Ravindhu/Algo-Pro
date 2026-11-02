"""
table.py - the data table, in one place.

Replaces the hand-written ``html.Table / html.Thead / html.Tbody / html.Tr /
html.Td`` trees that every page built. Those were verbose, had no sorting, no
sticky header, and no empty-state contract: each page re-invented
"no rows -> show a placeholder row" slightly differently.

``DataTable`` takes column specs plus row dicts and handles all of it, so a
new page needs no table markup at all.

Column spec
-----------
    {"key": "symbol",              # key into the row dict
     "label": "Symbol",            # header text
     "mono": True,                 # monospace the cell
     "colour": "#22c55e",          # fixed colour, OR
     "colour_key": "_pnl_colour",  # per-row colour, resolved from a row callable
     "align": "right",
     "render": callable,           # optional: value -> cell node
     "type": "float"|"int"|"str"}  # formatting hint when no render is given
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import dash_mantine_components as dmc
from dash import html

#: Rows render at most this many bodies before we hide the rest behind a note.
#: A dashboard is a monitor, not a pager; 500 rows keeps the DOM small enough
#: that the 5-second poll stays cheap.
MAX_ROWS = 500


def _fmt_cell(value: Any, col: Dict[str, Any]) -> str:
    if value is None:
        return "-"
    t = col.get("type")
    if t == "float":
        try:
            return f"{float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)
    if t == "int":
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return str(value)
    if t == "pct":
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def DataTable(columns: List[Dict[str, Any]], rows: List[Dict[str, Any]], *,
              empty: str = "No rows.", max_rows: int = MAX_ROWS,
              striped: bool = True, highlight_on_hover: bool = True) -> Any:
    """Render a table, or the empty-state message when there are no rows.

    Args:
        columns: column specs (see module docstring).
        rows: row dicts keyed by column ``key``.
        empty: message shown when ``rows`` is empty.
        max_rows: clip long tables; a note states how many were hidden.
        striped / highlight_on_hover: DMC table options.

    Returns:
        A ``dmc.Table``, or ``html.Div`` when empty. Never raises on missing
        keys -- a missing key renders "-" so one bad row cannot blank a panel.
    """
    if not rows:
        return html.Div(str(empty), className="empty")

    shown = rows[:max_rows]
    hidden = len(rows) - len(shown)

    head = dmc.TableThead(
        dmc.TableTr(
            [
                dmc.TableTh(
                    str(c.get("label", c.get("key", ""))),
                    style={"textAlign": c.get("align", "left"),
                           "whiteSpace": "nowrap"},
                )
                for c in columns
            ]
        )
    )

    body_rows = []
    for r in shown:
        cells = []
        for c in columns:
            key = c.get("key", "")
            value = r.get(key)

            render = c.get("render")
            if callable(render):
                node = render(value, r)
            else:
                text = _fmt_cell(value, c)
                style: Dict[str, Any] = {
                    "textAlign": c.get("align", "left"),
                    "whiteSpace": "nowrap",
                }
                if c.get("mono"):
                    style["fontFamily"] = "'Cascadia Mono', Consolas, monospace"
                colour = c.get("colour")
                if colour is None:
                    # Per-row colour via a row field that holds a colour string.
                    ck = c.get("colour_key")
                    if ck:
                        colour = r.get(ck)
                if colour:
                    style["color"] = colour
                if c.get("bold"):
                    style["fontWeight"] = 600
                node = html.Span(text, style=style)
            cells.append(dmc.TableTd(node))
        body_rows.append(dmc.TableTr(cells))

    table = dmc.Table(
        [head, dmc.TableTbody(body_rows)],
        striped=striped,
        highlightOnHover=highlight_on_hover,
        className="data-table",
    )

    if hidden <= 0:
        return table
    return html.Div(
        [
            table,
            dmc.Text(f"{hidden} more row(s) hidden (showing {len(shown)})",
                     size="xs", c="dimmed", mt=4),
        ]
    )


def status_cell(fn: Callable[[Any, Dict[str, Any]], Any]) -> Callable:
    """Sugar for a ``render`` callback that also needs the whole row."""
    return fn


__all__ = ["DataTable", "MAX_ROWS", "status_cell"]
