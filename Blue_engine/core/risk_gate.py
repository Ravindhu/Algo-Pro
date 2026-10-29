"""
risk_gate.py - pre-trade risk gate for the blue_engine runner.

Why this exists
---------------
The POV slicer is a *good* executor but it is not a risk manager: it will
happily slice whatever quantity it is handed. Before this module, the
config-driven runner path (``blue_engine.core.runner``) handed the signal's
intent straight to ``POVSlicer.execute`` with no cap on size, notional or
daily loss. That is fine in paper mode and dangerous in live mode.

This gate is a small, *pure*, dependency-free check that runs ONCE, after the
signal fires and BEFORE the slicer is built. It answers one question:

    "Is this intent allowed to trade right now?"

Design rules (mirroring ``blue_engine.core.base``)
--------------------------------------------------
* PURE: no network, no SDK, no DB. It only reads the intent + config + the
  optional daily-PnL figure the caller passes in.
* FAIL-CLOSED when a configured limit cannot be evaluated: a *breach* rejects
  the intent, and required price/P&L/exposure inputs must be verified. A
  missing limit means "not configured" and never rejects.
* The gate never raises. A malformed config is reported as a rejection reason
  with ``ok_but_unconfigured`` semantics rather than an exception, so it can
  sit on the hot path of a live loop.

Config block (all keys optional; absence = no limit)
----------------------------------------------------
    "risk": {
        "max_order_quantity":    5000,     # reject if intent.quantity exceeds
        "max_order_notional":    2_000_000,# reject if qty * price exceeds
        "price":                 1480.5,   # reference price for notional check
        "max_daily_loss":        25000,    # reject if day_pnl <= -max_daily_loss
        "max_open_exposure":     5_000_000,# reject if |open_exposure| + this
        "open_exposure":         1_200_000,#   notional exceeds the cap
        "allowed_actions":       ["BUY", "SELL"],
        "blocked":               false,    # master kill-switch
        "require_live_ack":      false     # when true, live mode w/o risk block
                                           #   is rejected (see below)
    }

Example (reject a 9,000-share order when the cap is 5,000)::

    >>> gate = RiskGate({"max_order_quantity": 5000})
    >>> gate.check({"action": "BUY", "quantity": 9000})
    RiskDecision(approved=False, reason='quantity 9000 > max_order_quantity 5000', ...)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["RiskDecision", "RiskGate", "check_intent"]


@dataclass
class RiskDecision:
    """The verdict returned by :meth:`RiskGate.check`.

    Attributes
    ----------
    approved : True when the intent may proceed to the slicer.
    reason   : Human-readable explanation (always populated).
    breaches : Every individual limit that was violated (empty when approved).
    limits   : The limits that were actually evaluated (for the audit log).
    """

    approved: bool
    reason: str
    breaches: List[str] = field(default_factory=list)
    limits: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:  # allow ``if decision:`` as ``if approved``
        return self.approved


def _safe_float(value: Any) -> Optional[float]:
    """Parse to a finite float, else None. Mirrors core.results.safe_float."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return f


def _safe_int(value: Any) -> Optional[int]:
    f = _safe_float(value)
    return None if f is None else int(f)


