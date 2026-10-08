"""
mode.py - the safety-critical mode -> appearance mapping.

WHY THIS IS ITS OWN MODULE
--------------------------
"Which mode am I in?" is the single most important thing this UI communicates.
Analyzer mode is simulated; live mode spends real money. A user who confuses
the two can act on a simulated fill believing it was real.

So the mapping lives here, alone, with no dependencies on DMC or Dash:

    analyzer  ->  DARK   + purple accent  + "ANALYZER (SIMULATED)"  banner
    live      ->  LIGHT  + red accent     + "LIVE - REAL MONEY"     banner
    unknown   ->  DARK   + red accent     + "MODE UNKNOWN ..."      banner

The DARK/LIGHT value feeds DMC's ``forceColorScheme``; the banner colours feed
``provider.mode_banner``. Both are derived from one dict so they can never
disagree -- which is exactly the bug class this module exists to prevent.

TWO INDEPENDENT AXES (read this before changing the banner)
-----------------------------------------------------------
There are two different questions and they must never be conflated:

  1. **OpenAlgo mode** (this module) - where ORDERS go. "live" = a placed order
     reaches the broker; "analyze" = it is simulated. Read from
     ``/api/v1/analyzer``.
  2. **Dashboard data wiring** (``provider.banner_text``) - where the PANELS'
     prices come from. The terminal is config-only (SOP 4): it never places,
     modifies or cancels an order. Its reads are either stubbed (paper) or
     wired to the SDK (live candles/quotes).

A dashboard can therefore be (OpenAlgo live, panels paper) -- which is the
normal, safe configuration today -- and the banner MUST say both, because
the alternative is a red "LIVE - REAL MONEY" banner over simulated panels.
That specific mismatch is why ``data_source`` exists on each palette below.
"""
from __future__ import annotations

from typing import Any, Dict

#: The two color schemes DMC understands.
COLOR_SCHEMES = ("dark", "light")

#: Palette keys kept from the original theme.py so the Plotly figures
#: (paper, body_fg, muted_fg, grid) keep working unchanged.
_ANALYZER: Dict[str, Any] = {
    "key": "analyzer",
    "label": "ANALYZER (SIMULATED)",
    "color_scheme": "dark",
    # Orders are simulated AND the panels read stubbed data: fully safe.
    "data_source": "paper",
    "data_source_label": "PANELS: PAPER DATA",
    # banner: purple, unmistakably "not real money"
    "banner_bg": "#2e1065",
    "banner_fg": "#e9d5ff",
    "banner_border": "#7c3aed",
    # DMC accent (drives buttons, sliders, focus rings)
    "accent": "violet",
    # surfaces
    "body_bg": "#0b1220",
    "body_fg": "#e2e8f0",
    "panel_bg": "#0f172a",
    "panel_border": "#1e293b",
    "muted_fg": "#94a3b8",
    "table_hover": "#131f36",
    "row_border": "#16233b",
    # plotly
    "grid": "rgba(180,160,255,0.12)",
    "paper": "rgba(0,0,0,0)",
}

_LIVE: Dict[str, Any] = {
    "key": "live",
    "label": "LIVE - REAL MONEY",
    "color_scheme": "light",
    # Orders ROUTE TO THE BROKER here (OpenAlgo is live). Whether the panels
    # are fed real candles is a separate flag the app sets at startup; the
    # default reflects the config-only terminal, whose reads are stubbed.
    "data_source": "paper",
    "data_source_label": "PANELS: PAPER DATA (ORDER-PLACING DISABLED)",
    # banner: red on white, unmistakably "this is real"
    "banner_bg": "#fef2f2",
    "banner_fg": "#b91c1c",
    "banner_border": "#dc2626",
    "accent": "red",
    # surfaces: white/light
    "body_bg": "#ffffff",
    "body_fg": "#0f172a",
    "panel_bg": "#f8fafc",
    "panel_border": "#e2e8f0",
    "muted_fg": "#64748b",
    "table_hover": "#eef2f7",
    "row_border": "#e2e8f0",
    # plotly
    "grid": "rgba(15,23,42,0.08)",
    "paper": "rgba(0,0,0,0)",
}

