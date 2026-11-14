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

#: Consecutive sessions RSI must stay above its EMA before a sector qualifies.
#:
#: MEASURED 2026-10-24 on the 53,130-row dataset (3,810 dates, 14 sectors,
#: 5-day horizon). Requiring confirmation cuts flicker but also cuts the edge:
#:
#:   N  fires  hit-rate  edge    daily flips
#:   1   2519    0.490   -0.010      34.3%
#:   2   1755    0.502   +0.002      22.3%   <- DEFAULT
#:   3   1130    0.487   -0.013      15.2%
#:   4    696    0.484   -0.016      10.5%
#:   5    364    0.451   -0.049       7.2%
#:
#: N=1 flickers on ~1 day in 3. N=2 cuts that by a third while IMPROVING the
#: hit rate. N>=3 keeps cutting flicker but the edge turns negative, so more
#: confirmation is not free - it filters out the early part of the move, which
#: is where the edge lives. N=2 is therefore the only setting that is better on
#: BOTH counts. Values above ``MAX_CONFIRM_BARS`` are clamped down with a
#: warning rather than silently accepted.
DEFAULT_CONFIRM_BARS = 2
MAX_CONFIRM_BARS = 3


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
        #: Consecutive sessions RSI must stay above its EMA. Default 2 because
        #: that is the only value that reduced flicker (34.3% -> 22.3% daily
        #: flips) AND improved the hit rate; N>=3 turned the edge negative.
        raw_confirm = int(cfg.get("confirm_bars", DEFAULT_CONFIRM_BARS))
        self.confirm_warning = ""
        if raw_confirm > MAX_CONFIRM_BARS:
            self.confirm_warning = (
                f"confirm_bars={raw_confirm} clamped to {MAX_CONFIRM_BARS}: "
                f"beyond that the measured edge is negative")
            raw_confirm = MAX_CONFIRM_BARS
        if raw_confirm < 1:
            self.confirm_warning = (
                f"confirm_bars={raw_confirm} raised to 1 (single-bar check)")
            raw_confirm = 1
        self.confirm_bars = raw_confirm
        #: Warnings produced by the gate itself (e.g. a clamped confirm_bars),
        #: surfaced so a misconfiguration is never silent.
        self._extra_warnings: List[str] = []
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
            # The measured rule: the RSI has stopped falling, confirmed over
            # `confirm_bars` consecutive sessions to suppress single-bar
            # flicker (see DEFAULT_CONFIRM_BARS for the measurements).
            chosen = [r for r in ranked
                      if r.get("rising")
                      and (r.get("confirm_run") or 0) >= self.confirm_bars]
            if self.require_discount:
                chosen = [r for r in chosen if r.get("below_level")]
            rule = f"RSI > EMA for {self.confirm_bars}+ session(s)"
            rule += (" AND RSI <= discount level"
                     if self.require_discount else "")
            if self.confirm_warning:
                self._extra_warnings = [self.confirm_warning]
        return {
            "ok": True,
            "rule": rule,
            "confirm_bars": self.confirm_bars,
            "as_of": table.get("as_of"),
            "selected": [r["stem"] for r in chosen],
            "details": chosen,
            "considered": len(ranked),
            "warnings": list(getattr(self, "_extra_warnings", []) or []),
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

        warnings: List[str] = list(gate.get("warnings") or [])
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
            "confirm_bars": gate.get("confirm_bars"),
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
