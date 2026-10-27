"""
login_status.py - the broker-login card for the Infrastructure page.

WHY THIS EXISTS
===============
A failed 08:50 login was previously invisible unless the operator opened
``Temp/run/last_login.json`` manually - which is exactly the kind of thing that
gets noticed at 09:20 instead of 08:51. This card reads that file and puts the
result on ``/infra``, next to the service health it belongs beside.

HONESTY RULES (same as the rest of the dashboard)
-------------------------------------------------
* **No file, or a file from before today -> "no run yet".** The card never
  invents a success. A stale record is *stale*, not green.
* **A record is only trusted if it is today's** (UTC date match). Yesterday's
  successful login says nothing about today's broker session, because broker
  sessions roll over daily - so showing it as OK would be a lie.
* Values are reported, never decorated: ``leg``, ``reason``, the step list, and
  the broker name come straight from the record.
* Read-only. This module never writes, never triggers a login, and never
  touches the broker.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["last_login_record", "login_card", "LOGIN_STALE_AFTER_HOURS"]

ROOT = Path(__file__).resolve().parents[3]
LAST_LOGIN = ROOT / "Temp" / "run" / "last_login.json"

#: A record older than this is shown as stale even if it is today's: the
#: morning login should have run by 08:50, so anything older than 4 hours is
#: yesterday's news in practice.
LOGIN_STALE_AFTER_HOURS = 4.0

#: ``NAME=value`` where NAME looks like a credential.
_SECRET_ASSIGNMENT = re.compile(
    r"\b([A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY|PIN|SEED)[A-Z0-9_]*)\s*=\s*\S+",
    re.IGNORECASE)
#: A bare 6-digit TOTP.
_SIX_DIGITS = re.compile(r"\b\d{6}\b")


def last_login_record(path: Optional[Path] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return ``(record, note)``. ``record`` is None when there is nothing honest.

    ``note`` explains WHY, so the card can say "no run yet" vs "unreadable"
    vs "from yesterday" instead of a single useless "unknown".
    """
    target = Path(path) if path else LAST_LOGIN
    if not target.is_file():
        return None, "no run recorded yet"

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"record unreadable: {type(exc).__name__}"

    if not isinstance(raw, dict) or "ok" not in raw:
        return None, "record has the wrong shape"

    # Broker sessions roll over daily, so only TODAY's record counts.
    stamp = str(raw.get("at") or raw.get("finished_at") or "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today not in stamp:
        return raw, f"stale: record is from {stamp[:10] or 'an unknown date'}, not today"

    # Freshness within the day: a 08:50 login should not be trusted at 14:00.
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - parsed).total_seconds() / 3600.0
        if age_hours > LOGIN_STALE_AFTER_HOURS:
            return raw, f"stale: recorded {age_hours:.1f}h ago (window is " \
                        f"{LOGIN_STALE_AFTER_HOURS:g}h)"
    except ValueError:
        return raw, "stale: timestamp unparseable"

    return raw, "today's run"


def _state(record: Optional[Dict[str, Any]], note: str
           ) -> Tuple[str, str, bool]:
    """``(label, colour_token, is_ok)`` for the card header."""
    if record is None:
        return ("NO RUN" if "no run" in note else "UNAVAILABLE", note, False)
    if not record.get("ok"):
        return ("FAILED", note, False)
    if "stale" in note:
        return ("STALE", note, False)
    broker = record.get("broker") or record.get("broker_name") or "-"
    return (f"READY ({broker})", note, True)


def _redact(text: str) -> str:
    """Strip anything credential-shaped from a recorded detail line.

    The runner is careful never to write a password or a 6-digit code, but this
    card is the LAST line of defence: it renders text from a JSON file on a page
    any logged-in operator can open, so a leak in the writer must not become a
    leak on screen. Two rules, both cheap:

    * ``NAME=value`` for any name that looks secret (PASSWORD, SECRET, TOKEN,
      API_KEY, PIN) -> ``NAME=***``.
    * a bare 6-digit token (a live TOTP) -> ``******``.
    """
    text = str(text or "")
    text = _SECRET_ASSIGNMENT.sub(r"\1=***", text)
    return _SIX_DIGITS.sub("******", text)
def login_rows(record: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The step table rows, in order, exactly as recorded - minus secrets."""
    if not record:
        return []
    return [{"step": row.get("step", ""),
             "ok": bool(row.get("ok")),
             "detail": _redact(row.get("detail")),
             "at": str(row.get("at") or "")[-8:]}
            for row in (record.get("steps") or [])]


_LOGIN_COLUMNS = [
    {"key": "step", "label": "Step", "bold": True},
    {"key": "ok", "label": "Result", "render": lambda _v, row: _pill(row["ok"])},
    {"key": "detail", "label": "Detail", "mono": True},
    {"key": "at", "label": "At (UTC)", "mono": True},
]


def _pill(ok: bool):
    """Local import to avoid a cycle at module import time."""
    from blue_engine.dashboard.ui.kpi import pill

    return pill("OK" if ok else "FAIL", ok=ok)


def login_card(*, path: Optional[Path] = None):
    """The DMC card for ``/infra``. Returns ``(card_component, rows, state)``.

    Importing dash components lazily keeps this module importable from tests
    that do not have a Dash app running.
    """
    import dash_mantine_components as dmc
    from dash import html

    from blue_engine.dashboard.ui.panel import Panel
    from blue_engine.dashboard.ui.table import DataTable
    from blue_engine.dashboard.ui.kpi import NEGATIVE, POSITIVE, WARNING

    # Defined locally (not imported at module scope) so this module stays
    # importable without the dashboard stack - the tests rely on that.
    _AI_GREY = "#C1C2C5"

    record, note = last_login_record(path)
    label, detail, ok = _state(record, note)

    colour = POSITIVE if ok else (WARNING if record is not None else NEGATIVE)
    ai_rows = [
        ("Broker login", label, colour),
        ("Detail", detail[:80], colour if ok else NEGATIVE),
        ("Broker", (record or {}).get("broker") or
                  (record or {}).get("broker_name") or "-", _AI_GREY),
        ("Recorded", str((record or {}).get("at") or "-")[:19], _AI_GREY),
    ]

    rows = login_rows(record)
    body_children = [
        html.Div(detail, style={
            "color": "#C1C2C5", "fontSize": "12px", "marginBottom": "10px"}),
        DataTable(_LOGIN_COLUMNS, rows,
                  empty="No login steps recorded.", highlight_on_hover=False),
    ] if rows else [
        html.Div(detail, style={
            "color": "#C1C2C5", "fontSize": "12px"}),
    ]

    card = Panel(
        f"Broker login ({label})",
        body_children,
        icon="mdi:key-variant",
    )
    return card, ai_rows, {"label": label, "ok": ok, "detail": detail}