#: OpenAlgo reports the analyzer mode as "analyze"; accept every spelling the
#: callers and the API have used so a rename upstream cannot silently fall
#: through to the "unknown" branch.
_PALETTES: Dict[str, Dict[str, Any]] = {
    "analyzer": _ANALYZER,
    "analyze": _ANALYZER,
    "analysis": _ANALYZER,
    "simulated": _ANALYZER,
    "live": _LIVE,
    "real": _LIVE,
}


def scheme_for_mode(mode: str) -> str:
    """Return the DMC color scheme ("dark" | "light") for a mode string."""
    pal = _PALETTES.get((mode or "").lower().strip())
    return pal["color_scheme"] if pal else "dark"


def theme(mode: str) -> Dict[str, Any]:
    """Return the full palette dict for a mode string.

    Kept name-compatible with the old ``pages.theme.theme`` so the Plotly
    figure builders and every page can migrate without changing their calls.

    An unrecognised/empty mode is treated as analyzer-style DARK but is
    explicitly flagged ``unknown`` so the banner says "MODE UNKNOWN" rather
    than pretending to know. Never returns None.
    """
    pal = _PALETTES.get((mode or "").lower().strip())
    if pal is None:
        pal = dict(_ANALYZER)
        pal["key"] = "unknown"
        pal["label"] = "MODE UNKNOWN - VERIFY IN OPENALGO UI"
        pal["banner_bg"] = "#450a0a"
        pal["banner_fg"] = "#fecaca"
        pal["banner_border"] = "#dc2626"
        pal["accent"] = "red"
        # An unreadable mode tells us nothing about order routing. We must NOT
        # imply the data wiring is known either, so say so plainly rather than
        # reusing the paper label as though it had been confirmed.
        pal["data_source"] = "unknown"
        pal["data_source_label"] = "PANELS: DATA SOURCE UNKNOWN"
    return pal


def banner_text(pal: Dict[str, Any], *, data_source: str) -> str:
    """Compose the banner from BOTH axes: order routing + panel data wiring.

    Args:
        pal: a palette from :func:`theme`.
        data_source: ``"paper"`` when the terminal's reads are stubbed, or
            ``"live"`` when they are wired to the SDK. This is the dashboard's
            own wiring flag, NOT OpenAlgo's mode.

    Returns:
        e.g. ``"LIVE - REAL MONEY  |  PANELS: PAPER DATA (ORDER-PLACING DISABLED)"``
        or, when the two agree, just the mode label.

    Why both: OpenAlgo's mode says where ORDERS go; ``data_source`` says where
    the panels' PRICES come from. In the current config-only design they are
    routinely different (OpenAlgo live, panels paper). Showing only the mode
    would put a red "REAL MONEY" banner over simulated charts -- the exact
    two-surfaces-disagree failure we must not ship. When both are live (or both
    paper) the extra clause is redundant, so it is omitted.

    Never invents agreement: if either side is unknown, the panel clause is
    always shown.
    """
    mode_label = str(pal.get("label") or "UNKNOWN")
    # Coerce BEFORE calling .lower(): a non-string flag (an int from a config
    # file, or None) would otherwise raise AttributeError and take the whole
    # banner -- and with it the mode signal -- down. Degrade to "unknown".
    ds = str(data_source if data_source is not None else "").lower().strip()
    if ds not in ("paper", "live"):
        ds = "unknown"

    # Agreed states need no qualifier; everything else states the panel side.
    mode_key = str(pal.get("key") or "unknown")
    agreed = ((mode_key == "live" and ds == "live")
              or (mode_key == "analyzer" and ds == "paper"))
    if agreed:
        return mode_label

    # The panel clause is derived from the palette so its wording lives in one
    # place (the palettes above) and cannot drift from the tests.
    if ds == "live":
        clause = "PANELS: LIVE SDK DATA"
    elif ds == "paper":
        clause = str(pal.get("data_source_label") or "PANELS: PAPER DATA")
    else:
        clause = "PANELS: DATA SOURCE UNKNOWN"
    return f"{mode_label}  |  {clause}"


__all__ = ["COLOR_SCHEMES", "scheme_for_mode", "theme", "banner_text"]
