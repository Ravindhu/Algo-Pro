"""
Flow stage: Signal and conviction (VWAP band indicator, shared by strategies).
Status: PORTED COPY of Blue_engine/core/band.py. Tests: src/quant/tests/test_signals_vwap_band.py.

band.py - VWAP Value Filter Band (Phase 1, Feature 13).

================================================================================
PURPOSE
================================================================================
Pure (no SDK, no DB, no network) module that computes the dynamic POV rate
to use on the next poll, given:

  * the current buyer-initiated VWAP (from the slicer's tick stream),
  * the current LTP,
  * the slicer configuration.

The band VWAP is the *buyer-initiated* VWAP: it aggregates only those ticks
where the trade was buyer-initiated (LTP >= ask). That makes the band track
where buyers are actually transacting rather than the all-trade session VWAP.
The caller (the POV slicer) is responsible for that classification and passes
the resulting value in as ``vwap``.

================================================================================
DECISION TREE (mirrors the heatmap in the design doc)
================================================================================

  LTP
   ^
   |
   |  +======================================================+  <- ceiling
   |  ||  HALT zone  (LTP >= ceiling for BUY)              ||
   |  +======================================================+
   |  ||                                                   ||
   |  ||   INTERPOLATION BAND                              ||
   |  ||   rate = lerp(min_pov, max_pov, f_band)           ||
   |  ||                                                   ||
   |  +======================================================+
   |  ||  PASSTHROUGH  (LTP <= floor -> part_pct)         ||
   |  +======================================================+
   |
   +------------------------------------------------------>
                              floor

Three regimes:
  1. HALT           (LTP >= ceiling for BUY)        -> 0%   (slice_qty = 0)
  2. INTERPOLATE    (floor < LTP < ceiling)         -> lerp(min_pov, max_pov, f_band)
  3. PASSTHROUGH    (LTP <= floor for BUY)          -> part_pct (or config alt)

For SELL the sides flip: HALT is at floor, passthrough is at ceiling.
The *internal* band (floor, ceiling) is symmetric for both sides.

The band is anchored on the buyer-initiated VWAP with asymmetric default
half-widths: ceiling = vwap * 1.005 (0.5% above) and floor = vwap * 0.97
(3% below).

================================================================================
SMOOTHNESS GUARANTEE
================================================================================
Every output is a continuous, piecewise-linear function of (vwap, ltp).
The only non-smooth point is the HALT boundary, which is intentional.

================================================================================
USAGE
================================================================================
    from quant.indicators.vwap_band import BandConfig, compute_target_pov_rate

    cfg = BandConfig(
        vwap=1000.0,
        vwap_ceiling_pct=0.5,
        vwap_floor_pct=3.0,
        min_pov_pct=5.0,
        max_pov_pct=25.0,
        part_pct=10.0,
        passthrough_floor="part_pct",
        action="BUY",
    )
    decision = compute_target_pov_rate(ltp=987.50, config=cfg)
    # decision.rate   -> 15.0
    # decision.reason -> "interpolated"
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


# -----------------------------------------------------------------------------
# Public dataclasses
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class BandConfig:
    """All knobs needed to evaluate the band.

    Attributes
    ----------
    vwap : float
        Current *buyer-initiated* VWAP (must be > 0; the caller is
        responsible for computing it from the quote stream by aggregating
        only ticks where LTP >= ask).
    vwap_ceiling_pct : float
        Percentage ABOVE VWAP that defines the HALT band. Default 0.5%.
        ceiling = vwap * (1 + vwap_ceiling_pct / 100).
    vwap_floor_pct : float
        Percentage BELOW VWAP that defines the band/passthrough boundary.
        Default 3.0%. floor = vwap * (1 - vwap_floor_pct / 100).
    min_pov_pct : float
        Lower clamp for the interpolated rate.
    max_pov_pct : float
        Upper clamp for the interpolated rate.
    part_pct : float
        The slicer's configured rate; used in the passthrough zone.
    passthrough_floor : str
        What to do when LTP <= floor (BUY). One of:
            "part_pct"  -> use part_pct (default; legacy behaviour)
            "max_pov"   -> use max_pov (keep accumulating aggressively)
            "min_pov"   -> use min_pov (most conservative passthrough)
    action : str
        "BUY" or "SELL". For SELL the ceiling/floor swap roles so the slicer
        refuses to sell below floor (HALT) and sells aggressively at/above
        ceiling.
    """

    vwap: float
    vwap_ceiling_pct: float = 0.5
    vwap_floor_pct: float = 3.0
    min_pov_pct: float = 5.0
    max_pov_pct: float = 25.0
    part_pct: float = 10.0
    passthrough_floor: str = "part_pct"
    action: str = "BUY"


@dataclass(frozen=True)
class BandDecision:
    """The output of one band evaluation.

    Attributes
    ----------
    rate : float
        The POV rate to use on the next poll (in percent, e.g. 15.0 for 15%).
    reason : str
        Short tag describing which regime produced the rate.
    f_band : float
        Band-position fraction in [0, 1]. Useful for logging.
    ceiling : float
        The ceiling value that was used.
    floor : float
        The floor value that was used.
    """

    rate: float
    reason: str
    f_band: float
    ceiling: float
    floor: float


# -----------------------------------------------------------------------------
# Small numeric helpers
# -----------------------------------------------------------------------------
def _clamp01(value: float) -> float:
    """Clamp a value into [0.0, 1.0]."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into ``[lo, hi]``."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation between ``a`` and ``b`` by ``t`` in [0, 1]."""
    return a + (b - a) * t


# -----------------------------------------------------------------------------
# Anchor computation
# -----------------------------------------------------------------------------
def compute_ceiling(vwap: float, ceiling_pct: float) -> float:
    """Return ``vwap * (1 + ceiling_pct / 100)``."""
    return vwap * (1.0 + ceiling_pct / 100.0)


def compute_floor(vwap: float, floor_pct: float) -> float:
    """Return ``vwap * (1 - floor_pct / 100)``."""
    return vwap * (1.0 - floor_pct / 100.0)


# -----------------------------------------------------------------------------
# Phase 1 fractions (the "two signals" that drive the heatmap)
# -----------------------------------------------------------------------------
def compute_band_fraction(
    ltp: float, ceiling: float, floor: float
) -> float:
    """Position of LTP inside the band, in [0, 1].

    0.0 means LTP is at the ceiling (most conservative for BUY).
    1.0 means LTP is at the floor (most aggressive for BUY).
    """
    if ceiling <= floor:
        # Degenerate band; treat as fully conservative.
        return 0.0
    return _clamp01((ceiling - ltp) / (ceiling - floor))

# -----------------------------------------------------------------------------
# Passthrough rate
    # -----------------------------------------------------------------------------
def passthrough_rate(config: BandConfig) -> float:
    """Return the rate to use when LTP <= floor (BUY) / LTP >= ceiling (SELL)."""
    mode = str(config.passthrough_floor or "part_pct").lower()
    if mode == "max_pov":
        return float(config.max_pov_pct)
    if mode == "min_pov":
        return float(config.min_pov_pct)
    # default
    return float(config.part_pct)


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------
def compute_target_pov_rate(
    *, ltp: float, config: BandConfig
) -> BandDecision:
    """Compute the next-poll POV rate for Phase 1 (VWAP Value Filter).

    Parameters
    ----------
    ltp : float
        Current last-traded price. NaN / <= 0 is treated as "unavailable".
    config : BandConfig
        All knobs and anchors.

    Returns
    -------
    BandDecision
        A frozen dataclass with the rate, reason, fractions, and anchors
        used. ``decision.rate`` is always a finite float in
        ``[0, max_pov_pct]``.
    """
    ceiling = compute_ceiling(config.vwap, config.vwap_ceiling_pct) \
        if config.vwap and config.vwap > 0 else 0.0
    floor = compute_floor(config.vwap, config.vwap_floor_pct) \
        if config.vwap and config.vwap > 0 else 0.0
    min_pov = float(config.min_pov_pct)
    max_pov = float(config.max_pov_pct)

    # ---- Degenerate input handling --------------------------------------- #
    if ltp is None or (isinstance(ltp, float) and (ltp != ltp or ltp <= 0)):
        return BandDecision(
            rate=_clamp(float(config.part_pct), 0.0, max_pov),
            reason="ltp_unavailable",
            f_band=0.0,
            ceiling=ceiling,
            floor=floor,
        )
    if config.vwap is None or config.vwap <= 0:
        # No VWAP yet -- degrade to part_pct.
        return BandDecision(
            rate=float(config.part_pct),
            reason="vwap_unavailable",
            f_band=0.0,
            ceiling=0.0,
            floor=0.0,
        )

    action = str(config.action or "BUY").upper()
    is_sell = action == "SELL"

    # ---- HALT zone (side-aware) ----------------------------------------- #
    # BUY  : ltp >= ceiling -> HALT (overpriced; do not chase)
    # SELL : ltp <= floor   -> HALT (underselling; do not dump)
    if (not is_sell and ltp >= ceiling) or (is_sell and ltp <= floor):
        return BandDecision(
            rate=0.0,
            reason="halt",
            f_band=0.0,
            ceiling=ceiling,
            floor=floor,
        )

    # ---- Passthrough zone (side-aware) ---------------------------------- #
    # BUY  : ltp <= floor -> passthrough (use part_pct or alternate)
    # SELL : ltp >= ceiling -> passthrough (sell into strength)
    if (not is_sell and ltp <= floor) or (is_sell and ltp >= ceiling):
        return BandDecision(
            rate=_clamp(passthrough_rate(config), 0.0, max_pov),
            reason="passthrough",
            f_band=1.0 if not is_sell else 0.0,
            ceiling=ceiling,
            floor=floor,
        )

    # ---- Interpolation zone --------------------------------------------- #
    f_band = compute_band_fraction(ltp, ceiling, floor)
    rate = _lerp(min_pov, max_pov, f_band)

    return BandDecision(
        rate=_clamp(rate, 0.0, max_pov),
        reason="interpolated",
        f_band=f_band,
        ceiling=ceiling,
        floor=floor,
    )


__all__ = [
    "BandConfig",
    "BandDecision",
    "compute_ceiling",
    "compute_floor",
    "compute_band_fraction",
    "passthrough_rate",
    "compute_target_pov_rate",
]

# -----------------------------------------------------------------------------
# Buyer-initiated daily VWAP (confirmation layer)
# -----------------------------------------------------------------------------
class BuyerInitiatedDailyVWAP:
    """VWAP over DAILY candles counting only buyer-initiated volume.

    Classification (identical to the POV slicer)
    --------------------------------------------
    A daily candle counts toward this VWAP only when it is buyer-initiated,
    using the SAME rule the POV slicer applies to its tick stream:

        ltp >= ask   ->  buyer-initiated

    On daily bars there is no live ``ask``, so the caller supplies the
    classification inputs. Two supported modes:

    Mode 1 - real quote data available (intraday polling of the daily
             candle): pass ``ltp`` and ``ask`` and the exact slicer rule is
             applied unchanged.

    Mode 2 - OHLCV only (backtests, daily history): pass
             ``buyer_initiated=True/False`` computed by the caller from
             whatever proxy it has chosen. This class does NOT invent a
             proxy, because any OHLCV-only substitute (e.g. close position
             within the day's range) is an approximation, not the real
             tape rule, and must be labelled as such by the caller rather
             than hidden inside this accumulator.

    The window starts at the keylevel touch and accumulates each subsequent
    daily candle until the setup resolves. If the armed setup spans N daily
    candles, the VWAP covers those N candles - not the whole series.
    """

    __slots__ = ("level", "action", "start_bar", "_pv", "_vol", "_bars")

    def __init__(self) -> None:
        self.level: Optional[float] = None
        self.action: Optional[str] = None
        self.start_bar: Optional[int] = None
        self._pv: float = 0.0
        self._vol: float = 0.0
        self._bars: int = 0

    # -- lifecycle ---------------------------------------------------------
    @property
    def is_active(self) -> bool:
        """True once the keylevel touch has opened the window."""
        return self.start_bar is not None

    @property
    def count(self) -> int:
        """Daily candles that qualified as buyer-initiated."""
        return self._bars

    @property
    def total_volume(self) -> float:
        """Buyer-initiated volume accumulated (not total market volume)."""
        return self._vol

    def reset(
        self,
        *,
        level: Optional[float] = None,
        action: Optional[str] = None,
        start_bar: Optional[int] = None,
    ) -> None:
        """Open a fresh window at the keylevel touch, discarding prior flow."""
        self.level = None if level is None else float(level)
        self.action = None if action is None else str(action).upper()
        self.start_bar = None if start_bar is None else int(start_bar)
        self._pv = 0.0
        self._vol = 0.0
        self._bars = 0

    # -- accumulation ------------------------------------------------------
    def add_daily_candle(
        self,
        *,
        price: float,
        volume: float,
        ltp: Optional[float] = None,
        ask: Optional[float] = None,
        buyer_initiated: Optional[bool] = None,
    ) -> bool:
        """Add one DAILY candle. Returns True if it counted.

        Precedence: if ``ltp`` and ``ask`` are both given, the real slicer
        rule (``ltp >= ask``) decides. Otherwise ``buyer_initiated`` decides.
        If neither is supplied the candle is NOT counted - failing closed is
        the safe behaviour here, because silently counting every candle
        would turn this into a plain VWAP while still being treated as a
        buyer-initiated one.
        """
        try:
            p = float(price)
            v = float(volume)
        except (TypeError, ValueError):
            return False
        if p <= 0 or v <= 0:
            return False

        if ltp is not None and ask is not None:
            try:
                counted = float(ltp) >= float(ask)
            except (TypeError, ValueError):
                return False
        elif buyer_initiated is not None:
            counted = bool(buyer_initiated)
        else:
            return False          # fail closed: unclassifiable candle

        if not counted:
            return False
        self._pv += p * v
        self._vol += v
        self._bars += 1
        return True

    # -- readout -----------------------------------------------------------
    @property
    def vwap(self) -> Optional[float]:
        """Buyer-initiated daily VWAP, or None when nothing qualified yet.

        ``None`` means "not measurable" - never a price. A run where every
        day was seller-initiated legitimately has no buyer VWAP, and the
        band must be treated as absent rather than defaulted to zero.
        """
        if self._vol <= 0:
            return None
        return self._pv / self._vol

    def bars_since_touch(self, current_bar: int) -> Optional[int]:
        """Daily candles elapsed since the touch, or None if not started."""
        if self.start_bar is None:
            return None
        return int(current_bar) - int(self.start_bar)

    def deviation_pct(self, price: float) -> Optional[float]:
        """Distance of ``price`` from the VWAP in percent (None if absent)."""
        v = self.vwap
        if v is None or v <= 0:
            return None
        try:
            p = float(price)
        except (TypeError, ValueError):
            return None
        return (p - v) / v * 100.0

    def snapshot(self) -> Dict[str, Any]:
        """Serialisable view for logs, dashboards and intent metadata."""
        return {
            "level": self.level,
            "action": self.action,
            "start_bar": self.start_bar,
            "daily_candles_counted": self._bars,
            "buyer_volume": self._vol,
            "buyer_vwap": self.vwap,
            "classification": "daily buyer-initiated (ltp >= ask)",
        }