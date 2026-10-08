"""
panel.py - the titled surface, in one place.

Every page built the same ``html.Div([html.H3(title), ...], className="panel")``
by hand -- nine files, one pattern. This is that pattern as a component, with
an optional right-aligned action slot (used by the optimizer page's study
picker and the risk page's guard count).
"""
from __future__ import annotations

from typing import Any, Optional

import dash_mantine_components as dmc


def Panel(title: Optional[str] = None, children: Any = None, *,
          right: Any = None, icon: Optional[str] = None,
          description: Optional[str] = None,
          className: str = "panel") -> dmc.Paper:
    """A bordered surface with an optional title row.

    Args:
        title: the panel heading. Omit for an untitled surface.
        children: the panel body.
        right: optional right-aligned node in the header row (badges, selects).
        icon: optional MDI icon shown before the title.
        description: optional muted line under the title.
        className: kept as ``panel`` so any residual CSS still applies.
    """
    body: list = []
    if title is not None or right is not None:
        heading: list = []
        if icon:
            from dash_iconify import DashIconify

            heading.append(DashIconify(icon=icon, width=16, color="dimmed"))
        if title is not None:
            heading.append(
                dmc.Text(str(title), size="sm", fw=700, tt="uppercase",
                         style={"letterSpacing": ".05em"})
            )
        body.append(
            dmc.Group(
                [
                    dmc.Group(heading, gap="xs") if heading else dmc.Box(),
                    right if right is not None else dmc.Box(),
                ],
                justify="space-between",
                align="center",
                mb="xs",
            )
        )
    if description:
        body.append(dmc.Text(description, size="xs", c="dimmed", mb="sm"))
    if isinstance(children, list):
        body.extend(children)
    else:
        body.append(children)

    return dmc.Paper(body, withBorder=True, radius="md", p="md",
                     className=className, mb="md")


__all__ = ["Panel"]
