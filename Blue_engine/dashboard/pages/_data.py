"""
_data.py - shared, read-only data adapter for the Dash pages.

The Dash pages must not care how the engine got its numbers. This module
is the one place that knows:

  * how to load the user inputs (``blue_engine/input/master.json`` and any
    override file), so every page shows the *same* inputs the slicer uses;
  * how to read running executions / VWAP / slippage from the ledger
    (:mod:`blue_engine.core.ledger`), which mirrors to the DB when available;
  * how to fall back to a live in-process snapshot when the dashboard is
    hosted in the same process as :class:`LiveDashboard`.

Everything is best-effort: if the ledger or config is missing we return
empty structures and a flag, never raise. A dashboard that dies because a
DB is down is worse than a dashboard with blank tables.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from blue_engine.core.mode import read_mode

logger = logging.getLogger(__name__)


# Default config location, relative to the repo root.
_DEFAULT_CONFIG = Path("blue_engine") / "input" / "master.json"


def config_path() -> Path:
    """Resolve the config path (env override wins)."""
    env = os.getenv("POV_CONFIG_PATH")
    if env:
        return Path(env)
    return _DEFAULT_CONFIG


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the dashboard/slicer config JSON.

    Returns ``{}`` when the file is missing or malformed so callers can
    treat "no config" and "empty config" identically.
    """
    p = Path(path) if path else config_path()
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        logger.debug("config not found at %s", p)
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("config load failed at %s: %s", p, exc)
        return {}


