"""
provider.py - the DMC application shell.

Replaces the hand-rolled ``app.layout`` + ``_THEME_CSS`` of the native Dash
build. Two jobs:

1. Wrap every page in ``dmc.MantineProvider`` so DMC components get a real
   theme (colours, radius, fonts) instead of falling back to defaults.

2. Keep the workspace dark in all modes and drive the accent + status badges
   from OpenAlgo mode, which is a SAFETY SIGNAL, not a preference. The mode is
   read from the same ``/pov/mode`` endpoint the old JS
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
    ("mdi:chart-box-multiple-outline", "Sectors", "/sectors"),
    ("mdi:server-network", "Infrastructure", "/infra"),
    ("mdi:brain", "ML Models", "/ml"),
    ("mdi:tune-variant", "Optimizer", "/optimizer"),
    ("mdi:factory", "Agent Line", "/agent-line"),
]

_DARK_COLORS = [
    "#C1C2C5", "#A6A7AB", "#909296", "#5C5F66", "#3F4146",
    "#2C2E33", "#25262B", "#1A1B1E", "#141517", "#101113",
]


def _mantine_theme(pal: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "primaryColor": pal["accent"],
        "fontFamily": "'Segoe UI', system-ui, sans-serif",
        "defaultRadius": "md",
        "colors": {"dark": _DARK_COLORS},
    }


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
                  id="mode-banner-text", className="mode-banner-text"),
        id="mode-banner", className="mode-banner",
        style=_mode_banner_style(pal),
    )


def _mode_banner_style(pal: Dict[str, Any]) -> Dict[str, str]:
    return {
        "background": pal["banner_bg"],
        "color": pal["banner_fg"],
        "border": f"1px solid {pal['banner_border']}",
    }


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
            dmc.Card(
                dmc.Group(
                    [
                        dmc.ThemeIcon(
                            DashIconify(icon="mdi:chart-box-outline", width=20),
                            color=pal["accent"], variant="light", radius="md",
                            size="lg", id="sidebar-brand-icon",
                        ),
                        dmc.Stack(
                            [
                                dmc.Text("BLUE ENGINE", size="sm", fw=800,
                                         style={"letterSpacing": ".08em"}),
                                dmc.Text("TRADING SYSTEM", size="xs",
                                         c="dimmed"),
                            ],
                            gap=0,
                        ),
                    ],
                    gap="sm",
                ),
                withBorder=True, radius="md", shadow="sm", padding="sm",
            ),
            dmc.Divider(),
            dmc.Text("WORKSPACE", size="xs", c="dimmed", fw=700,
                     style={"letterSpacing": ".1em"}, px="sm"),
            dmc.Stack(links, gap=2, className="sidebar-nav"),
            dmc.Space(h="md"),
            dmc.Divider(),
            dmc.Text("INDIA MARKETS", size="xs", c="dimmed", px="sm"),
        ],
        gap="sm",
        className="sidebar",
    )


def _workspace_header(pal: Dict[str, Any], data_source: str) -> dmc.Group:
    """Compact workspace header; both badges report configured system state."""
    return dmc.Group(
        [
            dmc.Stack(
                [
                    dmc.Text("Market operations", fw=700, fz="lg"),
                    dmc.Text("Strategy, portfolio and execution workspace",
                             size="xs", c="dimmed"),
                ],
                gap=2,
            ),
            html.Div(
                _workspace_badges(pal, data_source),
                id="workspace-header-status",
            ),
        ],
        justify="space-between",
        align="center",
        mb="lg",
        className="workspace-header",
    )


def _workspace_badges(pal: Dict[str, Any], data_source: str) -> dmc.Group:
    """Return status badges for the independently reported mode/data axes."""
    mode_color = "red" if pal["key"] == "live" else (
        "orange" if pal["key"] == "unknown" else "violet")
    source_known = data_source in ("live", "paper")
    source_label = ("LIVE SDK DATA" if data_source == "live" else
                    "PAPER DATA" if data_source == "paper" else
                    "DATA SOURCE UNKNOWN")
    return dmc.Group(
        [
            dmc.Badge(
                pal["label"], color=mode_color, variant="dot",
                size="sm", radius="sm",
            ),
            dmc.Badge(
                source_label, color="teal" if source_known else "gray",
                variant="dot", size="sm", radius="sm",
            ),
        ],
        gap="xs",
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
        theme=_mantine_theme(pal),
        # Explicitly pin the workspace dark regardless of OS preference. Mode
        # remains independently visible in the banner and live-updating badges.
        forceColorScheme="dark",
        children=html.Div(
            [
                mode_banner(pal, data_source=data_source),
                html.Div(
                    [
                        _sidebar(pal),
                        html.Div(
                            [
                                _workspace_header(pal, data_source),
                                page_container,
                            ],
                            className="content",
                        ),
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


__all__ = ["app_shell", "mode_banner", "_mantine_theme",
           "_mode_banner_style", "_workspace_badges"]
