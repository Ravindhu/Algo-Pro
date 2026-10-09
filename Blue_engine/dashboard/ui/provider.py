"""
provider.py - the DMC application shell.

Replaces the hand-rolled ``app.layout`` + ``_THEME_CSS`` of the native Dash
build. Two jobs:

1. Wrap every page in ``dmc.MantineProvider`` so DMC components get a real
   theme (colours, radius, fonts) instead of falling back to defaults.

2. Drive that theme from the OpenAlgo mode, because the mode is a SAFETY
   SIGNAL, not a preference: dark == analyzer/simulated, light == live/real
   money. The mode is read from the same ``/pov/mode`` endpoint the old JS
   poll used, so a mid-session flip in OpenAlgo still re-themes the UI
   without a page reload.

The banner is rendered server-side (initial paint is already correct) and is
then kept in sync by the small client script in ``index_html``. That split
matters: a user who opens the terminal while OpenAlgo is in LIVE mode must see
"LIVE - REAL MONEY" on the FIRST frame, not 2 seconds later.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import dash_mantine_components as dmc
from dash import dcc, html
from dash_iconify import DashIconify

from blue_engine.dashboard.ui.mode import banner_text, theme

#: Nav entries: (icon, label, href). Order defines sidebar order and must stay
#: consistent with each page's ``order=`` in ``dash.register_page``.
_NAV: list[tuple[str, str, str]] = [
    ("mdi:chart-timeline-variant", "Terminal", "/"),
    ("mdi:briefcase-outline", "Portfolio", "/portfolio"),
    ("mdi:chart-areaspline", "Portfolio Analytics", "/analytics-portfolio"),
    ("mdi:shield-alert-outline", "Risk Control", "/risk"),
    ("mdi:sigma", "Analytics", "/analytics"),
    ("mdi:filter-variant", "Screener", "/screener"),
    ("mdi:earth", "Economic", "/economic"),
    ("mdi:server-network", "Infrastructure", "/infra"),
    ("mdi:brain", "ML Models", "/ml"),
    ("mdi:tune-variant", "Optimizer", "/optimizer"),
]


def mode_banner(pal: Dict[str, Any], *, data_source: str = "paper") -> html.Div:
    """The always-visible banner: order-routing mode AND panel data source.

    Rendered on the server from the initial mode read. ``data-mode`` on <body>
    lets the client script restyle the shell on a mid-session flip; the span
    text is what the client rewrites, so keep exactly one ``.mode-banner > span``.

    ``data_source`` is the DASHBOARD's own wiring flag (paper = stubbed reads,
    live = real candles/quotes). It is deliberately a separate argument from the
    OpenAlgo mode in ``pal``, because the two answer different questions --
    see ``ui/mode.py``. The client-mode poll only refreshes the OpenAlgo half;
    the panel half is fixed for the process lifetime, which is correct (you
    cannot re-wire the data sources without a restart).
    """
    return html.Div(
        # Two spans: the mode (rewritten live by the poll) and the panel data
        # source (static for this process). Splitting them is what lets the
        # client script update one without touching the other.
        html.Span(banner_text(pal, data_source=data_source),
                  className="mode-banner-text"),
        className="mode-banner",
        style={
            "background": pal["banner_bg"],
            "color": pal["banner_fg"],
            "border": f"1px solid {pal['banner_border']}",
        },
    )


def _sidebar(pal: Dict[str, Any]) -> dmc.Stack:
    """The left nav, built from real DMC components (no CSS class needed)."""
    links = [
        dcc.Link(
            dmc.Group(
                [
                    DashIconify(icon=icon, width=18),
                    dmc.Text(label, size="sm", fw=500),
                ],
                gap="sm",
            ),
            href=href,
            className="nav-link",
        )
        for icon, label, href in _NAV
    ]
    return dmc.Stack(
        [
            dmc.Group(
                [
                    DashIconify(icon="mdi:robot-industrial", width=22,
                                color=pal["muted_fg"]),
                    dmc.Text("POV Terminal", size="sm", fw=700,
                             style={"letterSpacing": ".08em",
                                    "textTransform": "uppercase"}),
                ],
                gap="xs",
            ),
            dmc.Divider(),
            dmc.Stack(links, gap=2),
        ],
        gap="sm",
        className="sidebar",
    )


def app_shell(page_container: Any, *, mode: str = "unknown",
              embedded: bool = False,
              data_source: str = "paper") -> dmc.MantineProvider:
    """Build the whole application shell.

    Args:
        page_container: ``dash.page_container`` (Dash Pages injects the page).
        mode: the OpenAlgo mode string read at startup.
        embedded: when True the sidebar is hidden -- the app is loaded inside
            OpenAlgo's own UI iframe (``?embed=1``) and OpenAlgo supplies the
            chrome.
        data_source: the DASHBOARD's own market-data wiring -- ``"paper"`` when
            its reads are stubbed, ``"live"`` when wired to the SDK. Separate
            from ``mode`` on purpose; see ``ui/mode.py``.

    Returns:
        A ``MantineProvider`` wrapping banner + sidebar + content.
    """
    pal = theme(mode)
    return dmc.MantineProvider(
        id="mantine-provider",
        # Broadcast the palette to every DMC component AND to plain CSS
        # (the .mode-banner) without a hard-coded stylesheet.
        theme={
            "primaryColor": pal["accent"],
            "fontFamily": "'Segoe UI', system-ui, sans-serif",
            "defaultRadius": "md",
        },
        # forceColorScheme pins dark/light to the mode. Without it DMC would
        # follow the OS preference and a user in a dark-themed OS would get a
        # dark UI while trading live -- the exact confusion we must prevent.
        forceColorScheme=pal["color_scheme"],
        children=html.Div(
            [
                mode_banner(pal, data_source=data_source),
                html.Div(
                    [
                        _sidebar(pal),
                        html.Div(page_container, className="content"),
                    ],
                    className="app-body embedded" if embedded else "app-body",
                ),
                # The poll that keeps the banner/banner honest mid-session.
                dcc.Interval(id="mode-poll", interval=2000, n_intervals=0),
                html.Div(id="mode-poll-target", style={"display": "none"}),
            ],
            className="app-shell",
        ),
    )


__all__ = ["app_shell", "mode_banner"]