def _pov_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return the flat POV input block from the config.

    ``master.json`` in this repo keeps the POV inputs at the top level
    (alongside the other Feature knobs). A hand-written dashboard config
    may instead nest them under a ``pov`` key. Support both so the pages
    work with either shape.
    """
    if not isinstance(cfg, dict):
        return {}
    nested = cfg.get("pov")
    if isinstance(nested, dict):
        merged = dict(cfg)
        merged.pop("pov", None)
        merged.update(nested)
        return merged
    return cfg


@dataclass
class InputRow:
    """One user input rendered as a table row."""

    group: str
    key: str
    value: Any
    source: str  # "master.json" | "env" | "default"


@dataclass
class Snapshot:
    """A point-in-time read of everything the pages need."""

    config: Dict[str, Any] = field(default_factory=dict)
    inputs: List[InputRow] = field(default_factory=list)
    executions: List[Dict[str, Any]] = field(default_factory=list)
    positions: List[Dict[str, Any]] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    ledger_ok: bool = False
    live_engine: Any = None
    fetched_at: float = 0.0
    mode: str = "unknown"  # "live" | "analyze" | "unknown" (OpenAlgo analyzer mode)

    def pov(self) -> Dict[str, Any]:
        return _pov_block(self.config)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

# Which top-level inputs belong to which page group. This is what lets
# the Terminal page show "runtime" inputs and the Risk page show the
# risk inputs, from a single flat config.
_INPUT_GROUPS: Dict[str, tuple] = {
    "runtime": (
        "pov_mode", "mode", "action", "symbol", "exchange", "product",
        "order_mode", "quantity", "interval", "duration", "starting_cash",
    ),
    "pov_band": (
        "vwap_filter_enabled", "vwap_ceiling_pct", "vwap_floor_pct",
        "min_pov_pct", "max_pov_pct", "passthrough_floor",
        "vwap_session_floor_price",
    ),
    "slippage": (
        "slippage_guard_enabled", "max_slippage_bps",
        "slippage_penalty_factor", "slippage_window_fills",
        "arrival_window_ticks",
    ),
    "risk": (
        "max_universe_rows", "quote_failure_threshold",
        "ceiling_price", "stop_loss_price", "upper_circuit", "lower_circuit",
        "max_symbols", "max_blocks_pct", "cancellation_cool_off_seconds",
    ),
}

# Keys the dashboard treats as secret-ish; never render their value.
_MASKED_KEYS = ("api_key", "apikey", "secret", "password", "token")


def group_for_key(key: str) -> str:
    for group, keys in _INPUT_GROUPS.items():
        if key in keys:
            return group
    return "other"


def _mask(key: str, value: Any) -> Any:
    if any(s in key.lower() for s in _MASKED_KEYS) and value:
        return "********"
    return value


def collect_inputs(cfg: Dict[str, Any]) -> List[InputRow]:
    """Flatten the config into a list of labelled input rows."""
    flat = _pov_block(cfg)
    rows: List[InputRow] = []
    for key in sorted(flat.keys()):
        if key.startswith("_"):  # comment keys
            continue
        rows.append(
            InputRow(
                group=group_for_key(key),
                key=key,
                value=_mask(key, flat[key]),
                source="master.json",
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _try_ledger() -> tuple[bool, Optional[Any]]:
    try:
        from blue_engine.core import ledger as _ledger

        return bool(_ledger.ledger_available()), _ledger
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("ledger unavailable: %s", exc)
        return False, None


def _execution_rows(ledger: Any) -> List[Dict[str, Any]]:
    """Best-effort enumeration of running executions.

    The ledger is keyed by execution_id and has no public "list" API, so
    we look in the in-memory registry (populated by create_execution) and
    hydrate each row through the public get_vwap / slippage accessors.
    """
    rows: List[Dict[str, Any]] = []
    store = getattr(ledger, "_IN_MEMORY_EXECUTIONS", None)
    if not isinstance(store, dict):
        return rows
    for execution_id in list(store.keys()):
        try:
            row = ledger.get_execution(execution_id) or {}
        except Exception:
            continue
        if not isinstance(row, dict):
            row = {}
        vwap = None
        slip = 0.0
        try:
            vwap = ledger.get_vwap(execution_id)
            slip = ledger.recent_slippage_bps(execution_id)
        except Exception:
            pass
        rows.append(
            {
                "execution_id": execution_id,
                "strategy": row.get("strategy") or row.get("request", {}).get("strategy") or "-",
                "symbol": row.get("symbol") or row.get("request", {}).get("symbol") or "-",
                "exchange": row.get("exchange") or row.get("request", {}).get("exchange") or "-",
                "action": row.get("action") or row.get("request", {}).get("action") or "-",
                "phase": row.get("phase", "accumulating"),
                "target_qty": row.get("target_quantity") or row.get("quantity") or 0,
                "filled_qty": row.get("filled_quantity") or 0,
                "vwap": vwap,
                "slippage_bps": slip,
                "status": row.get("status", "running"),
            }
        )
    return rows


def _live_snapshot(engine: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Pull rows out of a live LiveDashboard instance, if one is attached."""
    out: Dict[str, List[Dict[str, Any]]] = {"positions": [], "trades": [], "alerts": []}
    if engine is None:
        return out
    try:
        out["positions"] = engine._build_universe_rows()  # noqa: SLF001 - read-only view
    except Exception as exc:
        logger.debug("live universe read failed: %s", exc)
    try:
        for t in engine._positions.recent_trades(limit=200):  # noqa: SLF001
            out["trades"].append(
                {
                    "symbol": t.symbol,
                    "exchange": t.exchange,
                    "side": t.side,
                    "qty": t.qty,
                    "price": t.price,
                    "state": t.state,
                    "pov_pct": t.pov_pct,
                    "ts": t.ts,
                }
            )
    except Exception as exc:
        logger.debug("live trades read failed: %s", exc)
    try:
        for a in engine._data.state.last_alerts[-200:]:  # noqa: SLF001
            out["alerts"].append(
                {
                    "ts": getattr(a, "ts", 0.0),
                    "severity": getattr(a, "severity", "info"),
                    "message": getattr(a, "message", repr(a)),
                }
            )
    except Exception as exc:
        logger.debug("live alerts read failed: %s", exc)
    return out


