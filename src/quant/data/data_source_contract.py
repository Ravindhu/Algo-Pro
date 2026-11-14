"""Data sources wrapper: provenance hashes for market data, DELEGATED to Blue_engine.

Flow stage: Data sources
Status: PHASE 5 IMPLEMENTED (wrap; hashing only; no source is fetched or built here)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/ml/data_sources/base.py
(rows_hash, schema_fingerprint)

Every run that uses data should cite the data it used. These hashes give that proof.
The network adapters (OpenAlgo, yfinance) are NOT built or called here.

FAIL-CLOSED RULES
-----------------
- Blue unavailable or a hash fails: returns None. A run with no provenance must not
  cite the data as verified.
- Never raises.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["data_fingerprint"]


def data_fingerprint(rows: Sequence[Any]) -> Optional[Dict[str, Any]]:
    """Return ``{rows_hash, schema}`` for ``rows``, or None if it cannot be computed."""
    try:
        base = load("ml.data_sources.base")
    except BlueEngineUnavailable:
        return None
    try:
        digest = base.rows_hash(list(rows))
        schema = base.schema_fingerprint(list(rows))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(digest, str) or not digest:
        return None
    return {"rows_hash": digest, "schema": schema}
