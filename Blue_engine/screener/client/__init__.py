"""screener.client - the Python port of the vendored screener-india client.

Nothing in this package talks to a broker, OpenAlgo, or the order path. It is a
read-only HTTP client for public screener.in pages plus the parsers that turn
their HTML into plain Python dicts.
"""
from __future__ import annotations

from .base import ApiResponse, Meta, Throttle, TtlCache
from .screener_client import COMPANY_TABS, ScreenerClient

__all__ = [
    "ScreenerClient",
    "COMPANY_TABS",
    "ApiResponse",
    "Meta",
    "Throttle",
    "TtlCache",
]
