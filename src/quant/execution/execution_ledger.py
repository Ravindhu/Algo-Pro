"""Execution ledger: child orders, session VWAP, slippage and arrival price.

Flow stage: Ledger and recon, Execution analytics, TCA
Status: PHASE 5 IMPLEMENTED (wrap; recording only, never sends an order)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/core/ledger.py  (create_execution, record_child,
        claim_child, finish_child, record_fill, recover_filled_quantity,
        get_execution, update_execution, update_vwap, get_vwap,
        open_arrival_window, record_arrival_tick, get_arrival_vwap,
        close_arrival_window, recent_slippage_bps, slippage_sample_count,
        record_slippage_sample, ledger_available, make_execution_id,
        api_key_hash)

WHY THIS IS SEPARATE FROM ``monitor/ledger.py``
-----------------------------------------------
``monitor/ledger.py`` answers ONE question: "what do I hold, and does the broker
agree?" It appends fill events to a JSONL file and reconciles positions. That is
the position ledger, and it is correct for that job.

The engine's ``core/ledger.py`` answers a DIFFERENT question: "what happened to
each child order I sent?" It tracks an execution from creation through child
claims and fills, keeps a running session VWAP, a rolling slippage window and an
arrival-price window per open child. None of that existed on this side, so the
dashboard's TCA panel had nothing to read.

These are two ledgers for two purposes. Merging them would corrupt both.

WHAT THIS WRAPPER ADDS
----------------------
1. **A safe call surface.** Every engine function is reached through a module
   that is unavailable when the SDK is missing; without this wrapper a missing
   dependency would raise mid-cycle inside the execution path.
2. **Honest degradation.** ``available()`` reports whether the ledger is really
   usable. A recording failure must never stop an order, but it must also never
   look like a successful record.
3. **A read-only TCA view** (`slippage_report`) the dashboard can render without
   knowing the engine's field names.

WHAT IT DOES NOT DO
-------------------
- It never places, cancels or modifies an order. It records what the order path
  already did.
- It never raises on a recording failure. A ledger problem is logged and
  reported; an order must not fail because a metric could not be written.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

logger = logging.getLogger(__name__)

__all__ = [
    "ExecutionLedgerUnavailable",
    "available",
    "new_execution_id",
    "create_execution",
    "record_child",
    "claim_child",
    "finish_child",
    "record_fill",
    "get_execution",
    "update_execution",
    "recovered_filled_quantity",
    "update_vwap",
    "get_vwap",
    "open_arrival",
    "record_arrival_tick",
    "get_arrival_vwap",
    "close_arrival",
    "slippage_report",
    "record_slippage",
]


class ExecutionLedgerUnavailable(RuntimeError):
    """The engine's execution ledger cannot be loaded."""


def _module():
    try:
        return load("core.ledger")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise ExecutionLedgerUnavailable(f"engine execution ledger unavailable: {exc}") from exc


def available() -> bool:
    """True when the engine ledger module loaded.

    Deliberately does NOT use the engine's ``ledger_available()``: that flag
    reports whether the optional SQL mirror is up, and the engine's own header
    calls the in-memory ledger the AUTHORITATIVE implementation. Gating on that
    flag would report the ledger as down while recording works fine.
    """
    try:
        _module()
        return True
    except ExecutionLedgerUnavailable:
        return False


def new_execution_id(strategy_label: str) -> str:
    """Stable, unique id for one logical execution. Engine-formatted."""
    try:
        return str(_module().make_execution_id(strategy_label))
    except ExecutionLedgerUnavailable:
        # Fail soft: an id is still needed to correlate the fill records.
        import uuid
        return f"{strategy_label}-{uuid.uuid4().hex[:12]}"


