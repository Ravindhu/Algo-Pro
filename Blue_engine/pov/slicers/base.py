"""
base.py — abstract base class for all execution algorithms (slicers).

A slicer takes a trade intent from a signal and decides HOW to get it filled:
how many shares per child order, at what price, when to cancel, when to
exit. Different slicers (POV, TWAP, Iceberg) implement the same interface
so they can be swapped in JSON without code changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseSlicer(ABC):
    """Abstract base for execution algorithms.

    Subclasses must implement execute(intent). The intent dict is what
    the signal's detect() returned.
    """

    def __init__(self, client: Any, config: Dict[str, Any]):
        self.client = client
        self.config = config

    @abstractmethod
    def execute(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the trade intent and return a summary.

        Args:
            intent: dict from signal.detect(), with at least
                    {"action": "BUY"|"SELL", "quantity": int}

        Returns:
            dict with at least:
                filled:           total shares actually submitted
                target:           target shares from the intent
                child_orders:     number of placeorder() calls made
                mode:             "live" or "analyze" (from OpenAlgo)
        """
        raise NotImplementedError

    def log(self, message: str) -> None:
        print(f"[slicer:{self.__class__.__name__}] {message}")