def gather(engine: Any = None, config: Optional[Dict[str, Any]] = None) -> Snapshot:
    """Read everything the pages need in one call."""
    cfg = config if config is not None else load_config()
    ok, ledger = _try_ledger()
    snap = Snapshot(config=cfg, ledger_ok=ok, live_engine=engine, fetched_at=time.time())
    snap.mode = read_mode(getattr(engine, "client", None))
    snap.inputs = collect_inputs(cfg)
    if ledger is not None:
        snap.executions = _execution_rows(ledger)
    live = _live_snapshot(engine)
    snap.positions = live["positions"]
    snap.trades = live["trades"]
    snap.alerts = live["alerts"]
    return snap


# ---------------------------------------------------------------------------
# Small formatting helpers shared by the pages
# ---------------------------------------------------------------------------


def fmt(value: Any, default: str = "-") -> str:
    """Format a value for a table cell without blowing up on None."""
    if value is None:
        return default
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


# ---------------------------------------------------------------------------
# Economic engine (macro regime) - read-only helpers for the Economic page
# ---------------------------------------------------------------------------

# The economic pipeline config is separate from the POV master config; it lives
# in input/macro_master.json (the six-engine macro pipeline).
_DEFAULT_MACRO_CONFIG = Path("blue_engine") / "input" / "macro_master.json"


def macro_config_path() -> Path:
    """Resolve the macro-pipeline config path (env override wins)."""
    env = os.getenv("BLUE_MACRO_CONFIG_PATH")
    if env:
        return Path(env)
    return _DEFAULT_MACRO_CONFIG


def load_macro_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the macro pipeline config (returns {} on any failure)."""
    p = Path(path) if path else macro_config_path()
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        logger.debug("macro config not found at %s", p)
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("macro config load failed at %s: %s", p, exc)
        return {}


def economic_history(limit: int = 400) -> List[Dict[str, Any]]:
    """Read the persisted economic-engine runs (oldest first).

    Backed by ``blue_engine.economics.run_history`` (data/economic_runs/*.jsonl).
    Best-effort: any import/IO failure yields an empty list, never an error.
    """
    try:
        from blue_engine.economics import run_history
        return run_history.read_recent(days=30, limit=limit)
    except Exception as exc:  # noqa: BLE001 - a dashboard must not die on data
        logger.debug("economic history unavailable: %s", exc)
        return []


def economic_snapshot() -> Dict[str, Any]:
    """The latest economic run payload plus its config (for the Economic page)."""
    hist = economic_history(limit=1)
    latest = hist[-1] if hist else None
    return {
        "latest": latest,
        "config": load_macro_config(),
        "has_history": latest is not None,
    }


def band_from_inputs(cfg: Dict[str, Any], vwap: float) -> Dict[str, float]:
    """Recompute the POV band from the same inputs the slicer uses.

    Mirrors ``blue_engine.core.band`` exactly (ceiling = +x%, floor = -y%)
    so the Terminal page can draw the band even when no live engine is
    attached.
    """
    from blue_engine.core.band import compute_ceiling, compute_floor

    pov = _pov_block(cfg)
    ceiling_pct = float(pov.get("vwap_ceiling_pct", 0.5))
    floor_pct = float(pov.get("vwap_floor_pct", 3.0))
    if not vwap or vwap <= 0:
        return {"ceiling": 0.0, "floor": 0.0}
    return {
        "ceiling": compute_ceiling(vwap, ceiling_pct),
        "floor": compute_floor(vwap, floor_pct),
        "ceiling_pct": ceiling_pct,
        "floor_pct": floor_pct,
    }


__all__ = [
    "InputRow",
    "Snapshot",
    "band_from_inputs",
    "collect_inputs",
    "config_path",
    "economic_history",
    "economic_snapshot",
    "fmt",
    "gather",
    "group_for_key",
    "load_config",
    "load_macro_config",
    "macro_config_path",
]
