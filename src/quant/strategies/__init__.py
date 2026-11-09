"""Strategies: interface and reference implementations."""
from quant.strategies.base import Strategy, check_weights
from quant.strategies.momentum import MomentumStrategy

__all__ = ["Strategy", "check_weights", "MomentumStrategy"]
