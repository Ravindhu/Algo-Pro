"""
sector_membership.py - FINAL gate: keep only stocks that belong to the
HM-selected sector indices.

WHY A LOOKUP, AND NOT A PER-STOCK FETCH
---------------------------------------
The screener has two ways to produce candidates:

  * ``source="sector"`` - query specific sectors, so everything returned
    already belongs to them.
  * ``source="screen"`` / ``source="symbols"`` - an arbitrary list. Nothing
    ties those rows to a sector, and a company page does NOT expose one.

So for the second case there is no field to check. The only honest answer is a
**membership lookup**: build {symbol -> index} from the selected sectors, then
drop any candidate not in it.

That is what this module does, and it is fail-CLOSED: a symbol whose sector
cannot be established is EXCLUDED and reported. The alternative - keeping
unknowns - would quietly re-open the universe the gate just closed, which is
exactly the bug this module exists to prevent.

THE TWO FLAGS THIS FIXES
------------------------
1. ``_apply_sector_filter`` only populated ``sectors`` for ``source="sector"``.
   ``source="screen"`` and ``source="symbols"`` never consulted it, so the gate
   was computed and then ignored for those paths.
2. Nothing verified that a returned stock actually belonged to a selected
   sector, even on the sector path.

ONE SECONDARY-REQUEST COST, PAID ONCE
-------------------------------------
Building the lookup reads the member list of EACH selected sector (up to
``max_symbols`` rows each) and maps every symbol to the index that selected it.
That is bounded by the number of selected sectors, not by the candidate count,
and results are cached for the life of the engine instance.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = ["SectorMembership", "build_membership", "apply_membership"]

#: Sectors whose member list could not be read. Reported, never silently
#: treated as "no restriction".
_DEFAULT_PER_SECTOR_LIMIT = 250


class SectorMembership:
    """{symbol -> sector index} for the sectors that passed the HM gate."""

    def __init__(self, index_to_slugs: Dict[str, List[str]]):
        self.index_to_slugs = dict(index_to_slugs or {})
        self.symbol_to_index: Dict[str, str] = {}
        self.failed: Dict[str, str] = {}
        self.built = False

    # ---- build ------------------------------------------------------------ #
    def build(self, client: Any, *, limit: int = 50,
              include_all_pages: bool = False,
              max_pages: int = 40) -> "SectorMembership":
        """Populate the lookup. Failures are recorded, never raised."""
        self.symbol_to_index = {}
        self.failed = {}
        for index, slugs in self.index_to_slugs.items():
            found_any = False
            for slug in slugs:
                try:
                    res = client.get_sector_data(
                        slug, limit=limit, include_all_pages=include_all_pages,
                        max_pages=max_pages)
                except Exception as exc:  # noqa: BLE001 - network boundary
                    logger.warning("membership: %s failed (%r)", slug, exc)
                    self.failed.setdefault(index, f"{type(exc).__name__}: {exc}")
                    continue
                if not getattr(res, "ok", False) or not res.data:
                    self.failed.setdefault(
                        index, str(getattr(res, "error", "no data")))
                    continue
                rows = res.data.get("rows") or []
                if rows:
                    found_any = True
                for row in rows:
                    symbol = _row_symbol(row)
                    if symbol:
                        # First sector wins: a company listed under two
                        # sub-industries still counts as one membership.
                        self.symbol_to_index.setdefault(symbol, index)
            if not found_any:
                self.failed.setdefault(index, "sector returned no rows")
        self.built = True
        return self

    # ---- query ------------------------------------------------------------ #
    def sector_of(self, symbol: str) -> Optional[str]:
        return self.symbol_to_index.get(str(symbol).upper())

    def known(self, symbol: str) -> bool:
        return str(symbol).upper() in self.symbol_to_index

    @property
    def empty(self) -> bool:
        return not self.symbol_to_index


def _row_symbol(row: Any) -> Optional[str]:
    """Best-effort symbol extraction from a sector table row."""
    if not isinstance(row, dict):
        return None
    for key in ("_symbol", "Symbol", "Ticker"):
        value = row.get(key)
        if value:
            return str(value).strip().upper()
    label = row.get("Name") or row.get("Company")
    if label:
        text = str(label).strip().upper()
        if 0 < len(text) <= 20:
            return text
    return None


def build_membership(index_to_slugs: Dict[str, List[str]], client: Any,
                     **kwargs: Any) -> SectorMembership:
    """Build a membership lookup for the given sectors."""
    return SectorMembership(index_to_slugs).build(client, **kwargs)


def apply_membership(candidates: Sequence[Dict[str, Any]],
                     membership: SectorMembership
                     ) -> Dict[str, Any]:
    """Split candidates into kept / dropped using the lookup.

    Returns ``{"kept", "dropped", "keep_symbols", "drop_symbols"}``. A
    candidate with no established sector is DROPPED and reported as
    ``unknown_sector`` - fail-closed by design.
    """
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for cand in candidates or []:
        symbol = str(cand.get("symbol") or "").upper()
        index = membership.sector_of(symbol)
        if index:
            cand = dict(cand)
            cand["sector_index"] = index
            kept.append(cand)
        else:
            dropped.append({"symbol": symbol, "reason": "unknown_sector"})
    return {
        "kept": kept,
        "dropped": dropped,
        "keep_symbols": [c.get("symbol") for c in kept],
        "drop_symbols": [d["symbol"] for d in dropped],
    }
