"""POV execution engine wrapper: Blue plans the slice, the Risk gate still owns the order.

Flow stage: Algo router, Approved order queue
Status: PHASE 5 IMPLEMENTED (wrap; planning only, never sends an order)
Existing code to reuse or wrap (do not copy): Blue_engine/pov/pov_engine.py (PovEngine),
Blue_engine/pov/slicers/ (POVSlicer)

WHY THIS WRAPPER NEVER PLACES AN ORDER (AGENTS.md rule 2)
---------------------------------------------------------
Blue's ``PovEngine._run`` submits to the slicer when ``dry_run`` is False and a
client is in the context. In v5 only the Risk gate may pass an intent to
OpenAlgo, so this wrapper:

1. Always runs Blue in ``dry_run`` mode and never puts a ``client`` in the
   context. Blue's slicer path is therefore unreachable from here by construction,
   not by a flag a caller might forget.
2. Returns a :class:`PovPlan`: the child quantities, the caps that were applied
   and the reasons. A caller turns the plan into child ``TradeIntent`` objects and
   submits each one through ``risk.gate`` then ``execution.pov_executor`` - the
   same path every other order takes.

LONG-ONLY (owner, 2026-11-09)
-----------------------------
This is an investment algo: BUY and ADD, never short. A plan for an action
other than BUY is refused with status ``skipped`` and a reason, not silently
re-labelled. Exits are real and still needed (a stop must sell), so ``SELL`` is
accepted ONLY as an exit of an existing long - which the caller states with
``exit_leg=True``. Without that flag a SELL plan is refused, because it can only
be a short entry.

CAPS ARE APPLIED WHEN PLANNING, NOT WHEN SENDING
------------------------------------------------
Portfolio allocation, the economic multiplier and the POV participation ceiling
all shrink the plan here, before an intent exists. A child can never be larger
than the plan, and the gate can only shrink it further.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load
from quant.execution.router import pov_child_qty

__all__ = ["PovEngineUnavailable", "PovPlan", "plan_pov", "child_quantities"]

#: Participation ceiling. AGENTS.md lets the POV agent vary the rate inside a
#: band; outside it, a slice is either meaningless (near zero) or a market
#: order in disguise (above 25% of average volume).
MIN_POV = 0.01
MAX_POV = 0.25


class PovEngineUnavailable(RuntimeError):
    """Blue's POV engine cannot be loaded."""


def _engine_cls():
    try:
        module = load("pov.pov_engine")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise PovEngineUnavailable(f"Blue POV engine unavailable: {exc}") from exc
    return module.PovEngine


@dataclass(frozen=True)
class PovPlan:
    """What the slicer may send, in total. An intent, never an order."""

    status: str                                  # planned | dry_run | skipped | error
    symbol: str = ""
    exchange: str = "NSE"
    action: str = "BUY"
    quantity: int = 0
    pov: Optional[float] = None
    config: Dict[str, Any] = field(default_factory=dict)
    economic_guard: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    reason: str = ""

    @property
    def sends_order(self) -> bool:
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "symbol": self.symbol,
            "exchange": self.exchange, "action": self.action,
            "quantity": self.quantity, "pov": self.pov,
            "config": dict(self.config),
            "economic_guard": dict(self.economic_guard),
            "warnings": list(self.warnings), "reason": self.reason,
            "sends_order": False,
        }


def pov_rate(config: Optional[Dict[str, Any]], *, default: float = 0.1) -> float:
    """Participation rate from config, clamped to the band. Never raises.

    An unreadable or out-of-band rate is NOT passed through: it is clamped and
    the clamp is visible to the caller (``clamp_pov`` returns the reason).
    """
    return clamp_pov(config, default=default)[0]


def clamp_pov(config: Optional[Dict[str, Any]], *, default: float = 0.1):
    """(rate, reason). Non-numeric -> default; outside the band -> clamped."""
    raw = None
    if isinstance(config, dict):
        for key in ("participation_pct", "pov", "participation"):
            if config.get(key) is not None:
                raw = config.get(key)
                break
    if raw is None:
        return default, ""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default, f"unreadable participation rate {raw!r}: default {default} used"
    if value > 1.0:                     # percent form, e.g. 10 -> 0.10
        value = value / 100.0
    if value != value:
        return default, "participation rate is NaN: default used"
    if value < MIN_POV:
        return MIN_POV, f"participation {value:.4f} below the band: floored at {MIN_POV}"
    if value > MAX_POV:
        return MAX_POV, f"participation {value:.4f} above the band: capped at {MAX_POV}"
    return value, ""


