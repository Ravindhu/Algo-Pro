"""Protective SL/TP level registry per execution. Wraps the engine's registry.

Flow stage: Exit manager, Protective orders, Order intent
Status: PHASE 5 IMPLEMENTED (wrap; records levels only, never places an order)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/core/protective_orders.py  (register, update, clear,
        snapshot, get)

WHY THIS IS SEPARATE FROM ``execution/protection.py``
-----------------------------------------------------
``execution/protection.py`` decides WHAT the protective levels should be for a
signal (stop distance, target multiple). The engine registry below decides
WHERE the levels are kept once a strategy has stated them, so the position
monitor, the exit engine and the dashboard all read one shared truth.

Two different questions: "what should the stop be" vs "what stop did the
strategy actually declare for this execution". Merging them would make the
exit engine depend on the strategy's parameterisation.

WHY A WRAPPER AT ALL FOR 105 LINES
----------------------------------
The registry is a module-global, which means the level recorded by one
component is invisible to another unless both import the SAME module object.
``blue_bridge.load`` guarantees that (one module in ``sys.modules``). Importing
the file by path, as some scripts do, would create a second, empty registry and
the exits would silently see no stop. This wrapper is the single entry point
that prevents that class of bug.

WHAT THIS WRAPPER ADDS
----------------------
1. **One import path** for every caller, so the registry is genuinely shared.
2. **A fail-safe read** (`levels_snapshot`) that returns [] with a reason rather
   than raising when the engine is unavailable, so a dashboard panel renders.
3. **An explicit `is_protected` check** the position monitor can branch on: a
   position with no recorded stop must be treated as unprotected, never assumed
   safe.

WHAT IT DOES NOT DO
-------------------
- It never places, modifies or cancels a broker order. The engine's own header
  says the registry "does NOT place orders by itself", and this wrapper keeps
  that boundary: ``risk/gate.py`` remains the only path to an order.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

logger = logging.getLogger(__name__)

__all__ = [
    "ProtectionRegistryUnavailable",
    "register_levels",
    "ratchet_stop",
    "clear_levels",
    "get_levels",
    "levels_snapshot",
    "is_protected",
]


class ProtectionRegistryUnavailable(RuntimeError):
    """The engine's protective-orders registry cannot be loaded."""


def _module():
    try:
        return load("core.protective_orders")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise ProtectionRegistryUnavailable(
            f"engine protective orders unavailable: {exc}") from exc


def register_levels(execution_id: str, *, symbol: str, exchange: str,
                    action: str, stop_loss: Optional[float],
                    target: Optional[float], strategy: str = "",
                    meta: Optional[Dict[str, Any]] = None,
                    persist: bool = True) -> bool:
    """Record the SL/TP a strategy declared for one execution. True when stored.

    Idempotent per (execution_id, symbol): calling twice with the same key
    overwrites, which is what a re-registration after a restart should do.
    """
    try:
        _module().register(execution_id, symbol=symbol, exchange=exchange,
                           action=action, stop_loss=stop_loss, target=target,
                           strategy=strategy, meta=meta, persist=persist)
        return True
    except ProtectionRegistryUnavailable as exc:
        logger.warning("protection registry unavailable, level not recorded: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - recording must not break a cycle
        logger.warning("register_levels failed: %r", exc)
        return False


def ratchet_stop(execution_id: str, symbol: str, *,
                 stop_loss: Optional[float]) -> None:
    """Move the stop in the favourable direction only (breakeven, ATR trail).

    The engine's rule is that a BUY stop only ratchets UP. A caller trying to
    loosen a stop is silently ignored there, which is the safe behaviour.
    """
    try:
        _module().update(execution_id, symbol, stop_loss=stop_loss)
    except ProtectionRegistryUnavailable:
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("ratchet_stop failed: %r", exc)


def clear_levels(execution_id: str, symbol: str) -> None:
    """Drop the record once the position is closed."""
    try:
        _module().clear(execution_id, symbol)
    except ProtectionRegistryUnavailable:
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("clear_levels failed: %r", exc)


def get_levels(execution_id: str, symbol: str) -> Optional[Dict[str, Any]]:
    """The recorded levels, or None when the position is not registered."""
    try:
        return _module().get(execution_id, symbol)
    except ProtectionRegistryUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_levels failed: %r", exc)
        return None


def levels_snapshot() -> List[Dict[str, Any]]:
    """Every currently registered level, for the dashboard and the monitor."""
    try:
        return list(_module().snapshot())
    except ProtectionRegistryUnavailable as exc:
        logger.warning("protection snapshot unavailable: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("levels_snapshot failed: %r", exc)
        return []


def is_protected(execution_id: str, symbol: str) -> bool:
    """True only when a stop loss is actually recorded for this position.

    A position with no recorded stop is UNPROTECTED. The monitor must treat that
    as an alert, not as an assumption that some default stop is in force.
    """
    rec = get_levels(execution_id, symbol)
    return bool(rec and rec.get("stop_loss") is not None)
