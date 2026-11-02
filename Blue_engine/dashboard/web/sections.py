"""
sections.py - pure data builders for the FastAPI dashboard header sections.

Each of the twelve header buttons maps to one builder here. A builder returns
a plain dict that the browser renders as a popup:

    {
      "key": "portfolio",
      "title": "Portfolio",
      "status": "ok" | "empty" | "error",
      "summary": [{"label": str, "value": str}, ...],
      "tables":  [{"title": str, "columns": [str], "rows": [[str]]}, ...],
      "notes":   [str, ...],
      "error":   str | None,
    }

Design rules
------------
* READ-ONLY. Nothing in this module places an order or writes state.
* FAIL SOFT. A missing source gives ``status="empty"`` with a note; an
  exception gives ``status="error"``. The page never raises for one section.
* SECRET-SAFE. Keys that look like credentials are never rendered.
* NO FRAMEWORK IMPORTS. This module does not import FastAPI, so the builders
  are unit-testable without the web stack.
"""
from __future__ import annotations

import json
import logging
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Header buttons, in display order: (key, title).
SECTIONS: Tuple[Tuple[str, str], ...] = (
    ("terminal", "Terminal"),
    ("portfolio", "Portfolio"),
    ("risk", "Risk Control"),
    ("analytics", "Analytics"),
    ("infra", "Infrastructure"),
    ("economic", "Economic"),
    ("sectors", "Sectors"),
    ("analytics_portfolio", "Portfolio Analytics"),
    ("ml", "ML Models"),
    ("optimizer", "Optimizer"),
    ("screener", "Screener"),
    ("agent_line", "Agent Line"),
)

#: Config and payload keys whose values must never reach the browser.
_SECRET_MARKERS = ("api_key", "apikey", "secret", "password", "token", "pepper", "salt")
_MAX_ROWS = 50
_MAX_TEXT = 200

Builder = Callable[[], Dict[str, Any]]


# ---------------------------------------------------------------- helpers
def _is_secret(key: str) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    text = str(value)
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _is_record_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(r, dict) for r in value)


def _flatten(data: Any, *, prefix: str = "", depth: int = 0,
             summary: List[Dict[str, str]], tables: List[Dict[str, Any]]) -> None:
    """Turn an arbitrary payload into summary rows and record tables.

    Scalars become summary rows, lists of records become tables, and nested
    dicts are flattened up to two levels with dotted labels. Secret-looking
    keys are skipped entirely.
    """
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if _is_secret(key):
            continue
        label = f"{prefix}{key}"
        if _is_scalar(value):
            summary.append({"label": label, "value": _fmt(value)})
        elif _is_record_list(value):
            columns = [str(c) for c in value[0].keys() if not _is_secret(c)]
            rows = [[_fmt(r.get(c)) for c in columns] for r in value[:_MAX_ROWS]]
            tables.append({"title": label, "columns": columns, "rows": rows,
                           "total": len(value)})
        elif isinstance(value, dict) and depth < 2:
            _flatten(value, prefix=f"{label}.", depth=depth + 1,
                     summary=summary, tables=tables)
        elif isinstance(value, list):
            summary.append({"label": label, "value": f"{len(value)} items"})


def _payload(key: str, title: str, *, summary=None, tables=None,
             notes=None, status: str = "ok") -> Dict[str, Any]:
    summary = summary or []
    tables = tables or []
    notes = notes or []
    if status == "ok" and not summary and not tables:
        status = "empty"
    return {"key": key, "title": title, "status": status, "summary": summary,
            "tables": tables, "notes": notes, "error": None}


def _section(key: str, title: str) -> Callable[[Builder], Builder]:
    """Decorator: run a builder, converting any exception into an error payload."""
    def wrap(builder: Builder) -> Builder:
        @wraps(builder)
        def guarded() -> Dict[str, Any]:
            try:
                return builder()
            except Exception as exc:  # noqa: BLE001 - one section must not break the page
                logger.warning("dashboard section %s failed: %r", key, exc)
                payload = _payload(key, title, status="error")
                payload["error"] = f"{type(exc).__name__}: {exc}"
                return payload
        return guarded
    return wrap