def create_execution(execution_id: str, *, request_data: Optional[Dict[str, Any]] = None,
                     **extra: Any) -> Optional[Dict[str, Any]]:
    """Create the execution row. Returns the engine result, or None when unavailable.

    Recording never blocks: an unavailable ledger returns None and logs. The
    caller keeps trading; the TCA panel simply reports no data.
    """
    try:
        return _module().create_execution(
            execution_id=execution_id,
            request_data=request_data,
            **extra,
        )
    except ExecutionLedgerUnavailable as exc:
        logger.warning("execution ledger unavailable, not recording: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - recording must never break a cycle
        logger.warning("create_execution failed: %r", exc)
        return None


def record_child(**kwargs: Any) -> Any:
    """Record one placed child order. Keyword-only, forwarded unchanged."""
    try:
        return _module().record_child(**kwargs)
    except ExecutionLedgerUnavailable as exc:
        logger.warning("execution ledger unavailable, child not recorded: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_child failed: %r", exc)
        return None


def claim_child(execution_id: str, quantity: int, observed_volume: float, phase: str) -> Any:
    """Claim a child slot against the execution, bounded by observed volume."""
    try:
        return _module().claim_child(execution_id, quantity, observed_volume, phase)
    except ExecutionLedgerUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("claim_child failed: %r", exc)
        return None


def finish_child(execution_id: str, child_number: int, *, success: bool,
                 order_id: Optional[str] = None, message: Optional[str] = None) -> Any:
    """Close out a child as succeeded or failed."""
    try:
        return _module().finish_child(execution_id, child_number, success=success,
                                      order_id=order_id, message=message)
    except ExecutionLedgerUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("finish_child failed: %r", exc)
        return None


def record_fill(order_id: str, filled_quantity: int, status: str) -> Any:
    """Record a broker fill report against its order id."""
    try:
        return _module().record_fill(order_id, filled_quantity, status)
    except ExecutionLedgerUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_fill failed: %r", exc)
        return None


def get_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    """The execution row, or None when it is unknown or the ledger is down."""
    try:
        return _module().get_execution(execution_id)
    except ExecutionLedgerUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_execution failed: %r", exc)
        return None


def update_execution(execution_id: str, **fields: Any) -> Any:
    """Patch execution fields. Unknown fields are the engine's to refuse."""
    try:
        return _module().update_execution(execution_id, **fields)
    except ExecutionLedgerUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("update_execution failed: %r", exc)
        return None


def recovered_filled_quantity(execution_id: str) -> int:
    """Filled quantity rebuilt from the durable store after a restart.

    This is what stops a restart from re-sending a child that already filled.
    Zero on any failure: zero is the safe answer, because a caller that then
    re-sends is caught by the position recon gate, whereas a wrong non-zero
    would silently under-fill.
    """
    try:
        return int(_module().recover_filled_quantity(execution_id) or 0)
    except ExecutionLedgerUnavailable:
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("recover_filled_quantity failed: %r", exc)
        return 0


def update_vwap(execution_id: str, *, price: float, volume: float) -> Any:
    """Add a fill to the running session VWAP."""
    try:
        return _module().update_vwap(execution_id, price=price, volume=volume)
    except ExecutionLedgerUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("update_vwap failed: %r", exc)
        return None


def get_vwap(execution_id: str) -> Optional[float]:
    """Session VWAP so far, or None when nothing has filled."""
    try:
        return _module().get_vwap(execution_id)
    except ExecutionLedgerUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_vwap failed: %r", exc)
        return None


def open_arrival(execution_id: str, *, max_ticks: int = 20) -> Any:
    """Start the arrival-price window for a new child."""
    try:
        return _module().open_arrival_window(execution_id, max_ticks=max_ticks)
    except ExecutionLedgerUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("open_arrival_window failed: %r", exc)
        return None


def record_arrival_tick(execution_id: str, *, price: float,
                        volume_weight: float = 1.0) -> Any:
    """Add one tick to the arrival-price window."""
    try:
        return _module().record_arrival_tick(execution_id, price=price,
                                             volume_weight=volume_weight)
    except ExecutionLedgerUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_arrival_tick failed: %r", exc)
        return None


def get_arrival_vwap(execution_id: str) -> Optional[float]:
    """Arrival VWAP. This is the benchmark slippage is measured against."""
    try:
        return _module().get_arrival_vwap(execution_id)
    except ExecutionLedgerUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_arrival_vwap failed: %r", exc)
        return None


def close_arrival(execution_id: str) -> Any:
    """Close the arrival window once the child is done."""
    try:
        return _module().close_arrival_window(execution_id)
    except ExecutionLedgerUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("close_arrival_window failed: %r", exc)
        return None


def record_slippage(execution_id: str, bps: float) -> Any:
    """Add a slippage sample (basis points) to the rolling window."""
    try:
        return _module().record_slippage_sample(execution_id, bps)
    except ExecutionLedgerUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_slippage_sample failed: %r", exc)
        return None


def slippage_report(execution_id: str, *, window: int = 10) -> Dict[str, Any]:
    """Read-only TCA view for the dashboard. Never raises.

    Returns a stable shape whether or not anything has been recorded, so the
    page can tell "no data yet" apart from "ledger down":

        {"available": bool, "execution_id": str, "vwap": float|None,
         "arrival_vwap": float|None, "slippage_bps": float|None,
         "samples": int, "detail": str}
    """
    if not available():
        return {"available": False, "execution_id": execution_id, "vwap": None,
                "arrival_vwap": None, "slippage_bps": None, "samples": 0,
                "detail": "execution ledger unavailable"}

    vwap = get_vwap(execution_id)
    arrival = get_arrival_vwap(execution_id)
    try:
        bps = _module().recent_slippage_bps(execution_id, window=window)
        samples = int(_module().slippage_sample_count(execution_id) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("slippage read failed: %r", exc)
        bps, samples = None, 0

    if vwap is None and arrival is None and not samples:
        detail = "no fills recorded for this execution"
    elif samples == 0:
        detail = "fills recorded, no slippage samples yet"
    else:
        detail = f"{samples} slippage sample(s)"

    return {"available": True, "execution_id": execution_id, "vwap": vwap,
            "arrival_vwap": arrival, "slippage_bps": bps, "samples": samples,
            "detail": detail}