class RiskGate:
    """Evaluate a trade intent against a JSON-configured set of limits."""

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Any] = None) -> None:
        cfg = config if isinstance(config, dict) else {}
        #: The block may arrive pre-nested ({"risk": {...}}) or flat.
        block = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else cfg
        self.config: Dict[str, Any] = dict(block or {})
        self._log = log or logger.info

        self.max_order_quantity = _safe_int(self.config.get("max_order_quantity"))
        self.max_order_notional = _safe_float(self.config.get("max_order_notional"))
        self.max_daily_loss = _safe_float(self.config.get("max_daily_loss"))
        self.max_open_exposure = _safe_float(self.config.get("max_open_exposure"))
        self.allowed_actions = [
            str(a).upper() for a in (self.config.get("allowed_actions") or ["BUY", "SELL"])
        ]
        self.blocked = bool(self.config.get("blocked", False))
        #: When true, a LIVE run with NO monetary quantity/notional/exposure/
        #: daily-loss limit is rejected (see ``check(live=...)``). Opt-in so
        #: existing analyze-mode and test callers keep their behaviour.
        self.require_live_ack = bool(self.config.get("require_live_ack", False))

    # ---- helpers ------------------------------------------------------- #
    def _reference_price(self, intent: Dict[str, Any]) -> Optional[float]:
        """Return a finite positive execution reference price, if available."""
        for value in (intent.get("price"), intent.get("limit_price")):
            if isinstance(value, bool):
                continue
            price = _safe_float(value)
            if price is not None and price > 0:
                return price
        return _safe_float(self.config.get("price"))

    def _notional(self, intent: Dict[str, Any], qty: int) -> Optional[float]:
        price = self._reference_price(intent)
        if price is None:
            return None
        return qty * price

    # ---- main ---------------------------------------------------------- #
    def check(self, intent: Dict[str, Any],
              *, day_pnl: Optional[float] = None,
              open_exposure: Optional[float] = None,
              require_intent_price: bool = False,
              reduces_exposure: bool = False,
              live: bool = False) -> RiskDecision:
        """Evaluate ``intent`` and return a :class:`RiskDecision`.

        Args:
            intent: the trade intent from a signal, with ``action`` and
                ``quantity`` (and optionally ``price``).
            day_pnl: today's realised PnL (negative = loss). Used by the
                ``max_daily_loss`` check. ``None`` rejects when that limit is
                configured.
            open_exposure: absolute current open notional. Used by the
                ``max_open_exposure`` check. Falls back to the config value.
                ``None`` rejects when that limit is configured.
            require_intent_price: reject unless a finite positive price exists
                (monetary limits cannot be evaluated without one).
            reduces_exposure: this order lowers exposure, so the daily-loss
                and exposure caps are evaluated as a reduction.
            live: the caller believes this intent reaches real capital. When
                ``require_live_ack`` is configured, a live intent with no
                monetary limit at all is rejected instead of silently
                running unlimited.

        Returns:
            RiskDecision. Never raises.
        """
        breaches: List[str] = []
        limits: Dict[str, Any] = {}
        try:
            action = str(intent.get("action", "")).upper()
            quantity = _safe_int(intent.get("quantity")) or 0
            notional = self._notional(intent, quantity)
            if require_intent_price:
                reference_price = None
                for value in (intent.get("price"), intent.get("limit_price")):
                    if isinstance(value, bool):
                        continue
                    reference_price = _safe_float(value)
                    if reference_price is not None and reference_price > 0:
                        break
                    reference_price = None
                if reference_price is None or reference_price <= 0:
                    notional = None
            limits = {
                "max_order_quantity": self.max_order_quantity,
                "max_order_notional": self.max_order_notional,
                "max_daily_loss": self.max_daily_loss,
                "max_open_exposure": self.max_open_exposure,
                "allowed_actions": self.allowed_actions,
                "blocked": self.blocked,
                "action": action,
                "quantity": quantity,
                "notional": notional,
            }

            # 0. Live acknowledgement. A live intent is not "permission to
            #    trade": the MONEY caps (notional / daily loss / exposure)
            #    are what bound a run's economic loss. `max_order_quantity` is
            #    deliberately NOT accepted as the acknowledgement, because a
            #    share count alone says nothing about rupee risk (100 shares of
            #    a Rs 50 stock and of a Rs 50,000 stock pass it identically).
            monetary_gaps = [name for name, value in (
                ("max_order_notional", self.max_order_notional),
                ("max_daily_loss", self.max_daily_loss),
                ("max_open_exposure", self.max_open_exposure),
            ) if value is None]
            if live and self.require_live_ack and monetary_gaps:
                breaches.append(
                    "live run without a monetary risk limit (require_live_ack): "
                    "missing " + ", ".join(monetary_gaps))

            # 1. Master kill-switch.
            if self.blocked:
                breaches.append("risk gate is in 'blocked' state (kill-switch on)")

            # 2. Action must be permitted.
            if action not in self.allowed_actions:
                breaches.append(
                    f"action {action!r} not in allowed_actions {self.allowed_actions}")
            if quantity <= 0:
                breaches.append(
                    f"quantity {quantity} must be a positive integer")

            # 3. Size cap.
            if (self.max_order_quantity is not None
                    and quantity > self.max_order_quantity):
                breaches.append(
                    f"quantity {quantity} > max_order_quantity {self.max_order_quantity}")

            # 4. Configured monetary limits cannot be evaluated without price.
            if self.max_order_notional is not None:
                if notional is None:
                    breaches.append(
                        "cannot evaluate max_order_notional without a "
                        "finite positive order price")
                elif notional > self.max_order_notional:
                    breaches.append(
                        f"notional {notional:.2f} > max_order_notional "
                        f"{self.max_order_notional:.2f}")

            # 5. Daily-loss cap. day_pnl is signed; a loss is negative.
            if self.max_daily_loss is not None:
                pnl = _safe_float(day_pnl)
                if pnl is None:
                    breaches.append(
                        "cannot evaluate max_daily_loss without a verified "
                        "daily P&L value")
                elif pnl <= -abs(self.max_daily_loss) and not reduces_exposure:
                    breaches.append(
                        f"day_pnl {pnl:.2f} <= -max_daily_loss "
                        f"{abs(self.max_daily_loss):.2f}")

            # 6. Open-exposure cap: current + this order must fit.
            exposure = _safe_float(open_exposure)
            if exposure is None:
                exposure = _safe_float(self.config.get("open_exposure"))
            if self.max_open_exposure is not None:
                if exposure is None:
                    breaches.append(
                        "cannot evaluate max_open_exposure without verified "
                        "current exposure")
                elif notional is None:
                    breaches.append(
                        "cannot evaluate max_open_exposure without a finite "
                        "positive order price")
                else:
                    current_exposure = abs(exposure)
                    projected = (max(0.0, current_exposure - abs(notional))
                                 if reduces_exposure
                                 else current_exposure + abs(notional))
                    if (projected > self.max_open_exposure
                            and (not reduces_exposure
                                 or projected > current_exposure)):
                        breaches.append(
                            f"projected exposure {projected:.2f} > "
                            f"max_open_exposure "
                            f"{self.max_open_exposure:.2f}")

        except Exception as exc:  # noqa: BLE001 - the gate must never raise
            logger.warning("risk_gate: evaluation error: %r", exc)
            return RiskDecision(
                approved=False,
                reason=f"risk gate could not evaluate intent ({exc!r}); "
                       f"rejecting to be safe",
                breaches=[repr(exc)],
                limits=limits,
            )

        if breaches:
            reason = "; ".join(breaches)
            self._log(f"rejected: {reason}")
            return RiskDecision(approved=False, reason=reason,
                                breaches=breaches, limits=limits)

        self._log("approved")
        return RiskDecision(approved=True, reason="all configured limits passed",
                            limits=limits)


def check_intent(intent: Dict[str, Any], config: Optional[Dict[str, Any]] = None,
                 **kwargs: Any) -> RiskDecision:
    """Convenience wrapper: build a RiskGate and check the intent once."""
    return RiskGate(config).check(intent, **kwargs)