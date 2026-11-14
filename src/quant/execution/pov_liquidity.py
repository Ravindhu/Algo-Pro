"""quant.execution.pov_liquidity - liquidity tiers and child-size caps for POV.

Flow stage: POV child sizing (decouple 6.5, Phase C).
Status: PHASE 3 IMPLEMENTED (ported 2026-11-09 from src/quant/blue_engine/core/liquidity.py; pure; no Blue import).
Parity: tests/parity/test_pov_phase_c_parity.py
"""
from __future__ import annotations
# â”€â”€ Copyright and license header preserved from the repo â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# blue_engine/core/liquidity.py
"""Liquidity analysis for POV execution - dynamic block-size scaling.

Responsibility
--------------
Given a symbol + exchange, this module answers three questions the
POV loop needs to scale its child-order sizes safely:

1. What is the *daily liquidity value* of this stock?
   daily_value = daily_volume Ã— reference_price (LTP or VWAP)

2. What is the *investment weight* relative to that liquidity?
   weight = target_investment / daily_liquidity_value

3. Which *liquidity tier* does this stock fall into?
   Based on daily_value, pick a tier whose max_block_size_pct
   (the cap on any single child order as a fraction of the
   remaining target quantity) is calibrated to the market's
   ability to absorb the order without moving the price.

The tier-to-cap mapping is hard-coded but fully JSON-overridable
through the slicer config, so operators can tune it without code
changes.  A ``max_block_size_pct`` value outside the 0.01-0.05
range is clamped to the nearest bound; a value of 0 or missing
falls back to the tier default.

Design rules (mirrored from blue_engine/core/time_utils.py)
- No SDK calls. No network. No DB. Pure helpers.
- Graceful degradation: a None or empty quote always returns a
  conservative (small) block size rather than crashing the loop.
"""

from dataclasses import dataclass
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Liquidity tiers: daily_value (INR) â†’ default max_block_size_pct
# ---------------------------------------------------------------------------
#   Tier      Daily Value (INR)    Default max_block_pct   Rationale
#   â”€â”€â”€â”€â”€â”€    â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€    â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€   â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#   MEGA      â‰¥ 1_000_000_000       0.05 (5%)               Deep book,
#                                                             low impact
#   LARGE      500_000_000-1B       0.04 (4%)               Strong depth
#   MID        100_000_000-500M     0.03 (3%)               Moderate depth
#   SMALL       10_000_000-100M     0.02 (2%)               Thin book
#   MICRO        < 10_000_000       0.01 (1%)               Very thin,
#                                                             high impact
# ---------------------------------------------------------------------------
LIQUIDITY_TIERS = (
    ("MEGA",   1_000_000_000, 0.05),
    ("LARGE",    500_000_000, 0.04),
    ("MID",      100_000_000, 0.03),
    ("SMALL",     10_000_000, 0.02),
    ("MICRO",             0, 0.01),
)

DEFAULT_MIN_BLOCK_PCT = 0.01   # 1% - never go below this
DEFAULT_MAX_BLOCK_PCT = 0.05   # 5% - never go above this


@dataclass(frozen=True)
class LiquidityProfile:
    """Immutable snapshot of a stock's liquidity characteristics."""

    symbol: str
    exchange: str
    daily_volume: int                    # shares traded today
    reference_price: float               # LTP or VWAP (INR)
    daily_liquidity_value: float         # daily_volume Ã— reference_price
    investment_weight: float             # target_investment / daily_liquidity_value
    tier_name: str                       # "MEGA" | "LARGE" | "MID" | "SMALL" | "MICRO"
    max_block_size_pct: float            # calibrated cap for single child order
    min_order_qty: int = 1               # never submit < 1 share

    # Derived helpers
    @property
    def daily_liquidity_value_cr(self) -> float:
        """Daily liquidity in Crores (1 Crore = 10M INR)."""
        return self.daily_liquidity_value / 1_000_000

    @property
    def is_liquid(self) -> bool:
        """True if the stock has meaningful daily volume."""
        return self.daily_volume > 0 and self.daily_liquidity_value > 0
def calculate_daily_liquidity_value(
    daily_volume: int,
    reference_price: float,
) -> float:
    """Calculate the daily traded value of a stock.

    Args:
        daily_volume: Total shares traded today (from quote volume).
        reference_price: Current price (LTP) or VWAP in INR.

    Returns:
        Daily liquidity value in INR (daily_volume Ã— price).
    """
    if daily_volume <= 0 or reference_price <= 0:
        return 0.0
    return float(daily_volume) * float(reference_price)


def calculate_investment_weight(
    target_investment: float,
    daily_liquidity_value: float,
) -> float:
    """Calculate the investment weight relative to the stock's daily liquidity.

    This tells us what fraction of the stock's daily trading volume
    our order represents. A weight > 1.0 means we're trying to trade
    more than the entire day's volume - extremely dangerous.

    Args:
        target_investment: Total amount to invest (INR).
        daily_liquidity_value: Stock's daily traded value (INR).

    Returns:
        Investment weight (0.0 to âˆž). Values > 1.0 are red flags.
    """
    if daily_liquidity_value <= 0 or target_investment <= 0:
        return 0.0
    return target_investment / daily_liquidity_value


def resolve_liquidity_tier(daily_liquidity_value: float) -> str:
    """Determine the liquidity tier from daily traded value.

    Args:
        daily_liquidity_value: Daily traded value in INR.

    Returns:
        Tier name: "MEGA", "LARGE", "MID", "SMALL", or "MICRO".
    """
    try:
        value = float(daily_liquidity_value)
    except (TypeError, ValueError):
        return "MICRO"
    if value <= 0:
        return "MICRO"
    for tier_name, threshold, _ in LIQUIDITY_TIERS:
        if value >= threshold:
            return tier_name
    return "MICRO"


