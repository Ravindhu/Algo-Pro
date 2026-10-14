"""database - the Data Management Engine (DME) package.

Public entry point: :class:`blue_engine.database.engine.DatabaseEngine`.

The DME aggregates structured market data and unstructured text, normalizes
timestamps to UTC ISO-8601, diagnoses lagging data, aligns matrices without
look-ahead, and publishes one atomic ``current_cycle_payload.json`` per cycle
for the frontline agents. Every public method is fail-soft: it reports problems
in its result dict rather than raising into a trading cycle.

Storage routing (no duplication of existing stores):

    data/price_cache/prices.db          OHLCV bars -> existing PriceCache
    data/current_cycle_payload.json     the Tier-4 cycle payload
"""
from __future__ import annotations

__all__ = ["engine"]

from . import engine  # noqa: F401
