"""POV engine loop (decouple 6.5, Phase D). PAPER ONLY.

Flow stage: POV child sizing -> Algo router -> Risk gate -> Approved order queue -> paper broker
Status: PHASE 3 IMPLEMENTED (paper only; no live path; tested with fakes and the real gate)

Per poll (one pass of the loop):
  1. snapshot: read the top-of-book and the volume since the last poll. No snapshot counts as a
     miss; too many misses in a row stops the run (fail closed).
  2. reconcile: ask the paper sender which resting child has filled at the new touch.
  3. guards: evaluate_guards (circuit, auction, depth). A pause skips this poll.
  4. peg: PegEngine gives the LIMIT price (passive, repeg, or cross).
  5. stale cancel: if the peg price moved, cancel the resting child (a reduce-risk action, no gate).
  6. size: poll_slice_qty(delta, pov%), then the guard's depth cap, then clamp to remaining and lot.
  7. submit: make_child -> PovExecutor.submit_child (gate -> queue -> sender). A gate refusal stops
     the run with status "refused".
  8. timeout: after max_polls, any resting child is cancelled and the run ends with status "timeout".

Rules:
  - The loop refuses any sender that is not PaperChildSender (TypeError). There is no live path here.
  - The data source is injected through SnapshotSource. This module does not choose a feed.
  - No module-level state. Clock and sleep are injected, so tests run without waiting.
  - Every decision is audited through the audit sink passed to the executor.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from quant.core.types import GateDecision, TradeIntent
from quant.execution.pov_executor import AuditSink, PovExecutor
from quant.execution.pov_guards import TopOfBook, evaluate_guards
from quant.execution.pov_peg import PegEngine
from quant.execution.pov_sizing import clamp_child_qty, poll_slice_qty


@dataclass(frozen=True)
class PovSnapshot:
    """One poll's market view: the touch and the volume traded since the previous poll."""

    top: TopOfBook
    delta_volume: float


class SnapshotSource(Protocol):
    """Injected data source. Returns None when no usable snapshot is available."""

    def snapshot(self, symbol: str) -> Optional[PovSnapshot]: ...


class PaperChildSender:
    """Paper-only child sender. MARKET fills at the reference price plus slippage (simulated, no network).

    LIMIT children fill only when the limit crosses the current touch (BUY limit >= best ask,
    SELL limit <= best bid). Otherwise they rest as OPEN until reconciled, cancelled or the run
    times out. Partial fills are not modelled: a resting child fills in full or not at all.
    """

    def __init__(self, slippage_bps: float = 5.0) -> None:
        if slippage_bps < 0:
            raise ValueError("slippage_bps must be >= 0")
        self._slippage = slippage_bps / 10_000
        self._touch: Optional[TopOfBook] = None
        self._open: Dict[str, Dict[str, Any]] = {}
        self._n = 0

    def set_touch(self, top: TopOfBook) -> None:
        self._touch = top

    @property
    def open_order_ids(self) -> Tuple[str, ...]:
        return tuple(self._open)

    def _crosses(self, side: str, limit: float) -> bool:
        if self._touch is None or not self._touch.has_top:
            return False
        if side == "BUY":
            return limit >= self._touch.best_ask
        return limit <= self._touch.best_bid

    def place_child(
        self,
        intent: TradeIntent,
        api_key: str,
        decision: GateDecision,
        price_type: str = "MARKET",
        limit_price: Optional[float] = None,
    ) -> Dict:
        if not decision.approved:
            raise PermissionError("paper sender refuses an unapproved decision")
        if price_type == "MARKET":
            # Simulated fill at the reference price plus slippage against the taker. No network.
            px = intent.ref_price * (1 + self._slippage if intent.side == "BUY" else 1 - self._slippage)
            self._n += 1
            return {"order_id": f"PAPER-{self._n}", "status": "FILLED", "filled_qty": intent.qty,
                    "avg_price": px}
        if price_type != "LIMIT" or limit_price is None or not math.isfinite(limit_price) or limit_price <= 0:
            raise ValueError("LIMIT child needs a finite, positive limit_price")
        self._n += 1
        oid = f"PAPER-L{self._n}"
        if self._crosses(intent.side, limit_price):
            return {"order_id": oid, "status": "FILLED", "filled_qty": intent.qty, "avg_price": limit_price}
        self._open[oid] = {"side": intent.side, "qty": intent.qty, "limit": limit_price,
                           "strategy": intent.strategy_id}
        return {"order_id": oid, "status": "OPEN", "filled_qty": 0, "avg_price": None}

    def reconcile(self) -> Dict[str, int]:
        """Fill resting children that now cross the touch. Returns {order_id: filled_qty}."""
        filled: Dict[str, int] = {}
        for oid in list(self._open):
            order = self._open[oid]
            if self._crosses(order["side"], order["limit"]):
                filled[oid] = int(order["qty"])
                del self._open[oid]
        return filled

    def cancel(self, order_id: str, strategy: str, api_key: str) -> Dict:
        if order_id in self._open:
            del self._open[order_id]
            return {"order_id": order_id, "status": "CANCELLED"}
        return {"order_id": order_id, "status": "NOT_FOUND"}


@dataclass
class PovRunConfig:
    pov_pct: float = 10.0                    # participation share of each poll's volume
    max_slice: Optional[int] = None          # per-child cap; None means no cap
    max_polls: int = 200
    poll_seconds: float = 1.0
    max_missing_snapshots: int = 5           # consecutive misses before the run stops
    peg: Dict[str, Any] = field(default_factory=dict)     # peg_* keys for PegEngine
    guard: Dict[str, Any] = field(default_factory=dict)   # cfg for evaluate_guards (except slice_qty)


