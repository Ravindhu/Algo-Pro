"""
log_view.py - the raw log / alert tail, in one place.

Used by the Infrastructure page (raw alert tail + raw config dump) and the
Analytics page. The native build hand-rolled ``html.Pre`` with a ``.log-pre``
class; this wraps DMC's ``Code`` block so the surface follows the mode theme
automatically and long lines scroll instead of stretching the layout.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

import dash_mantine_components as dmc
from dash import html

#: Severity -> DMC colour. Kept here so every log surface agrees.
SEVERITY_COLOURS: Dict[str, str] = {
    "info": "blue",
    "warning": "yellow",
    "error": "red",
    "critical": "red",
}


def format_alerts(alerts: Iterable[Dict[str, Any]],
                  limit: int = 200) -> List[str]:
    """Turn alert dicts into ``HH:MM:SS  SEVERITY  message`` lines.

    Sorted oldest-first so the newest line is at the bottom, matching how a
    trader reads a terminal tail.
    """
    lines: List[str] = []
    items = list(alerts)[-limit:]
    for a in items:
        ts = a.get("ts") or 0
        when = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
        sev = str(a.get("severity", "info")).upper()
        lines.append(f"{when}  {sev:<8}  {a.get('message', '')}")
    return lines


def LogView(lines: Any, *, empty: str = "No log entries yet.",
            max_height: int = 420,
            language: Optional[str] = None) -> Any:
    """A scrollable monospace block.

    Args:
        lines: a list of strings, or one pre-joined string.
        empty: message when there is nothing to show.
        max_height: pixel cap; the block scrolls beyond it rather than growing
            the page (an unbounded log tail pushed every panel off-screen in
            the native build -- the CSS ``max-height: 420px`` is now a prop).
        language: optional DMC Code language hint for highlighting.
    """
    if isinstance(lines, str):
        text = lines
    else:
        text = "\n".join(str(x) for x in (lines or []))

    if not text.strip():
        return html.Div(str(empty), className="empty")

    return dmc.Code(
        text,
        block=True,
        className="log-pre",
        style={
            "maxHeight": max_height,
            "overflow": "auto",
            "whiteSpace": "pre-wrap",
            "fontSize": 12,
        },
    )


__all__ = ["LogView", "SEVERITY_COLOURS", "format_alerts"]
