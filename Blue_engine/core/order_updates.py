"""
order_updates.py - Order-update WebSocket subscription (Feature 11).

Responsibility
--------------
Hook the broker's order-update event stream into the persistent
child-order ledger so the slicer can track exposure on slow /
partial fills. Instead of trusting a placeorder() ack as a "filled"
event, the loop now:

  1. Subscribes once to the broker's order-update feed.
  2. Folds every incoming event into :func:`ledger.record_fill`.
  3. Reads the durable ``filled_quantity`` BEFORE calculating the
     next slice, so the math is always anchored to the broker's
     truth.

The module supports two transport paths:

  * **OpenAlgo event-bus** (preferred when running inside the
    OpenAlgo platform) - the WebSocket proxy publishes order
    updates on a shared in-process bus, and we subscribe by name.
  * **Direct SDK WebSocket** (fallback for the standalone SDK
    framework) - we open ``client.order_update_stream()`` and
    iterate over its message generator.

The choice is automatic: if ``utils.event_bus.bus`` is importable
we use it; otherwise we fall back to the SDK. Both paths are
fire-and-forget: a callback running on a background thread is
the only place broker data is consumed.

Design rules
------------
* Subscription is idempotent: multiple POV runs in the same
  process share one bus subscription.
* Every event normalises the status to the same five strings
  used by the ledger: ``open``, ``filled``, ``cancelled``,
  ``rejected``, ``complete``.
* Any SDK error is logged and swallowed - the durable ledger
  still has the claim record, so a missed event does not lose
  the child's lifecycle.
"""
from __future__ import annotations
import logging
import threading
from typing import Any, Callable, Optional
logger = logging.getLogger(__name__)

# Status normalisation. The broker uses several synonyms ("complete",
# "completed", "traded", "executed", "filled"); we collapse them to
# the canonical ledger vocabulary.
_FILLED_STATUSES = {"complete", "completed", "filled", "executed", "traded"}
_CANCELLED_STATUSES = {"cancelled", "canceled"}
_REJECTED_STATUSES = {"rejected", "rejected_by_broker", "error"}
_OPEN_STATUSES = {"open", "pending", "trigger pending", "accepted", "open pending", "openpending"}

_SUBSCRIPTION_LOCK = threading.Lock()
_SUBSCRIBED = False
# ---------------------------------------------------------------------------
# Status normalisation
# ---------------------------------------------------------------------------
def normalize_status(raw: Any) -> str:
    """Map any broker-specific status string to the canonical set.

    Returns one of ``"open"``, ``"filled"``, ``"cancelled"``,
    ``"rejected"``, or ``"unknown"``. Comparison is case-insensitive
    and whitespace is stripped, so ``"  COMPLETE  "`` is the same
    as ``"complete"``.
    """
    if raw is None:
        return "unknown"
    text = str(raw).strip().lower()
    if not text:
        return "unknown"
    if text in _FILLED_STATUSES:
        return "filled"
    if text in _CANCELLED_STATUSES:
        return "cancelled"
    if text in _REJECTED_STATUSES:
        return "rejected"
    if text in _OPEN_STATUSES:
        return "open"
    # Be conservative: anything we don't recognise stays "open" so
    # the ledger never accidentally releases a reservation on an
    # unrecognised terminal state.
    return "open"


