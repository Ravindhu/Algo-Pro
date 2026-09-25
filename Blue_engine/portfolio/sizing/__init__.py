"""
sizing - dynamic risk-budget sizing for the Portfolio engine (pure).

The optimisers decide *relative* weights. Sizing decides *how big* the whole book
should be — the gross multiplier applied on top. Three independent governors:

    vol_target          scale gross toward a target portfolio volatility
    kelly               fractional Kelly from the regime probability / edge
    drawdown_governor   de-risk when the equity curve is in drawdown

The engine combines them (min of the caps, by default) so the most conservative
governor always binds — the "degrade, never over-trade" principle applied to size.
"""
from . import drawdown_governor, kelly, vol_target

__all__ = ["vol_target", "kelly", "drawdown_governor"]