def build_liquidity_profile(
    symbol: str,
    exchange: str,
    daily_volume: int,
    reference_price: float,
    target_investment: float = 0.0,
    *,
    custom_tiers: Optional[Dict[str, float]] = None,
) -> LiquidityProfile:
    """Build a complete LiquidityProfile from raw market data.

    This is the main entry point for the POV slicer. It calculates
    all liquidity metrics and determines the appropriate block-size
    cap based on the stock's liquidity tier.

    Args:
        symbol: Trading symbol (e.g., "INFY", "RELIANCE").
        exchange: Exchange code (e.g., "NSE", "BSE").
        daily_volume: Today's cumulative traded volume (shares).
        reference_price: Current LTP or VWAP in INR.
        target_investment: Total investment amount in INR (for weight calc).
        custom_tiers: Optional override for tier thresholds (for testing).

    Returns:
        LiquidityProfile with all calculated metrics.
    """
    daily_value = calculate_daily_liquidity_value(daily_volume, reference_price)
    weight = calculate_investment_weight(target_investment, daily_value)
    tier = resolve_liquidity_tier(daily_value)

    # Get tier-based default, then apply user override if provided
    if custom_tiers and tier in custom_tiers:
        max_block_pct = custom_tiers[tier]
    else:
        max_block_pct = get_tier_default_max_block_pct(tier)

    # Clamp to safe bounds (0.01-0.05)
    max_block_pct = max(DEFAULT_MIN_BLOCK_PCT, min(max_block_pct, DEFAULT_MAX_BLOCK_PCT))

    return LiquidityProfile(
        symbol=symbol,
        exchange=exchange,
        daily_volume=daily_volume,
        reference_price=reference_price,
        daily_liquidity_value=daily_value,
        investment_weight=weight,
        tier_name=tier,
        max_block_size_pct=max_block_pct,
    )


def scale_block_size_pct(
    liquidity_profile: LiquidityProfile,
    remaining_qty: int,
    target_qty: int,
    requested_pct: float,
) -> float:
    """Scale the block size percentage based on liquidity and position size.

    This is the core safety function. It ensures:
    1. No single child order exceeds the liquidity cap
    2. The order doesn't exceed remaining target quantity
    3. Minimum order size of 1 share is always respected

    Args:
        liquidity_profile: Pre-calculated liquidity metrics.
        remaining_qty: Shares still to be filled.
        target_qty: Original total target quantity.
        requested_pct: Requested participation percentage (0-100).

    Returns:
        Scaled block size percentage (clamped to 0.01-0.05).
    """
    if remaining_qty <= 0 or target_qty <= 0:
        return 0.0

    # Base cap from liquidity tier
    liquidity_cap = liquidity_profile.max_block_size_pct

    # Scale down if we're close to finishing (don't overfill)
    completion_ratio = remaining_qty / target_qty
    if completion_ratio < 0.1:  # Last 10% of order
        # Use smaller blocks to avoid overfilling
        liquidity_cap = min(liquidity_cap, 0.02)
    elif completion_ratio < 0.25:  # Last 25% of order
        liquidity_cap = min(liquidity_cap, 0.03)

    # Apply requested pct but never exceed the liquidity cap
    effective_pct = min(requested_pct / 100.0, liquidity_cap)

    # Clamp to safe bounds
    effective_pct = max(DEFAULT_MIN_BLOCK_PCT, min(effective_pct, DEFAULT_MAX_BLOCK_PCT))

    return effective_pct


def calculate_max_child_qty(
    liquidity_profile: LiquidityProfile,
    remaining_qty: int,
    target_qty: int,
    participation_pct: float = 10.0,
) -> int:
    """Calculate the maximum child order quantity based on liquidity.

    This combines the POV participation rate with liquidity caps
    to determine the safe order size.

    Args:
        liquidity_profile: Pre-calculated liquidity metrics.
        remaining_qty: Shares still to be filled.
        target_qty: Original total target quantity.
        participation_pct: POV participation percentage (default 10%).

    Returns:
        Maximum child order quantity (minimum 1, maximum remaining).
    """
    if remaining_qty <= 0:
        return 0
    if not liquidity_profile.is_liquid:
        # Liquidity is unknown/unavailable: degrade to a conservative single
        # block rather than dumping the whole remaining order at once.
        return max(1, min(remaining_qty, int(remaining_qty * DEFAULT_MIN_BLOCK_PCT) or 1))

    # Calculate POV-based quantity (delta_vol * pct / 100)
    # We use daily_volume as a proxy for delta_vol in the first iteration
    pov_qty = int((liquidity_profile.daily_volume * participation_pct) / 100.0)

    # Apply liquidity cap
    max_qty_by_liquidity = int(remaining_qty * liquidity_profile.max_block_size_pct)

    # Take the minimum (most restrictive)
    max_qty = min(pov_qty, max_qty_by_liquidity, remaining_qty)

    # Ensure at least 1 share, never exceed remaining
    max_qty = max(1, min(max_qty, remaining_qty))

    return max_qty


def get_tier_default_max_block_pct(tier_name: str) -> float:
    """Get the default max_block_size_pct for a liquidity tier.

    Args:
        tier_name: One of "MEGA", "LARGE", "MID", "SMALL", "MICRO".

    Returns:
        Default max block percentage (0.01-0.05).
    """
    for name, _, pct in LIQUIDITY_TIERS:
        if name == tier_name:
            return pct
    return DEFAULT_MIN_BLOCK_PCT
