"""
sector_universe_filter.py - choose WHICH sectors the stock screener may search.

THE RULE, AND WHY IT IS NOT WHAT WAS ORIGINALLY ASKED FOR
---------------------------------------------------------
The brief was "find the most discounted sector". Measured out-of-sample on
3,810 dates (5d forward, top-tercile hit rate, random = 0.500):

    RSI <= 40                    0.454   <- WORSE than random
    RSI>EMA AND RSI <= 40        0.479   <- still worse
    RSI > EMA (any level)        0.518   <- the only rule above random
    strongest 20d momentum       0.528

So the **discount cap hurt** and the **EMA cross helped**. The shipped rule is
therefore ``RSI > EMA`` with NO RSI cap. The 40% level survives only as a
``deep`` display flag, because labelling a sector "deep" is useful context even
though filtering on it costs money.

WHY THERE IS NO COST TEST HERE
------------------------------
This chooses a universe; it does not trade. Ranking sectors is free, and the
delivery round trip is paid once on the STOCK trades downstream. Applying a
10 bps hurdle to the sector layer (as an earlier pass did) rejects a signal
that never incurs that cost - the two uses are not comparable.

FAIL-CLOSED, NOT FAIL-OPEN
--------------------------
If no sector qualifies, the filter returns an EMPTY selection and says why. It
never falls back to "all sectors" to keep the pipeline busy: silently widening
a filter is how a momentum gate turns into buying into weakness.

DEGRADE, NEVER HALT
-------------------
Missing data, an unmapped sector or a scorer exception produce a documented
skip, never an exception.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = ["SectorUniverseFilter", "filter_universe"]

#: The measured gate. Changing this is a strategy change: re-run
#: ``ml/evaluation/sector_ranker_eval.py`` before doing so.
DEFAULT_MODE = "rsi_above_ema"


class SectorUniverseFilter:
    """Turns the sector HM table into a list of screener.in sector slugs."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = dict(config or {})
        self.config = cfg
        #: ``rsi_above_ema`` (default, measured) | ``below_level`` (the old
        #: discount-only rule, kept so the choice is explicit and testable).
        self.mode = str(cfg.get("mode") or DEFAULT_MODE)
        #: Optional override for the RSI cap used by ``below_level`` mode.
        self.discount_level = float(cfg.get("discount_level", 40.0))
        #: When True a sector must also be below the discount level. OFF by
        #: default because that condition measured NEGATIVE.
        self.require_discount = bool(cfg.get("require_discount", False))
        self.scorer = cfg.get("scorer")

    # ---- sector selection ------------------------------------------------- #
    def _scorer(self):
        if self.scorer is not None:
            return self.scorer
        from .sector_hm_score import SectorHMScorer

        self.scorer = SectorHMScorer(self.config.get("hm") or {})
        return self.scorer

    def qualifying_sectors(self, as_of: Optional[date] = None
                           ) -> Dict[str, Any]:
        """Scored sectors that pass the gate, most attractive first."""
        try:
            table = self._scorer().score_all(as_of)
        except Exception as exc:  # noqa: BLE001 - degrade, never halt
            logger.warning("sector filter: scorer failed (%r)", exc)
            return {"ok": False, "selected": [], "reason":
                    f"sector scoring failed ({type(exc).__name__})",
                    "table": None}

        ranked = [r for r in table.get("ranked") or [] if r.get("scored")]
        if self.mode == "below_level":
            chosen = [r for r in ranked if r.get("below_level")]
            rule = f"RSI <= {self.discount_level:.0f}"
        else:
            # The measured rule: the RSI has stopped falling.
            chosen = [r for r in ranked if r.get("rising")]
            if self.require_discount:
                chosen = [r for r in chosen if r.get("below_level")]
            rule = "RSI > EMA" + (" AND RSI <= discount level"
                                  if self.require_discount else "")

        return {
            "ok": True,
            "rule": rule,
            "as_of": table.get("as_of"),
            "selected": [r["stem"] for r in chosen],
            "details": chosen,
            "considered": len(ranked),
            "table": table,
        }

    # ---- slug expansion --------------------------------------------------- #
    def screener_slugs(self, as_of: Optional[date] = None) -> Dict[str, Any]:
        """Qualifying sectors expanded to screener.in slugs.

        ``unmapped`` lists sectors that passed the gate but have no screener
        equivalent (e.g. NIFTY MNC) - reported so the universe is not silently
        narrower than it looks.
        """
        from .sector_screener_map import is_mapped, screener_sectors_for, \
            unmapped_reason

        gate = self.qualifying_sectors(as_of)
        if not gate.get("ok"):
            return {"ok": False, "slugs": [], "sectors": [],
                    "reason": gate.get("reason"), "unmapped": []}

        slugs: List[str] = []
        mapped: List[str] = []
        unmapped: List[Dict[str, str]] = []
        for stem in gate["selected"]:
            if not is_mapped(stem):
                unmapped.append({"sector": stem,
                                 "reason": unmapped_reason(stem)})
                continue
            mapped.append(stem)
            for slug in screener_sectors_for(stem):
                if slug not in slugs:      # order-preserving dedupe
                    slugs.append(slug)

        warnings: List[str] = []
        if not mapped:
            warnings.append(
                "no qualifying sector maps to a screener.in sector; the stock "
                "universe is EMPTY. The filter does not fall back to all "
                "sectors - that would silently remove the gate.")
        if unmapped:
            warnings.append(
                "sectors passing the gate with no screener equivalent (not "
                "screened): " + ", ".join(u["sector"] for u in unmapped))

        return {
            "ok": True,
            "rule": gate.get("rule"),
            "as_of": gate.get("as_of"),
            "sectors": mapped,
            "slugs": slugs,
            "unmapped": unmapped,
            "warnings": warnings,
            "table": gate.get("table"),
        }


def filter_universe(config: Optional[Dict[str, Any]] = None,
                    as_of: Optional[date] = None) -> Dict[str, Any]:
    """Convenience wrapper."""
    return SectorUniverseFilter(config).screener_slugs(as_of)
