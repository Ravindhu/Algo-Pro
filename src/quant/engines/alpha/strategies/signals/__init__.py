"""Flow stage: Strategy (signal package).
Status: PHASE 1 PACKAGE MARKER. Exports the signal base and check contract.
"""
from .base import SignalStrategy, check_signal

__all__ = ["SignalStrategy", "check_signal"]
