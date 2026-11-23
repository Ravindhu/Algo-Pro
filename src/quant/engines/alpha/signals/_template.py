"""
_template.py — skeleton you can copy when adding a new signal strategy.

Usage:
    1. Copy this file to e.g. signals/engulfing.py
    2. Rename the class to EngulfingSignal
    3. Implement detect() with your pattern-detection logic
    4. Use the OpenAlgo SDK via self.client:
        - self.client.history(symbol=..., exchange=..., interval=..., start_date=..., end_date=...)
          returns a pandas DataFrame
        - self.client.quotes(symbol=..., exchange=...) for live LTP
        - See simple_ema_strategy.py and openalgo/data.py for examples
    5. Reference it from input/engulfing_pov.json:
        { "signal": "engulfing", "slicer": "pov", ... }
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .base import BaseSignal


class TemplateSignal(BaseSignal):
    """Replace this class with your own. The runner picks signals by class name."""

    def detect(self) -> Optional[Dict[str, Any]]:
        # Read pattern inputs via the OpenAlgo SDK
        # df = self.client.history(
        #     symbol=self.config["symbol"],
        #     exchange=self.config["exchange"],
        #     interval="5m",
        #     start_date="2026-01-01",
        #     end_date="2026-01-31",
        # )

        # Your pattern-detection logic here.
        # Return None to skip this cycle.
        # Return a dict like:
        #   {"action": "BUY", "quantity": 1000, "reason": "engulfing at support"}
        return None