@dataclass
class PovRunResult:
    status: str                              # complete | timeout | data_unavailable | refused
    filled: int
    remaining: int
    child_count: int
    events: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class _Resting:
    order_id: str
    intent_id: str
    qty: int
    limit: float


class PovEngineLoop:
    def __init__(
        self,
        *,
        executor: PovExecutor,
        sender: PaperChildSender,
        api_key: str,
        source: SnapshotSource,
        audit: AuditSink,
        config: PovRunConfig,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(sender, PaperChildSender):
            raise TypeError("POV engine loop is paper only: sender must be PaperChildSender")
        if config.max_polls < 1 or config.poll_seconds < 0 or config.max_missing_snapshots < 0:
            raise ValueError("bad POV run config")
        if not 0 < config.pov_pct <= 100:
            raise ValueError("pov_pct must be in (0, 100]")
        if not api_key:
            raise ValueError("POV engine loop needs an API key")
        self._executor = executor
        self._sender = sender
        self._api_key = api_key
        self._source = source
        self._audit = audit
        self._cfg = config
        self._clock = clock
        self._sleep = sleep

    def _event(self, events: List[Dict[str, Any]], kind: str, **payload: Any) -> None:
        rec = {"kind": kind, **payload}
        events.append(rec)
        self._audit.append("pov_" + kind, payload)

    def _cancel(self, resting: _Resting, parent: TradeIntent, events: List[Dict[str, Any]], why: str) -> None:
        res = self._sender.cancel(resting.order_id, parent.strategy_id, self._api_key)
        self._event(events, "cancel", order_id=resting.order_id, why=why, status=res.get("status"))

    def run(self, parent: TradeIntent) -> PovRunResult:
        cfg = self._cfg
        events: List[Dict[str, Any]] = []
        remaining = int(parent.qty)
        filled = 0
        child_count = 0
        seq = 0
        missing = 0
        resting: Optional[_Resting] = None
        peg = PegEngine(cfg.peg)

        def result(status: str) -> PovRunResult:
            return PovRunResult(status=status, filled=filled, remaining=remaining,
                                child_count=child_count, events=events)

        for _ in range(cfg.max_polls):
            now = self._clock()
            snap = self._source.snapshot(parent.symbol)
            if snap is None:
                missing += 1
                self._event(events, "snapshot_missing", count=missing)
                if missing > cfg.max_missing_snapshots:
                    if resting is not None:
                        self._cancel(resting, parent, events, "data_unavailable")
                        resting = None
                    return result("data_unavailable")
                self._sleep(cfg.poll_seconds)
                continue
            missing = 0
            self._sender.set_touch(snap.top)

            # Reconcile: a resting child that now crosses the touch has filled.
            fills = self._sender.reconcile()
            if resting is not None and resting.order_id in fills:
                qty = fills[resting.order_id]
                filled += qty
                remaining -= qty
                self._event(events, "fill", order_id=resting.order_id, qty=qty, filled=filled)
                resting = None
                peg.reset_for_new_slice()
            if remaining <= 0:
                return result("complete")

            slice_qty = poll_slice_qty(snap.delta_volume, cfg.pov_pct)
            guard_cfg = {**cfg.guard, "slice_qty": slice_qty}
            guard = evaluate_guards(snap.top, action=parent.side, cfg=guard_cfg)
            if guard.pause_reason:
                self._event(events, "paused", reason=guard.pause_reason)
                self._sleep(cfg.poll_seconds)
                continue

            pd = peg.decide(snap.top, action=parent.side, now_ts=now)
            if pd.price is None:
                self._sleep(cfg.poll_seconds)
                continue
            if resting is not None:
                if pd.price == resting.limit:
                    self._sleep(cfg.poll_seconds)
                    continue
                self._cancel(resting, parent, events, "stale_peg_moved")
                resting = None

            if guard.capped_qty is not None:
                slice_qty = min(slice_qty, int(guard.capped_qty))
            qty = clamp_child_qty(slice_qty, remaining=remaining, max_slice=cfg.max_slice,
                                  lot_size=parent.lot_size)
            if qty <= 0:
                self._sleep(cfg.poll_seconds)
                continue

            seq += 1
            child = self._executor.make_child(parent, qty, seq)
            outcome = self._executor.submit_child(child, price_type="LIMIT", limit_price=pd.price)
            if not outcome.sent:
                self._event(events, "refused", intent_id=child.intent_id, reasons=list(outcome.reasons))
                return result("refused")
            child_count += 1
            resp = outcome.response or {}
            order_id = str(resp.get("order_id", ""))
            if resp.get("status") == "FILLED":
                filled += qty
                remaining -= qty
                self._event(events, "fill", order_id=order_id, qty=qty, filled=filled, price=pd.price,
                            reason=pd.reason)
                peg.reset_for_new_slice()
                if remaining <= 0:
                    return result("complete")
            else:
                resting = _Resting(order_id=order_id, intent_id=child.intent_id, qty=qty, limit=pd.price)
                self._event(events, "rest", order_id=order_id, qty=qty, limit=pd.price, reason=pd.reason)

            self._sleep(cfg.poll_seconds)

        if resting is not None:
            self._cancel(resting, parent, events, "timeout")
        return result("timeout")


# ---- Blue-wrapper planning API (from master). Plans only; never sends an order. ----
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
