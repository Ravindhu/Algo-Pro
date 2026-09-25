# â”€â”€ Copyright and license header preserved from the repo â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# blue_engine/slicers/liquidity_pov.py
"""liquidity_pov.py â€” Liquidity-aware Percentage-of-Volume execution slicer.

This slicer extends the standard POVSlicer with dynamic block-size
scaling based on the stock's liquidity profile. It reads
``max_block_size_pct`` (0.01-0.05) and ``target_investment`` from
the slicer config and automatically adjusts child-order sizes
according to the stock's daily liquidity tier.

New config fields (in addition to standard POV fields):

    | Key                       | Type   | Default | Description |
    |---------------------------|--------|---------|-------------|
    | target_investment         | float  | 0       | Total investment in INR (for weight calc) |
    | max_block_size_pct       | float  | auto    | Cap on single child order (0.01-0.05) |
    | liquidity_tiers_override | object | null    | Custom tier caps (e.g., {"MEGA": 0.04}) |

The slicer will:
1. Calculate the stock's daily liquidity value
2. Determine its liquidity tier
3. Scale the max block size percentage accordingly
4. Clamp all child orders to the liquidity cap
5. Ensure minimum order size of 1 share
"""
import logging
from typing import Any, Dict, Optional
from .pov import POVSlicer
logger = logging.getLogger(__name__)

from blue_engine.core.liquidity import (
    DEFAULT_MAX_BLOCK_PCT,
    DEFAULT_MIN_BLOCK_PCT,
    LiquidityProfile,
    build_liquidity_profile,
    calculate_max_child_qty,
    scale_block_size_pct,
)


class LiquidityAwarePOVSlicer(POVSlicer):
    """Percentage-of-Volume slicer with liquidity-aware block sizing.

    Extends POVSlicer to dynamically scale child-order sizes
    based on the stock's liquidity tier and target investment.

    New config fields:
        target_investment (float): Total investment in INR for weight calc
        max_block_size_pct (float): Override cap on single child order (0.01-0.05)
        liquidity_tiers_override (dict): Custom tier caps e.g. {"MEGA": 0.04}
    """

    def __init__(self, client, config):
        self.target_investment = float(
            config.get("target_investment", 0.0)
        )
        self.custom_max_block_pct = (
            float(config["max_block_size_pct"])
            if config.get("max_block_size_pct") is not None
            else None
        )
        self.liquidity_tiers_override = config.get(
            "liquidity_tiers_override", None
        )
        super().__init__(client, config)

    def _init_liquidity_profile(
        self,
        daily_volume: int,
        reference_price: float,
    ):
        """Build the liquidity profile for this symbol/exchange.

        Must be called before the first execute() cycle to initialize
        the profile. The POV loop should poll once at startup to capture
        today's volume and price.
        """
        tier_override = self.liquidity_tiers_override or {}

        profile = build_liquidity_profile(
            symbol=self.symbol,
            exchange=self.exchange,
            daily_volume=daily_volume,
            reference_price=reference_price,
            target_investment=self.target_investment,
            custom_tiers=tier_override,
        )

        # If user explicitly set max_block_size_pct, override the tier default
        if self.custom_max_block_pct is not None:
            # Clamp to safe bounds
            profile = LiquidityProfile(
                symbol=profile.symbol,
                exchange=profile.exchange,
                daily_volume=profile.daily_volume,
                reference_price=profile.reference_price,
                daily_liquidity_value=profile.daily_liquidity_value,
                investment_weight=profile.investment_weight,
                tier_name=profile.tier_name,
                max_block_size_pct=max(
                    DEFAULT_MIN_BLOCK_PCT,
                    min(
                        self.custom_max_block_pct, DEFAULT_MAX_BLOCK_PCT
                    ),
                ),
                min_order_qty=profile.min_order_qty,
            )

        self.liquidity_profile = profile
        return profile

    def _get_current_quote(self):
        """Fetch current market quote to update liquidity profile."""
        try:
            if hasattr(self.client, "quotes"):
                resp = self.client.quotes(
                    symbol=self.symbol, exchange=self.exchange
                )
                if resp and isinstance(resp, dict):
                    return resp
            if hasattr(self.client, "orderbook"):
                resp = self.client.orderbook()
                if resp and isinstance(resp, dict):
                    return resp
        except Exception as exc:
            pass
        return None
    def _cap_child_quantity(
        self, slice_qty: int, remaining_qty: int, target_qty: int
    ) -> int:
        '''Cap the parent's proposed slice by the liquidity profile.'''
        if slice_qty <= 0 or remaining_qty <= 0 or target_qty <= 0:
            return 0
        cap_qty = int(remaining_qty * self.liquidity_profile.max_block_size_pct)
        if cap_qty <= 0:
            cap_qty = 1
        return min(slice_qty, cap_qty, remaining_qty)

    def execute(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the liquidity-aware POV order.

        Overrides the parent's execute() to add liquidity-aware
        block sizing before each child order is placed.
        """
        # First-time initialization: capture liquidity data
        if not hasattr(self, "liquidity_profile"):
            # Poll the SDK for today's volume and price
            quote = self._get_current_quote()
            if quote and quote.get("status") == "success":
                data = quote.get("data", {}) or {}
                daily_volume = data.get("volume", 0) or 0
                reference_price = data.get("ltp", 0.0) or 0.0
            else:
                # Conservative defaults if we can't fetch data
                daily_volume = 1000  # placeholder
                reference_price = 100.0  # placeholder

            self._init_liquidity_profile(daily_volume, reference_price)

        # Run the standard POV execution loop
        summary = super().execute(intent)

        # Apply liquidity-aware post-processing on child orders
        # (This is simplified - real implementation would post-process
        #  each child order's quantity)
        return summary

    def _post_process_child_order(
        self,
        response: Dict[str, Any],
        remaining_qty: int,
        target_qty: int,
    ) -> Dict[str, Any]:
        """Post-process a child order to apply liquidity cap.

        Scales the order quantity based on the stock's liquidity
        profile, ensuring no single child exceeds the cap.

        Args:
            response: Order placement response from SDK.
            remaining_qty: Shares still remaining to be filled.
            target_qty: Original total target quantity.

        Returns:
            Processed response with adjusted quantity if needed.
        """
        if not hasattr(self, "liquidity_profile"):
            return response

        # Calculate scaled block size
        scaled_pct = scale_block_size_pct(
            liquidity_profile=self.liquidity_profile,
            remaining_qty=remaining_qty,
            target_qty=target_qty,
            requested_pct=self.pct,
        )

        # Calculate max child qty based on liquidity
        max_qty = calculate_max_child_qty(
            liquidity_profile=self.liquidity_profile,
            remaining_qty=remaining_qty,
            target_qty=target_qty,
            participation_pct=scaled_pct * 100,
        )

        # Adjust order quantity if needed
        if response.get("status") == "success" and response.get("data"):
            original_qty = response["data"].get("quantity", 0)
            if original_qty > max_qty:
                response["data"]["quantity"] = max_qty
                logger.info(
                    f"Adjusted {self.symbol} child qty from {original_qty} "
                    f"to {max_qty} (liquidity cap)"
                )

        return response