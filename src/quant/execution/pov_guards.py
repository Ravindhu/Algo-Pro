"""
pov_guards.py (ported from src/quant/blue_engine/pov/slicers/guards.py, 2026-11-09; decouple stage 6.5 Phase B).
Status: PHASE 3 IMPLEMENTED (pure checks; the quote/depth fetch is NOT ported and stays in the Phase D loop).

guards.py - composable execution-time safety checks for POV (and any future
slicer) in the OpenAlgo-SDK-style blue_engine framework.

Three guards are provided:

    1. DepthPricingGuard   - best bid/ask-aware LIMIT pricing, never
                              crosses the spread.
    2. DepthSliceCapGuard  - caps the calculated slice to the displayed
                              quantity on the best opposite-side tier.
    3. CircuitGuard        - auto-pauses the execution loop when the LTP
                              moves within ``circuit_buffer_pct`` of the
                              exchange's upper or lower circuit.

Design rules (mirrored from blue_engine/core/client.py):

* The OpenAlgo SDK (openalgo==2.0.3) is the ONLY transport. Depth comes
  from ``client.depth(symbol=..., exchange=...)`` and quotes from
  ``client.quotes(symbol=..., exchange=...)``. Keyword-only arguments.
* Every threshold is JSON-overridable through the slicer config.
* When the underlying data is unavailable, every guard degrades to the
  pre-existing behaviour (static ceiling_price / max_slice_quantity).
  Runs NEVER stall because a depth call failed.
* All guards are unit-testable without an SDK: the pure functions accept
  a TopOfBook snapshot built from any source.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from quant.core.auction_windows import current_auction_window, is_within_auction_window

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Snapshot types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TopOfBook:
    """A normalized L2 top-of-book snapshot.

    All fields are optional so a guard can still run when only one of
    ``client.quotes()`` or ``client.depth()`` succeeds.
    """

    best_bid: Optional[float] = None
    bid_qty: Optional[int] = None
    best_ask: Optional[float] = None
    ask_qty: Optional[int] = None
    ltp: Optional[float] = None
    upper_circuit: Optional[float] = None
    lower_circuit: Optional[float] = None
    tick_size: Optional[float] = None
    # Feature 6 - auction_equilibrium fields, populated when the
    # underlying quote carries the SME/PCAS auction data.
    auction_price: Optional[float] = None
    auction_volume: Optional[float] = None
    source: str = "unknown"  # "depth" | "quote" | "merged"

    @property
    def has_bid(self) -> bool:
        return self.best_bid is not None and self.best_bid > 0

    @property
    def has_ask(self) -> bool:
        return self.best_ask is not None and self.best_ask > 0

    @property
    def has_top(self) -> bool:
        """True when at least the best bid AND best ask are known."""
        return self.has_bid and self.has_ask

    @property
    def spread(self) -> Optional[float]:
        if not self.has_top:
            return None
        spread = self.best_ask - self.best_bid
        return spread if spread > 0 else None


@dataclass
class GuardDecision:
    """The aggregated verdict returned to the slicer each poll.

    Fields default to None / 0 so any individual guard can return a
    partial decision without clobbering the others.
    """

    new_price: Optional[float] = None       # depth-aware LIMIT price
    capped_qty: Optional[int] = None        # depth-capped slice quantity
    pause_reason: Optional[str] = None      # circuit-pause signal
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers - safe number parsing
# ---------------------------------------------------------------------------
def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    parsed = _safe_float(value)
    if parsed is None:
        return default
    return int(parsed)


def _to_tick(price: float, tick: float, *, direction: str) -> float:
    """Snap ``price`` onto the tick grid, rounding INWARD for the order side.

    ``direction="down"`` floors (BUY never moves toward the ask) and
    ``"up"`` ceils (SELL never moves toward the bid). Plain nearest-tick
    rounding can push a passive price onto or across the opposite quote.
    """
    if tick <= 0:
        return price
    steps = price / tick
    n = math.floor(steps + 1e-9) if direction == "down" else math.ceil(steps - 1e-9)
    return round(n * tick, 4)


def _first_level(data: Any, key: str) -> Tuple[Optional[float], Optional[int]]:
    """Read price/quantity from the first level of a depth array."""
    if not isinstance(data, dict):
        return None, None
    level = data.get(key)
    if isinstance(level, list):
        level = level[0] if level else None
    if not isinstance(level, dict):
        return None, None
    price = _safe_float(
        level.get("price")
        or level.get("rate")
        or level.get("p")
    )
    qty = _safe_int(
        level.get("quantity")
        or level.get("qty")
        or level.get("volume")
        or level.get("q")
    )
    return price, qty


# ---------------------------------------------------------------------------
# Normalisation - SDK responses -> TopOfBook
# ---------------------------------------------------------------------------
def normalize_top_of_book(
    *,
    depth_resp: Optional[Dict[str, Any]] = None,
    quote_resp: Optional[Dict[str, Any]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> TopOfBook:
    """Build a TopOfBook from one or two SDK responses.

    The OpenAlgo SDK returns both ``client.depth()`` and ``client.quotes()``
    in the envelope ``{"status": "success", "data": {...}}``. Depth
    typically carries ``data.bids`` / ``data.asks`` and the quote carries
    ``data.ltp`` / ``data.upper_circuit_limit``. Either or both are
    accepted.
    """
    cfg = cfg or {}

    depth_data: Any = None
    quote_data: Any = None
    if isinstance(depth_resp, dict) and depth_resp.get("status") == "success":
        depth_data = depth_resp.get("data") or {}
    if isinstance(quote_resp, dict) and quote_resp.get("status") == "success":
        quote_data = quote_resp.get("data") or {}

    # Some broker quotes nest the depth under a single object - handle both.
    nested_depth = (
        depth_data.get("depth") if isinstance(depth_data, dict) else None
    )
    nested_bids_asks = (
        quote_data.get("depth") if isinstance(quote_data, dict) else None
    )

    bid_price, bid_qty = _first_level(depth_data, "bids")
    if bid_price is None:
        bid_price, bid_qty = _first_level(nested_depth, "bids")
    if bid_price is None:
        bid_price, bid_qty = _first_level(nested_bids_asks, "bids")
    if bid_price is None:
        bid_price, bid_qty = _first_level(nested_bids_asks, "buy")
    if bid_price is None:
        for alias in ("bid", "best_bid", "buy_price"):
            if isinstance(quote_data, dict) and alias in quote_data:
                bid_price = _safe_float(quote_data.get(alias))
                break

    ask_price, ask_qty = _first_level(depth_data, "asks")
    if ask_price is None:
        ask_price, ask_qty = _first_level(nested_depth, "asks")
    if ask_price is None:
        ask_price, ask_qty = _first_level(nested_bids_asks, "asks")
    if ask_price is None:
        ask_price, ask_qty = _first_level(nested_bids_asks, "sell")
    if ask_price is None:
        for alias in ("ask", "best_ask", "sell_price"):
            if isinstance(quote_data, dict) and alias in quote_data:
                ask_price = _safe_float(quote_data.get(alias))
                break

    ltp = None
    upper = None
    lower = None
    tick_size = None
    auction_price = None
    auction_volume = None
    if isinstance(quote_data, dict):
        ltp = _safe_float(
            quote_data.get("ltp")
            or quote_data.get("last_price")
            or quote_data.get("lastPrice")
        )
        upper = _safe_float(
            quote_data.get("upper_circuit_limit")
            or quote_data.get("upper_circuit")
            or quote_data.get("upperCircuit")
            or quote_data.get("uc")
        )
        lower = _safe_float(
            quote_data.get("lower_circuit_limit")
            or quote_data.get("lower_circuit")
            or quote_data.get("lowerCircuit")
            or quote_data.get("lc")
        )
        tick_size = _safe_float(
            quote_data.get("tick_size")
            or quote_data.get("ticksize")
            or quote_data.get("tickSize")
        )
        # Feature 6 - auction equilibrium price/volume, used by
        # the AuctionPriceGuard on SME/PCAS listings.
        auction_price = _safe_float(
            quote_data.get("auction_price")
            or quote_data.get("equilibrium_price")
            or quote_data.get("indicative_price")
        )
        auction_volume = _safe_float(
            quote_data.get("auction_volume")
            or quote_data.get("matched_volume")
            or quote_data.get("matched_quantity")
        )
    if isinstance(depth_data, dict):
        tick_size = tick_size or _safe_float(depth_data.get("tick_size"))

    cfg_tick = _safe_float(cfg.get("tick_size"))
    if cfg_tick:
        tick_size = tick_size or cfg_tick

    if tick_size is None or tick_size <= 0:
        tick_size = 0.05  # safe default for NSE cash equities

    if depth_data:
        source = "depth" if not quote_data else "merged"
    elif quote_data:
        source = "quote"
    else:
        source = "unknown"

    return TopOfBook(
        best_bid=bid_price if bid_price and bid_price > 0 else None,
        bid_qty=bid_qty if bid_qty and bid_qty > 0 else None,
        best_ask=ask_price if ask_price and ask_price > 0 else None,
        ask_qty=ask_qty if ask_qty and ask_qty > 0 else None,
        ltp=ltp if ltp and ltp > 0 else None,
        upper_circuit=upper if upper and upper > 0 else None,
        lower_circuit=lower if lower and lower > 0 else None,
        tick_size=tick_size,
        auction_price=auction_price if auction_price and auction_price > 0 else None,
        auction_volume=auction_volume if auction_volume and auction_volume > 0 else None,
        source=source,
    )


class BaseGuard(ABC):
    """Abstract base for every execution-time guard.

    Subclasses set ``name`` (used in JSON config and logs) and implement
    :meth:`evaluate`. The slicer calls :meth:`evaluate` once per poll,
    passing the L2 snapshot, the order action, and the slicer config.

    JSON overrides:
        "<name>_enabled": bool   - default True
        "enable_<name>":   bool  - default True (alias)
    """

    name: str = ""

    def __init__(self) -> None:
        if not self.name:
            self.name = self.__class__.__name__

    @abstractmethod
    def evaluate(
        self,
        top: TopOfBook,
        *,
        action: str,
        cfg: Dict[str, Any],
    ) -> GuardDecision:
        """Return a partial GuardDecision for this guard."""

    def is_enabled(self, cfg: Dict[str, Any]) -> bool:
        flag = f"{self.name}_enabled"
        if flag in cfg:
            return bool(cfg[flag])
        return bool(cfg.get(f"enable_{self.name}", True))


class DepthPricingGuard(BaseGuard):
    """Sets the LIMIT price to the best opposite-side quote +/- offset_ticks.

    BUY  -> ``best_bid + offset_ticks * tick_size`` (clamped below best_ask)
    SELL -> ``best_ask - offset_ticks * tick_size`` (clamped above best_bid)

    Never crosses the spread: BUY never prices at/above the ask, SELL
    never prices at/below the bid. When depth is unavailable, returns
    ``None`` for ``new_price`` so the slicer falls back to
    ``ceiling_price`` (the legacy static-cap path).
    """

    name = "depth_aware_pricing"

    def evaluate(
        self,
        top: TopOfBook,
        *,
        action: str,
        cfg: Dict[str, Any],
    ) -> GuardDecision:
        if not self.is_enabled(cfg):
            return GuardDecision(metadata={"guard": self.name, "enabled": False})
        if not top.has_top:
            return GuardDecision(
                metadata={"guard": self.name, "skipped": "no_top_of_book"}
            )

        # Default: sit ONE TICK inside the spread (best_bid + 1 tick
        # for BUY, best_ask - 1 tick for SELL). This "joins the
        # inside of the spread" and historically gives better fill
        # rates than sitting on the touch, at the cost of crossing
        # the spread one tick sooner if the price moves against us.
        # Operators who want the legacy "sit on bid/ask" behaviour
        # can explicitly set ``depth_offset_ticks: 0`` in the JSON.
        offset_ticks = max(0, _safe_int(cfg.get("depth_offset_ticks"), 1) or 0)
        tick = top.tick_size or 0.05
        action_upper = action.upper()

        if action_upper == "BUY":
            raw_price = top.best_bid + offset_ticks * tick
            # Stay at least one full tick below the ask, then floor (never up).
            new_price = min(raw_price, top.best_ask - tick)
            new_price = max(new_price, top.best_bid)
            new_price = _to_tick(new_price, tick, direction="down")
            new_price = max(new_price, top.best_bid)
        elif action_upper == "SELL":
            raw_price = top.best_ask - offset_ticks * tick
            # Stay at least one full tick above the bid, then ceil (never down).
            new_price = max(raw_price, top.best_bid + tick)
            new_price = min(new_price, top.best_ask)
            new_price = _to_tick(new_price, tick, direction="up")
            new_price = min(new_price, top.best_ask)
        else:
            return GuardDecision(
                metadata={
                    "guard": self.name,
                    "skipped": f"unknown_action:{action_upper}",
                }
            )

        return GuardDecision(
            new_price=new_price,
            metadata={
                "guard": self.name,
                "offset_ticks": offset_ticks,
                "tick_size": tick,
                "best_bid": top.best_bid,
                "best_ask": top.best_ask,
                "raw_price": raw_price,
            },
        )


class DepthSliceCapGuard(BaseGuard):
    """Caps the child slice to the displayed quantity at the best opposite tier.

    BUY  -> ``min(slice_qty, ask_qty)``
    SELL -> ``min(slice_qty, bid_qty)``

    If the resulting quantity is below ``depth_slice_min_qty``, the guard
    sets ``metadata["skip"]="below_min_qty"`` and returns ``capped_qty=0``
    so the slicer can avoid placing a dust order. When depth is
    unavailable, the guard returns the original quantity unchanged
    (legacy behaviour).
    """

    name = "depth_slice_cap"

    def evaluate(
        self,
        top: TopOfBook,
        *,
        action: str,
        cfg: Dict[str, Any],
    ) -> GuardDecision:
        if not self.is_enabled(cfg):
            return GuardDecision(metadata={"guard": self.name, "enabled": False})

        slice_qty = _safe_int(cfg.get("slice_qty"), 0) or 0
        if slice_qty <= 0:
            return GuardDecision(
                metadata={"guard": self.name, "skipped": "no_slice_qty"}
            )

        action_upper = action.upper()
        if action_upper == "BUY":
            opposite_qty = top.ask_qty
        elif action_upper == "SELL":
            opposite_qty = top.bid_qty
        else:
            return GuardDecision(
                metadata={
                    "guard": self.name,
                    "skipped": f"unknown_action:{action_upper}",
                }
            )

        if opposite_qty is None or opposite_qty <= 0:
            # Depth missing - keep the legacy slice size untouched.
            return GuardDecision(
                capped_qty=slice_qty,
                metadata={"guard": self.name, "fallback": "no_opposite_depth"},
            )

        capped = min(slice_qty, opposite_qty)
        min_qty = max(0, _safe_int(cfg.get("depth_slice_min_qty"), 1) or 1)
        metadata: Dict[str, Any] = {
            "guard": self.name,
            "slice_qty": slice_qty,
            "opposite_qty": opposite_qty,
            "capped_qty": capped,
            "min_qty": min_qty,
        }
        if capped < min_qty:
            metadata["skip"] = "below_min_qty"
            capped = 0
        return GuardDecision(capped_qty=capped, metadata=metadata)


class CircuitGuard(BaseGuard):
    """Auto-pauses the loop when LTP is within buffer_pct of a circuit.

    BUY  triggers when ``ltp >= upper_circuit * (1 - buffer/100)``.
    SELL triggers when ``ltp <= lower_circuit * (1 + buffer/100)``.

    Defaults to a 0.5% buffer to match the playbook. ``buffer_pct=0``
    disables the guard (only pauses exactly at the circuit, which
    effectively means never).
    """

    name = "circuit_guard"

    def evaluate(
        self,
        top: TopOfBook,
        *,
        action: str,
        cfg: Dict[str, Any],
    ) -> GuardDecision:
        if not self.is_enabled(cfg):
            return GuardDecision(metadata={"guard": self.name, "enabled": False})

        if top.ltp is None or top.ltp <= 0:
            # Fail closed: without an LTP the circuit distance is unknown,
            # so pause the slice instead of trading blind near a circuit.
            if cfg.get("circuit_fail_closed", True):
                return GuardDecision(
                    pause_reason="MARKET_DATA_UNAVAILABLE",
                    metadata={"guard": self.name, "skipped": "no_ltp",
                              "fail_closed": True},
                )
            return GuardDecision(metadata={"guard": self.name, "skipped": "no_ltp"})

        buffer_pct = _safe_float(cfg.get("circuit_buffer_pct"), 0.5)
        if buffer_pct is None or buffer_pct < 0:
            buffer_pct = 0.0

        action_upper = action.upper()

        if action_upper == "BUY" and top.upper_circuit and top.upper_circuit > 0:
            threshold = top.upper_circuit * (1 - buffer_pct / 100)
            if top.ltp >= threshold:
                return GuardDecision(
                    pause_reason="NEAR_UPPER_CIRCUIT",
                    metadata={
                        "guard": self.name,
                        "ltp": top.ltp,
                        "upper_circuit": top.upper_circuit,
                        "threshold": threshold,
                        "buffer_pct": buffer_pct,
                    },
                )

        if action_upper == "SELL" and top.lower_circuit and top.lower_circuit > 0:
            threshold = top.lower_circuit * (1 + buffer_pct / 100)
            if top.ltp <= threshold:
                return GuardDecision(
                    pause_reason="NEAR_LOWER_CIRCUIT",
                    metadata={
                        "guard": self.name,
                        "ltp": top.ltp,
                        "lower_circuit": top.lower_circuit,
                        "threshold": threshold,
                        "buffer_pct": buffer_pct,
                    },
                )

        return GuardDecision(
            metadata={
                "guard": self.name,
                "ltp": top.ltp,
                "upper_circuit": top.upper_circuit,
                "lower_circuit": top.lower_circuit,
                "buffer_pct": buffer_pct,
            }
        )



# ---------------------------------------------------------------------------
# Feature 5 - AuctionWindowGuard
# ---------------------------------------------------------------------------
class AuctionWindowGuard(BaseGuard):
    """Restricts slice placement to configured call-auction windows.

    SME and Periodic Call Auction listings only execute at the
    discrete auction sessions the exchange schedules. A continuous
    child order submitted outside one of those windows is rejected
    instantly, and the broker may also flag the account for OTR
    abuse. The guard pauses the loop with ``OUTSIDE_AUCTION_WINDOW``
    whenever the current IST clock is NOT inside a configured
    ``"HH:MM-HH:MM"`` entry.

    The guard reads ``cfg["auction_windows"]`` (anything
    :func:`blue_engine.core.auction_windows.parse_auction_windows`
    accepts). A missing/empty config disables the guard so plain
    equity runs are unaffected.

    JSON overrides:
        ``auction_window_enabled`` (bool)  - master switch
        ``auction_windows`` (list[str|dict]) - the window spec
    """

    name = "auction_window"

    def evaluate(
        self,
        top: TopOfBook,
        *,
        action: str,
        cfg: Dict[str, Any],
    ) -> GuardDecision:
        if not self.is_enabled(cfg):
            return GuardDecision(
                metadata={"guard": self.name, "enabled": False}
            )
        windows = cfg.get("auction_windows")
        if not windows:
            return GuardDecision(
                metadata={
                    "guard": self.name,
                    "skipped": "no_auction_windows_configured",
                }
            )
        now = cfg.get("now") or datetime.now(IST)
        if is_within_auction_window(now=now, windows=windows):
            window = current_auction_window(now=now, windows=windows)
            return GuardDecision(
                metadata={
                    "guard": self.name,
                    "in_window": True,
                    "window": list(window) if window else None,
                }
            )
        return GuardDecision(
            pause_reason="OUTSIDE_AUCTION_WINDOW",
            metadata={
                "guard": self.name,
                "in_window": False,
                "windows": windows,
            },
        )


# ---------------------------------------------------------------------------
# Feature 6 - AuctionPriceGuard
# ---------------------------------------------------------------------------
class AuctionPriceGuard(BaseGuard):
    """Switches the LIMIT price anchor from LTP to the auction_price.

    On SME / PCAS listings the Last Traded Price during a call
    auction is the previous auction's clearing price, not the
    forthcoming one. The exchange publishes a real-time
    ``auction_price`` (a.k.a. equilibrium / indicative price) on
    the quote stream. The guard pulls that field out of the
    TopOfBook metadata and returns it as ``new_price`` so the
    slicer anchors the child to the right number.

    The guard is *only* active when:

      1. It is enabled (default ``True``).
      2. ``cfg["sme_periodic_call_auction"]`` is truthy (set by
         the runner from the symbol master).
      3. The TopOfBook has an ``auction_price`` attribute or the
         underlying quote response carried one.

    In every other case the guard returns ``new_price=None`` and
    the slicer falls back to the depth-aware pricing guard.
    """

    name = "auction_price"

    def evaluate(
        self,
        top: TopOfBook,
        *,
        action: str,
        cfg: Dict[str, Any],
    ) -> GuardDecision:
        if not self.is_enabled(cfg):
            return GuardDecision(
                metadata={"guard": self.name, "enabled": False}
            )
        if not cfg.get("sme_periodic_call_auction"):
            return GuardDecision(
                metadata={
                    "guard": self.name,
                    "skipped": "not_sme_pcas",
                }
            )
        auction_price = getattr(top, "auction_price", None)
        if auction_price is None or auction_price <= 0:
            return GuardDecision(
                metadata={
                    "guard": self.name,
                    "skipped": "no_auction_price",
                }
            )
        return GuardDecision(
            new_price=float(auction_price),
            metadata={
                "guard": self.name,
                "anchor": "auction_price",
                "auction_price": float(auction_price),
            },
        )




# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------
def default_guards() -> List[BaseGuard]:
    """Return the default guard set, in evaluation order.

    The ordering matters:

      1. ``CircuitGuard`` runs FIRST so we pause before any pricing
         work when LTP is near a circuit limit.
      2. ``AuctionWindowGuard`` runs next so an SME/PCAS run only
         places inside a configured call-auction window
         (Feature 5).
      3. ``AuctionPriceGuard`` runs BEFORE depth pricing because
         the auction price (when present) is a more reliable anchor
         than the LTP for SME/PCAS (Feature 6).
      4. ``DepthPricingGuard`` follows and provides the L2-aware
         LIMIT price for normal equity runs.
      5. ``DepthSliceCapGuard`` runs last because it needs the
         already-computed ``slice_qty`` to evaluate the cap
         (the slicer injects it via ``cfg["slice_qty"]``).
    """
    return [
        CircuitGuard(),
        AuctionWindowGuard(),
        AuctionPriceGuard(),
        DepthPricingGuard(),
        DepthSliceCapGuard(),
    ]


def evaluate_guards(
    top: TopOfBook,
    *,
    action: str,
    cfg: Dict[str, Any],
    guards: Optional[List[BaseGuard]] = None,
) -> GuardDecision:
    """Run each guard in turn and merge the partial decisions.

    The merged decision is rightmost-wins for ``new_price`` and
    ``capped_qty`` (guards later in the list override earlier ones). Any
    ``pause_reason`` from any guard short-circuits the merge and is
    returned immediately.
    """
    merged = GuardDecision()
    for guard in guards or default_guards():
        decision = guard.evaluate(top, action=action, cfg=cfg)
        # Pause is sticky - first guard that asks for a pause wins.
        if decision.pause_reason and not merged.pause_reason:
            merged.pause_reason = decision.pause_reason
            merged.metadata.update(decision.metadata)
            return merged
        if decision.new_price is not None:
            merged.new_price = decision.new_price
        if decision.capped_qty is not None:
            merged.capped_qty = decision.capped_qty
        merged.metadata.update(decision.metadata)
    return merged


__all__ = [
    "BaseGuard",
    "CircuitGuard",
    "AuctionWindowGuard",
    "AuctionPriceGuard",
    "DepthPricingGuard",
    "DepthSliceCapGuard",
    "GuardDecision",
    "TopOfBook",
    "default_guards",
    "evaluate_guards",
    "normalize_top_of_book",
]