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

Payload contract (audited 2026-10-29)
-------------------------------------
``parse_order_update`` is the ONE place that knows the event shape, and both
sources feed it:

* the OpenAlgo event bus / websocket_proxy relay / postback blueprint publish
  ``{"type": "order_update", "orderid", "order_status", "filled_quantity",
  "pending_quantity", "average_price", ...}`` with ``order_status`` in
  OpenAlgo's lowercase vocabulary (``open`` | ``complete`` | ``rejected`` |
  ``cancelled`` | ``trigger pending``);
* the SDK WebSocket delivers ``{"type": "order_update", ...}`` with the same
  snake_case names;
* some broker/webhook payloads use camelCase (``orderId``, ``orderStatus``,
  ``filledQty``), which the same extractor accepts.

``filled_quantity`` is CUMULATIVE. The ledger only ever raises a child's
filled count (``max``), so a replayed, out-of-order or downgraded event cannot
rewind it. Before this contract was enforced, a live parent never registered a
fill ("Opened"/"rejected by broker" were unrecognised), the slicer never saw
``filled_qty`` advance, and it kept placing children until its duration ran out.
"""
from __future__ import annotations
import logging
import os
import threading
from typing import Any, Callable, Optional
logger = logging.getLogger(__name__)

# Status normalisation. The vocabulary is OpenAlgo's own (lowercase, see
# openalgo/events/order_events.py and the broker order adapters:
# "open" | "complete" | "rejected" | "cancelled" | "trigger pending").
# Every entry below is an observed spelling from at least one of those
# sources; underscore/space variants are normalised by the fallback below.
_FILLED_STATUSES = {"complete", "completed", "filled", "executed", "traded"}
_CANCELLED_STATUSES = {"cancelled", "canceled"}
_REJECTED_STATUSES = {
    "rejected", "rejected by broker", "rejected by exchange", "rejected by rms",
    "reject", "error",
}
_OPEN_STATUSES = {
    "open", "opened", "pending", "trigger pending", "accepted", "open pending",
    "openpending", "put order req received", "validation pending",
    "modify validation pending", "modify pending", "cancel pending",
    "amo req received", "after market order req received", "modified",
    "update",
}

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
    # Fallback on the underscores the SDK/broker layers sometimes emit
    # ("trigger_pending", "rejected_by_broker", "open_pending").
    alt = text.replace("_", " ")
    if alt != text:
        return normalize_status(alt)
    # Be conservative: anything we don't recognise stays "open" so
    # the ledger never accidentally releases a reservation on an
    # unrecognised terminal state.
    return "open"


# ---------------------------------------------------------------------------
# Event field extraction
# ---------------------------------------------------------------------------
#: Field names seen for each logical value. The OpenAlgo event bus dataclass,
#: event-bus dicts published onto the event bus, the websocket_proxy relay
#: (subscribers/wsproxy_subscriber.py) and ``client.get_orders()`` all use the
#: snake_case names; older/broker payloads use the camelCase and short forms.
_ORDER_ID_KEYS = ("orderid", "order_id", "orderId")
_STATUS_KEYS = ("order_status", "status", "orderStatus")
_FILLED_KEYS = ("filled_quantity", "filledqty", "filled_qty", "filledQty",
                "filled")
_AVG_PRICE_KEYS = ("average_price", "avg_price", "avgprice", "avgprc",
                   "fill_price", "fillprice")
#: Fallback ONLY for an event that is already a fill. An order's own LIMIT
#: price is not its fill price, so an ACK never borrows it.
_PRICE_FALLBACK_KEYS = ("price",)
_ACTION_KEYS = ("action", "side", "transaction_type")
_PENDING_KEYS = ("pending_quantity", "pendingqty", "pending_qty")


class _Missing:
    """Sentinel for "field absent" (distinct from a 0 value)."""


_MISSING = _Missing()


def _field(event: Any, keys) -> Any:
    """First present value of ``keys`` on a dict or an object, else sentinel."""
    for key in keys:
        if isinstance(event, dict):
            if key in event and event[key] is not None:
                return event[key]
        else:
            value = getattr(event, key, None)
            if value is not None:
                return value
    return _MISSING


def _int_or_none(value: Any) -> Optional[int]:
    """Parse a broker quantity or return None. Never raises."""
    if value is _MISSING or value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    """Parse a broker price or return None. Never raises."""
    if value is _MISSING or value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _filled_from_pending(event: Any) -> int:
    """Derive a cumulative fill from an order that reports only quantity.

    Several OpenAlgo paths publish ``quantity`` and ``pending_quantity`` but
    no ``filled_quantity`` (an ACK before the first fill, and some broker
    adapters). ``quantity - pending`` is then the cumulative fill, and it is
    accepted ONLY when both numbers are present, non-negative and ordered; a
    feed that omits the order quantity cannot prove a fill, so it returns 0
    and the ledger keeps the maximum it already has.
    """
    quantity = _int_or_none(_field(event, ("quantity", "qty")))
    pending = _int_or_none(_field(event, _PENDING_KEYS))
    if quantity is None or pending is None:
        return 0
    if quantity < 0 or pending < 0 or pending > quantity:
        return 0
    return quantity - pending


def parse_order_update(event: Any) -> Optional[tuple]:
    """Extract ``(order_id, status, filled_qty, fill_price, side)``.

    This is the SINGLE place that knows the broker/OpenAlgo order-update
    shape. It accepts:

    * a dict published on the event bus or relayed over the WebSocket
      (``websocket_proxy/subscribers`` payload: ``orderid``, ``order_status``,
      ``filled_quantity``, ``pending_quantity``, ``average_price``), and
    * the ``OrderUpdateEvent`` dataclass / any object with the same names.

    Returns ``None`` when the event carries no order id (heartbeats, acks),
    so callers can ``if parsed is None: return`` without inspecting it.

    ``filled_qty`` is the CUMULATIVE filled quantity, never a delta: the
    ledger keeps the max it has seen, so a replayed or out-of-order event
    cannot double-count. Missing quantities parse to ``0`` and a missing
    price parses to ``None``, never to ``0.0`` (a zero price would poison the
    slippage window).
    """
    order_id = _field(event, _ORDER_ID_KEYS)
    if order_id is _MISSING or not order_id:
        return None
    status = normalize_status(_field(event, _STATUS_KEYS))
    filled = _int_or_none(_field(event, _FILLED_KEYS))
    if filled is None:
        filled = _filled_from_pending(event)
    fill_price = _float_or_none(_field(event, _AVG_PRICE_KEYS))
    # Feeds that send 0 (or "") for average_price before the first fill.
    if fill_price is not None and fill_price <= 0:
        fill_price = None
    if fill_price is None and status == "filled":
        # An ACK that is still working carries the LIMIT price, not a fill;
        # only a fill may fall back to it (and only as a slippage estimate).
        fill_price = _float_or_none(_field(event, _PRICE_FALLBACK_KEYS))
    side_value = _field(event, _ACTION_KEYS)
    side = str(side_value).upper() if side_value is not _MISSING and side_value else "BUY"
    return (str(order_id), status, max(0, int(filled)), fill_price, side)


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
    parsed = parse_order_update(event)
    if parsed is None:
        return
    order_id, status, filled_qty, fill_price, side = parsed
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
    global _SUBSCRIBED  # assigned in the SDK path below
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

    # Path 2: the SDK client's callback subscription.
    #
    # AUDIT (2026-10-29): the previous code probed ``order_update_stream()`` /
    # ``orderupdates()``, which the installed SDK does not have. The guarded
    # return False looked like a clean degradation, so a live run logged
    # "degraded" and then relied on placeorder ACKs alone - the ledger never
    # learned about a fill. openalgo/feed.py exposes the real API:
    # ``client.subscribe_orders(on_order_update=callable) -> bool``, which
    # pushes the dict documented in its docstring (the same shape
    # ``parse_order_update`` now understands).
    if client is None:
        logger.warning(
            "blue_engine.order_updates: no event bus and no SDK client; "
            "order updates will NOT be tracked via WebSocket."
        )
        return False

    def _dispatch(event: Any) -> None:
        """Run the handler; never let a handler error kill the feed."""
        try:
            handler(event)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("blue_engine.order_updates: handler raised: %s", exc)

    # AUDIT (2026-10-29, LIVE-9): the SDK feed authenticates with the SAME key
    # the client was constructed with (feed.py sends ``self.api_key``). The
    # runner resolves that key from the OpenAlgo DB when OPENALGO_API_KEY is
    # unset, but this module assumed the env var; the two could then be
    # different accounts, which would silently stream another user's orders.
    key = getattr(client, "api_key", None)
    if key and not os.getenv("OPENALGO_API_KEY"):
        os.environ["OPENALGO_API_KEY"] = str(key)

    subscribe = getattr(client, "subscribe_orders", None)
    if callable(subscribe):
        with _SUBSCRIPTION_LOCK:
            if _SUBSCRIBED:
                return True
            try:
                accepted = bool(subscribe(on_order_update=_dispatch))
            except TypeError:
                # Older signatures take no keyword.
                try:
                    accepted = bool(subscribe(_dispatch))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "blue_engine.order_updates: subscribe_orders failed: %s", exc)
                    accepted = False
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "blue_engine.order_updates: subscribe_orders failed: %s", exc)
                accepted = False
            _SUBSCRIBED = accepted
        if accepted:
            logger.info(
                "blue_engine.order_updates: SDK order-update subscription active")
        else:
            logger.warning(
                "blue_engine.order_updates: SDK rejected the order-update "
                "subscription (socket not connected/authenticated); fills "
                "will not advance until it is re-established")
        return accepted

    logger.warning(
        "blue_engine.order_updates: SDK client has no subscribe_orders(); "
        "ledger will rely on placeorder acks and REST orderbook polling."
    )
    return False


__all__ = [
        "normalize_status",
        "parse_order_update",
        "subscribe_to_order_updates",
    ]