def plan_pov(config: Optional[Dict[str, Any]] = None,
             context: Optional[Dict[str, Any]] = None,
             provider: Optional[Any] = None,
             *, exit_leg: bool = False) -> PovPlan:
    """Run Blue's POV engine in dry-run and return the plan it would send.

    The context must carry ``symbol`` or ``alpha.selected`` plus a positive
    ``quantity`` (directly, in the config, or from the portfolio block). A
    missing quantity is a skip, not a guess.

    ``exit_leg=True`` declares that a SELL closes an existing long. Anything
    else is a short entry and is refused (long-only investment book).
    """
    requested = str((context or {}).get("action")
                    or ((config or {}).get("pov") or {}).get("action")
                    or "BUY").upper()
    if requested in ("SELL", "SHORT") and not exit_leg:
        return PovPlan(
            status="skipped", symbol=str((context or {}).get("symbol") or ""),
            action=requested, quantity=0, pov=None,
            warnings=[f"long-only: {requested} refused; pass exit_leg=True only "
                      "to close an existing position"],
            reason="long_only_no_short")
    cls = _engine_cls()
    cfg = dict(config or {})
    engine = cls(cfg, provider=provider)
    ctx = dict(context or {})
    ctx["dry_run"] = True                       # forced: a caller cannot unset this
    ctx.pop("client", None)                     # no client can reach the slicer
    result = engine.run(ctx)

    data = dict(getattr(result, "data", {}) or {})
    slice_block = data.get("slice") if isinstance(data.get("slice"), dict) else data
    cfg_out = dict(slice_block.get("config") or {})
    warnings = list(getattr(result, "warnings", []) or []) + list(data.get("warnings") or [])

    rate, why = clamp_pov(cfg_out or cfg)
    if why:
        warnings.append(why)
    if cfg_out and (why or "pov" in cfg_out or "participation" in cfg_out):
        # Report the rate the plan will actually use, even when the caller gave a
        # percent form (10 -> 0.10) that needed no clamp.
        cfg_out["participation_pct"] = rate
        cfg_out.pop("pov", None)
        cfg_out.pop("participation", None)

    # Blue reports ``dry_run`` when it built a config but sent nothing, which is
    # every call from here. That is a PLAN, and calling it dry_run would confuse
    # a reader into thinking it was a rehearsal of a different path.
    blue_status = str(slice_block.get("status") or "")
    if blue_status in ("dry_run", "executed"):
        status = "planned"
    else:
        status = blue_status or ("error" if not getattr(result, "ok", True) else "planned")
    quantity = 0
    try:
        quantity = int(cfg_out.get("quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0
    if not getattr(result, "ok", True):
        status = "error"

    # The REQUEST wins over Blue's config echo. Blue copies the action it was
    # given from the context and defaults to BUY, so reading the echo could
    # report a plan the caller never asked for.
    planned_action = str(ctx.get("action")
                         or ((config or {}).get("pov") or {}).get("action")
                         or cfg_out.get("action") or "BUY").upper()
    if planned_action in ("SELL", "SHORT") and not exit_leg:
        return PovPlan(status="skipped", symbol=str(cfg_out.get("symbol") or ""),
                       action=planned_action, quantity=0, pov=rate,
                       config=cfg_out, warnings=warnings + [
                           f"long-only: {planned_action} planned by Blue refused "
                           "without exit_leg=True"],
                       reason="long_only_no_short")

    return PovPlan(
        status=status,
        symbol=str(cfg_out.get("symbol") or ctx.get("symbol") or ""),
        exchange=str(cfg_out.get("exchange") or ctx.get("exchange") or "NSE"),
        # ``requested`` is what the caller asked for, and it is what the long-only
        # check above tested. Reporting Blue's own default instead would make the
        # plan disagree with the decision already taken on it.
        action=planned_action,
        quantity=max(0, quantity),
        pov=rate,
        config=cfg_out,
        economic_guard=dict(cfg_out.get("economic_guard") or {}),
        warnings=warnings,
        reason=str(slice_block.get("reason") or getattr(result, "error", "") or ""),
    )


def child_quantities(remaining: int, bar_volumes: List[float], pov: float) -> List[int]:
    """Child sizes for a sequence of bar volumes. Sums to ``remaining`` exactly.

    The last child takes the remainder, so no share is silently left unsent and
    no share is sent twice. ``pov`` outside the band is clamped, not trusted.
    """
    if remaining < 0:
        raise ValueError("remaining must be zero or more")
    rate, _ = clamp_pov({"pov": pov})
    out: List[int] = []
    left = int(remaining)
    for vol in bar_volumes:
        if left <= 0:
            break
        q = pov_child_qty(left, float(vol or 0.0), rate)
        if q <= 0:
            continue
        out.append(q)
        left -= q
    if left > 0:
        out.append(left)
    return out