def _from_data(key: str, title: str, data: Any, *, notes=None) -> Dict[str, Any]:
    summary: List[Dict[str, str]] = []
    tables: List[Dict[str, Any]] = []
    _flatten(data, summary=summary, tables=tables)
    return _payload(key, title, summary=summary, tables=tables, notes=notes)


def _gather() -> Any:
    from blue_engine.dashboard import data_layer as _data
    return _data.gather()


# ---------------------------------------------------------------- builders
@_section("terminal", "Terminal")
def _terminal() -> Dict[str, Any]:
    snap = _gather()
    summary = [
        {"label": "Mode", "value": _fmt(snap.mode)},
        {"label": "Ledger", "value": "healthy" if snap.ledger_ok else "unavailable"},
        {"label": "Open positions", "value": _fmt(len(snap.positions))},
        {"label": "Executions", "value": _fmt(len(snap.executions))},
        {"label": "Trades", "value": _fmt(len(snap.trades))},
        {"label": "Alerts", "value": _fmt(len(snap.alerts))},
    ]
    tables = []
    if snap.alerts:
        tables.append({"title": "Alerts", "columns": list(snap.alerts[0].keys()),
                       "rows": [[_fmt(a.get(c)) for c in snap.alerts[0].keys()]
                                for a in snap.alerts[:_MAX_ROWS]],
                       "total": len(snap.alerts)})
    return _payload("terminal", "Terminal", summary=summary, tables=tables)


@_section("portfolio", "Portfolio")
def _portfolio() -> Dict[str, Any]:
    snap = _gather()
    tables = []
    if snap.positions:
        cols = list(snap.positions[0].keys())
        tables.append({"title": "Positions", "columns": cols,
                       "rows": [[_fmt(p.get(c)) for c in cols] for p in snap.positions[:_MAX_ROWS]],
                       "total": len(snap.positions)})
    return _payload("portfolio", "Portfolio",
                    summary=[{"label": "Open positions", "value": _fmt(len(snap.positions))}],
                    tables=tables)


@_section("risk", "Risk Control")
def _risk() -> Dict[str, Any]:
    from blue_engine.dashboard import data_layer as _data
    return _from_data("risk", "Risk Control", _data.risk_report(months=0))


@_section("analytics", "Analytics")
def _analytics() -> Dict[str, Any]:
    snap = _gather()
    fills = [t for t in snap.trades if isinstance(t, dict)]
    return _payload("analytics", "Analytics", summary=[
        {"label": "Trades recorded", "value": _fmt(len(fills))},
        {"label": "Executions", "value": _fmt(len(snap.executions))},
    ])


@_section("infra", "Infrastructure")
def _infra() -> Dict[str, Any]:
    from blue_engine.dashboard import data_layer as _data
    snap = _gather()
    return _payload("infra", "Infrastructure", summary=[
        {"label": "Config file", "value": _fmt(_data.config_path())},
        {"label": "Ledger", "value": "healthy" if snap.ledger_ok else "unavailable"},
        {"label": "Mode", "value": _fmt(snap.mode)},
        {"label": "Snapshot time", "value": _fmt(snap.fetched_at)},
    ])


@_section("economic", "Economic")
def _economic() -> Dict[str, Any]:
    from blue_engine.dashboard import data_layer as _data
    payload = _from_data("economic", "Economic", _data.economic_snapshot())
    history = _data.economic_history(limit=20)
    if history:
        cols = list(history[-1].keys())
        payload["tables"].append({"title": "Recent history", "columns": cols,
                                  "rows": [[_fmt(h.get(c)) for c in cols] for h in history[-_MAX_ROWS:]],
                                  "total": len(history)})
    if payload["summary"] or payload["tables"]:
        payload["status"] = "ok"
    return payload


