"""
base.py — abstract base class for all signal strategies.

A signal decides WHEN to enter a position and HOW MUCH. It does NOT decide
how the order gets sliced over time — that is the slicer's job.

This separation lets you mix and match:
    engulfing_signal  + pov_slicer
    breakout_signal   + pov_slicer
    manual_signal     + pov_slicer    (just "trade now" for testing)
    any_signal        + twap_slicer   (future)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class BaseSignal(ABC):
    """Abstract base for a signal strategy.

    Subclasses must implement detect(). They may also override log_state() to
    print human-readable status. The runner calls detect() repeatedly; when
    it returns a non-None intent, the runner hands the intent to a slicer.
    """

    def __init__(self, client: Any, config: Dict[str, Any]):
        """Store the SDK client and the parsed JSON config.

        Args:
            client: An initialized openalgo.api instance.
            config: Parsed JSON dictionary from pov_engine/input/*.json.
        """
        self.client = client
        self.config = config

    @abstractmethod
    def detect(self) -> Optional[Dict[str, Any]]:
        """Return a trade intent, or None to skip this cycle.

        Returns:
            A dict with at least:
                action:   "BUY" or "SELL"
                quantity: positive integer (shares/contracts/lots)
            Additional keys (e.g., "tag", "reason") are passed through to
            the slicer as metadata.
            Or None when no signal is present.
        """
        raise NotImplementedError

    def describe(self) -> str:
        """One-line human description, used in logs."""
        return f"{self.__class__.__name__} for {self.config.get('symbol', '?')}/{self.config.get('exchange', '?')}"

    def log(self, message: str) -> None:
        """Print a prefixed log line. Slicers and runner use the same prefix."""
        print(f"[signal:{self.__class__.__name__}] {message}")