# ---------------------------------------------------------------------------
# Public API - subscription
# ---------------------------------------------------------------------------
def _on_update(event: Any) -> None:
    """Default bus callback: route every update into the ledger.

    Feature 13: when the event carries a fill price (avg_price /
    average_price / price), we also compute slippage in basis points
    using ``max(session_vwap, arrival_vwap)`` as the benchmark and
    append the sample to the rolling slippage window.
    """
    # Import lazily to avoid a hard dependency on database.pov_db
    # when the framework is used outside the OpenAlgo platform.
    from blue_engine.core import ledger
    from blue_engine.core.benchmark import (
        compute_slippage_bps,
        resolve_expected_price,
    )

    order_id = None
    if isinstance(event, dict):
        order_id = event.get("orderid") or event.get("order_id")
    else:
        order_id = getattr(event, "orderid", None) or getattr(event, "order_id", None)
    if not order_id:
        return
    if isinstance(event, dict):
        status = normalize_status(event.get("status") or event.get("order_status"))
        filled_qty = int(event.get("filled_quantity") or event.get("filled_qty") or 0)
        fill_price = (
            event.get("average_price")
            or event.get("avg_price")
            or event.get("fill_price")
            or event.get("price")
        )
        side = (event.get("action") or event.get("side") or "BUY")
    else:
        status = normalize_status(getattr(event, "order_status", None))
        filled_qty = int(getattr(event, "filled_quantity", 0) or 0)
        fill_price = (
            getattr(event, "average_price", None)
            or getattr(event, "avg_price", None)
            or getattr(event, "fill_price", None)
            or getattr(event, "price", None)
        )
        side = getattr(event, "action", None) or getattr(event, "side", None) or "BUY"
    try:
        ledger.record_fill(str(order_id), filled_qty, status)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "blue_engine.order_updates: record_fill failed for %s: %s",
            order_id, exc,
        )

    # ---- Feature 13: Phase 2 (slippage) bookkeeping -------------------- #
    # Only compute slippage for actual fills (not open/pending updates).
    if status != "filled" or filled_qty <= 0 or not fill_price:
        return
    try:
        fill_price_f = float(fill_price)
    except (TypeError, ValueError):
        return
    execution_id = _find_execution_id_for_order(str(order_id))
    if not execution_id:
        return
    try:
        session_vwap = ledger.get_vwap(execution_id)
        arrival_vwap = ledger.get_arrival_vwap(execution_id)
        expected = resolve_expected_price(
            session_vwap=session_vwap, arrival_vwap=arrival_vwap,
        )
        if expected is None or expected <= 0:
            return
        bps = compute_slippage_bps(
            expected_price=expected,
            actual_price=fill_price_f,
            side=str(side or "BUY"),
        )
        ledger.record_slippage_sample(execution_id, bps)
        logger.info(
            "blue_engine.order_updates: fill order_id=%s expected=%.2f "
            "actual=%.2f slippage_bps=%.1f samples=%d",
            order_id, expected, fill_price_f, bps,
            ledger.slippage_sample_count(execution_id),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "blue_engine.order_updates: slippage update failed for %s: %s",
            order_id, exc,
        )
    finally:
        # Always close the arrival window for this child so the next
        # order starts with a fresh sample set.
        try:
            ledger.close_arrival_window(execution_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Order-id -> execution-id resolver
# ---------------------------------------------------------------------------
# The default ledger stores children per-execution; we need a cheap lookup
# to map an incoming order_id back to its execution_id. The DB-backed path
# already supports this via PovExecution.children_json; for the in-memory
# path we scan _IN_MEMORY_EXECUTIONS.
# ---------------------------------------------------------------------------
def _find_execution_id_for_order(order_id: str) -> Optional[str]:
    """Return the execution_id that owns ``order_id``, or ``None``."""
    if not order_id:
        return None
    from blue_engine.core import ledger
    # ---- DB path ----
    if ledger.ledger_available():
        try:
            from database import pov_db as _pov_db  # type: ignore
            rows = _pov_db.list_recoverable_executions()  # type: ignore[attr-defined]
            for row in rows or []:
                children = (
                    row.children_json if hasattr(row, "children_json") else "[]"
                )
                try:
                    import json
                    parsed = json.loads(children or "[]")
                except Exception:
                    continue
                for child in parsed or []:
                    if str(child.get("orderid") or "") == str(order_id):
                        return getattr(row, "id", None) or getattr(
                            row, "execution_id", None
                        )
        except Exception:
            pass
    # ---- In-memory path ----
    try:
        for eid, row in ledger._IN_MEMORY_EXECUTIONS.items():  # type: ignore[attr-defined]
            for child in getattr(row, "children", []) or []:
                if str(child.get("orderid") or "") == str(order_id):
                    return eid
    except Exception:
        return None
    return None


def subscribe_to_order_updates(
    client: Any = None,
    *,
    callback: Optional[Callable[[Any], None]] = None,
    name: str = "POVOrderUpdates",
) -> bool:
    """Hook the broker's order-update feed into the ledger.

    Tries the OpenAlgo event-bus first; falls back to a direct
    SDK WebSocket subscription when the platform is not in the
    import path. Safe to call multiple times - the bus path is
    idempotent and the SDK path always opens a new generator.

    Args:
        client:   An initialized OpenAlgo SDK client. Only needed
                  for the SDK fallback path.
        callback: Optional callback to override the default
                  :func:`_on_update`. The callback receives the raw
                  event dict / object.
        name:     Bus subscription name (used for idempotency).

    Returns:
        ``True`` if a subscription was wired up, ``False`` when no
        bus was available AND the SDK has no ``order_update_stream``
        method. Never raises.
    """
    global _SUBSCRIBED
    handler = callback or _on_update

    # Path 1: the OpenAlgo platform's in-process bus. Most reliable
    # because the WebSocket proxy already normalises events.
    try:
        from utils.event_bus import bus  # type: ignore
    except Exception:
        bus = None  # type: ignore
    if bus is not None:
        with _SUBSCRIPTION_LOCK:
            if not _SUBSCRIBED:
                bus.subscribe("order.update", handler, name=name)
                _SUBSCRIBED = True
                logger.info(
                    "blue_engine.order_updates: subscribed to event bus as %r", name
                )
        return True

    # Path 2: SDK fallback. The SDK's ``order_update_stream()``
    # returns an iterator of event dicts. We spawn a daemon thread
    # that consumes the iterator until the SDK closes it.
    if client is None:
        logger.warning(
            "blue_engine.order_updates: no event bus and no SDK client; "
            "order updates will NOT be tracked via WebSocket."
        )
        return False
    method = getattr(client, "order_update_stream", None) or getattr(
        client, "orderupdates", None
    )
    if method is None:
        logger.warning(
            "blue_engine.order_updates: SDK has no order_update_stream; "
            "ledger will rely on placeorder acks only."
        )
        return False

    def _consume() -> None:
        try:
            for event in method():
                try:
                    handler(event)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "blue_engine.order_updates: handler raised: %s", exc
                    )
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "blue_engine.order_updates: SDK stream terminated: %s", exc
            )

    thread = threading.Thread(
        target=_consume, name=f"pov-order-updates-{name}", daemon=True
    )
    thread.start()
    logger.info(
        "blue_engine.order_updates: SDK WebSocket consumer thread started"
    )
    return True


__all__ = [
        "normalize_status",
        "subscribe_to_order_updates",
    ]