@_section("sectors", "Sectors")
def _sectors() -> Dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "input" / "sector_engine.json"
    if not path.exists():
        return _payload("sectors", "Sectors", notes=[
            "No sector snapshot found (input/sector_engine.json). "
            "Run the sector engine to publish one."])
    data = json.loads(path.read_text(encoding="utf-8"))
    return _from_data("sectors", "Sectors", data)


@_section("analytics_portfolio", "Portfolio Analytics")
def _analytics_portfolio() -> Dict[str, Any]:
    from blue_engine.dashboard import data_layer as _data
    summary: List[Dict[str, str]] = []
    tables: List[Dict[str, Any]] = []
    for label, fn in (
        ("Attribution", _data.attribution_snapshot),
        ("Exposure", _data.exposure_snapshot),
        ("Policy", _data.policy_snapshot),
        ("Views", _data.views_snapshot),
    ):
        try:
            _flatten(fn(), prefix=f"{label}: ", summary=summary, tables=tables)
        except Exception as exc:  # noqa: BLE001 - keep the other blocks
            summary.append({"label": f"{label}: unavailable", "value": type(exc).__name__})
    return _payload("analytics_portfolio", "Portfolio Analytics",
                    summary=summary, tables=tables)


@_section("ml", "ML Models")
def _ml() -> Dict[str, Any]:
    from blue_engine.dashboard import data_layer as _data
    return _from_data("ml", "ML Models", _data.ml_training_snapshot(limit=50))


@_section("optimizer", "Optimizer")
def _optimizer() -> Dict[str, Any]:
    from blue_engine.dashboard import data_layer as _data
    return _from_data("optimizer", "Optimizer", _data.optimizer_snapshot(limit=40))


@_section("screener", "Screener")
def _screener() -> Dict[str, Any]:
    return _payload("screener", "Screener", notes=[
        "The screener publishes results to the screener bridge snapshot. "
        "This popup shows the snapshot when one is available."])


@_section("agent_line", "Agent Line")
def _agent_line() -> Dict[str, Any]:
    from blue_engine.core.agent_team import AgentTeam
    from blue_engine.core.stage_skills import load_skills
    from blue_engine.dashboard import state

    roster = AgentTeam(load_skills(None)).roster()
    summary: List[Dict[str, str]] = []
    tables: List[Dict[str, Any]] = []
    _flatten(state.read_shift(), prefix="Shift: ", summary=summary, tables=tables)
    _flatten(state.read_token_usage(), prefix="Tokens: ", summary=summary, tables=tables)
    if _is_record_list(roster):
        cols = list(roster[0].keys())
        tables.insert(0, {"title": "Agents", "columns": cols,
                          "rows": [[_fmt(r.get(c)) for c in cols] for r in roster[:_MAX_ROWS]],
                          "total": len(roster)})
    elif isinstance(roster, dict):
        _flatten(roster, prefix="Agents: ", summary=summary, tables=tables)
    return _payload("agent_line", "Agent Line", summary=summary, tables=tables)


_BUILDERS: Dict[str, Builder] = {
    "terminal": _terminal,
    "portfolio": _portfolio,
    "risk": _risk,
    "analytics": _analytics,
    "infra": _infra,
    "economic": _economic,
    "sectors": _sectors,
    "analytics_portfolio": _analytics_portfolio,
    "ml": _ml,
    "optimizer": _optimizer,
    "screener": _screener,
    "agent_line": _agent_line,
}


def section_titles() -> List[Dict[str, str]]:
    """The header buttons, as ``[{"key", "title"}]``."""
    return [{"key": key, "title": title} for key, title in SECTIONS]


def build_section(key: str) -> Optional[Dict[str, Any]]:
    """Build one section's popup payload, or ``None`` for an unknown key."""
    builder = _BUILDERS.get(key)
    return builder() if builder is not None else None
