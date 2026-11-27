"""Day-end report for Full Auto (owner request 2026-11-27: "create report each day end").

Plain English: at the end of the trading day, write one short report of what happened: how many
audit events of each kind, the Full Auto steps that passed or failed, kill-switch events, and the
bot's saved daily P&L figures. Written as JSON and Markdown under ``data/reports/``.

Read-only on the trading side. It sends nothing, changes no position and never reads a secret.
Days are IST. Audit timestamps are UTC and are converted before grouping.

Flow stage: Dashboard -> reporting (no order path)
Status: PHASE 3 IMPLEMENTED (2026-11-27; tests in tests/unit/test_daily_report.py)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from quant.audit.log import AuditLog

#: Numeric P&L fields are copied from the bot's saved state when they are plain numbers.
PNL_KEYS = ("realized_pnl", "unrealized_pnl", "daily_pnl", "pnl", "net_pnl")


def _ist_day(ts: str) -> str:
    """IST date (YYYY-MM-DD) of an audit timestamp. Empty when it cannot be read."""
    try:
        when = datetime.fromisoformat(str(ts))
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)).date().isoformat()


def _pnl(pnl_state_path) -> Dict[str, float]:
    try:
        raw = json.loads(Path(pnl_state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in PNL_KEYS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = round(float(value), 2)
    return out


def build(day: str, audit_paths: Iterable[Any], pnl_state_path=None) -> Dict[str, Any]:
    """The report for one IST day. ``audit_paths`` may list several audit files (bot, owner)."""
    kinds: Dict[str, int] = {}
    steps: List[Dict[str, Any]] = []
    kill_events: List[str] = []
    for path in audit_paths:
        if not Path(path).is_file():
            continue
        for row in AuditLog(path).read_all():
            if _ist_day(row.get("ts", "")) != day:
                continue
            kind = str(row.get("kind", ""))
            kinds[kind] = kinds.get(kind, 0) + 1
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if kind == "full_auto_step":
                steps.append({"step": payload.get("step"), "ok": bool(payload.get("ok")),
                              "detail": str(payload.get("detail", ""))[:120]})
            if "kill" in kind:
                kill_events.append(kind)
    return {
        "day": day,
        "mode": "sandbox",   # Full Auto is sandbox only (owner decision 2026-11-27)
        "events_by_kind": dict(sorted(kinds.items())),
        "full_auto_steps": steps,
        "kill_events": kill_events,
        "pnl": _pnl(pnl_state_path) if pnl_state_path else {},
    }


def to_markdown(report: Dict[str, Any]) -> str:
    lines = [f"# Daily report {report['day']} (mode: {report['mode']})", ""]
    lines.append("## Full Auto steps")
    if report["full_auto_steps"]:
        for s in report["full_auto_steps"]:
            lines.append(f"- {s['step']}: {'ok' if s['ok'] else 'FAILED'} - {s['detail']}")
    else:
        lines.append("- none today")
    lines += ["", "## Kill switch", f"- events: {len(report['kill_events'])}", ""]
    lines.append("## P&L (bot state)")
    if report["pnl"]:
        for key, value in report["pnl"].items():
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- no P&L state saved")
    lines += ["", "## Events by kind"]
    for kind, count in report["events_by_kind"].items():
        lines.append(f"- {kind}: {count}")
    return "\n".join(lines) + "\n"


def write(report: Dict[str, Any], reports_dir) -> List[str]:
    """Write ``<day>.json`` and ``<day>.md``. Returns the two paths."""
    target = Path(reports_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / f"{report['day']}.json"
    md_path = target / f"{report['day']}.md"
    json_path.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")
    return [str(json_path), str(md_path)]
