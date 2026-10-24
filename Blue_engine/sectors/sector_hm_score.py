"""
sector_hm_score.py - rank investable NSE SECTOR indices by Hilega-Milega score.

SCOPE (this is the point of the module)
---------------------------------------
The user's model separates two kinds of index:

  * **Benchmark indices** (NIFTY 50, NIFTY 500, SENSEX, MIDCAP 150, ...) - used
    ONLY to judge the portfolio. They are never "discounted", because you cannot
    allocate capital to a market-cap bucket as if it were a sector.
  * **Sector indices** (ENERGY, AUTO, IT, FMCG, BANK, CHEMICALS, ...) - these
    are the investable units that can be cheap or expensive versus the market.

This module scores ONLY the second group. Broad-market and volatility indices
are excluded by construction (see ``BENCHMARK_STEMS`` and ``EXCLUDED_STEMS``),
so a "least discounted" ranking can never be polluted by, say, INDIA VIX.

THE HM SCORE
------------
"Hilega-Milega" is the indicator in ``alpha/indicators/hilega_milega.py``:
RSI(9) with an EMA(3) drawn over it. Two distinct readings matter:

  * ``rsi``            - how depressed the sector is (lower = more beaten down)
  * ``hm_turn``        - RSI above its EMA, i.e. it has STOPPED falling

The user's phrase "discounted" is about the FIRST. The indicator's actual BUY
rule is BOTH together. This module reports both and never conflates them,
because "cheap" and "cheap and recovering" are different decisions.

Score construction (``hm_score``, 0-100, higher = MORE discounted):

    depth    = (1 - rsi/100)                      # 1 at RSI 0, 0 at RSI 100
    turn     = +1 if rsi > ema_rsi else -1        # direction of travel
    hm_score = 100 * depth, adjusted by the turn

The turn adjustment deliberately does NOT invert the ranking: a still-falling
sector keeps a high (discounted) score but is flagged ``falling_knife=True``,
because selling-into-a-falling-sector is the failure mode this system exists to
avoid. The caller decides; the score does not hide it.

NO LOOK-AHEAD
-------------
Every series is truncated at ``as_of``. RSI is computed on closes up to that
date only.

DEGRADE, NEVER HALT
-------------------
Missing parquets are skipped and reported. A sector with too little history is
listed as unscored WITH the reason, never silently dropped.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "BENCHMARK_STEMS",
    "EXCLUDED_STEMS",
    "SECTOR_INDEX_NAMES",
    "SectorHMScorer",
    "score_sectors",
]

#: Broad-market / size-bucket indices. These are for JUDGING the portfolio,
#: never for sector allocation. Excluded from every ranking.
BENCHMARK_STEMS = {
    "NIFTY_50", "SENSEX", "NIFTY_100", "NIFTY_200", "NIFTY_500",
    "NIFTY_NEXT_50", "NIFTY_MIDCAP_50", "NIFTY_MIDCAP_100", "NIFTY_MIDCAP_150",
    "NIFTY_SMALLCAP_250",
}

#: Not an investable equity sleeve at all.
EXCLUDED_STEMS = {"INDIA_VIX"}

#: Display name for each NSE sectoral index we hold, so the table reads like
#: the NSE list rather than like file names.
SECTOR_INDEX_NAMES: Dict[str, str] = {
    "NIFTY_BANK": "NIFTY BANK",
    "NIFTY_IT": "NIFTY IT",
    "NIFTY_PHARMA": "NIFTY PHARMA",
    "NIFTY_FMCG": "NIFTY FMCG",
    "NIFTY_AUTO": "NIFTY AUTO",
    "NIFTY_ENERGY": "NIFTY ENERGY",
    "NIFTY_METAL": "NIFTY METAL",
    "NIFTY_REALTY": "NIFTY REALTY",
    "NIFTY_PSU_BANK": "NIFTY PSU BANK",
    "NIFTY_FIN_SERVICE": "NIFTY FINANCIAL SERVICES",
    "NIFTY_INFRA": "NIFTY INFRASTRUCTURE",
    "NIFTY_CONSUMER_DURABLES": "NIFTY CONSUMER DURABLES",
    "NIFTY_CHEMICALS": "NIFTY CHEMICALS",
    "NIFTY_MNC": "NIFTY MNC",
    "NIFTY_PSE": "NIFTY PSE",
}

#: The default persist location. Daily history is what a future cross-sectional
#: ranking model needs, so it is written every run, not just displayed.
#: ``parents[1]`` is ``Blue_engine`` - this file is ``Blue_engine/sectors/...``.
DEFAULT_SCORE_PATH = (Path(__file__).resolve().parents[1] / "configs"
                      / "sector_hm_scores.json")


class SectorHMScorer:
    """Reads vendor parquets and produces the sector HM score table."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = dict(config or {})
        self.config = cfg
        self.indices_dir = Path(cfg.get("indices_dir") or self._default_dir())
        self.rsi_period = int(cfg.get("rsi_period", 9))
        self.ema_period = int(cfg.get("ema_period", 3))
        #: The RSI level under which a sector counts as "discounted". The user
        #: refers to this as the 40% line.
        self.discount_level = float(cfg.get("discount_level", 40.0))
        #: Minimum usable sessions for a meaningful RSI reading.
        self.min_sessions = int(cfg.get("min_sessions", 30))
        self.score_path = Path(cfg.get("score_path") or DEFAULT_SCORE_PATH)

    @staticmethod
    def _default_dir() -> Path:
        from .providers.vendor_sector import DEFAULT_INDICES_DIR

        return DEFAULT_INDICES_DIR

    # ---- data ------------------------------------------------------------- #
    def _stems(self) -> List[str]:
        """Sector indices only: benchmarks and VIX are never scored."""
        from .providers.vendor_sector import (INDEX_TICKERS,
                                              UNSCORABLE_NSE_SECTORS)

        skip = BENCHMARK_STEMS | EXCLUDED_STEMS | set(UNSCORABLE_NSE_SECTORS)
        return [s for s in INDEX_TICKERS if s not in skip]

    def _closes(self, stem: str, as_of: date) -> Optional[List[float]]:
        """Closes up to ``as_of``, oldest first. None if unusable."""
        path = self.indices_dir / f"{stem}.parquet"
        if not path.is_file():
            return None
        try:
            import pandas as pd

            frame = pd.read_parquet(path, columns=["Close"])
            index = pd.to_datetime(frame.index, errors="coerce")
            series = pd.Series(
                pd.to_numeric(frame["Close"], errors="coerce").to_numpy(),
                index=index)
            series = series[series.index.notna()]
            series = series[series.notna()]
            series = series[series > 0]
            series = series[series.index.date <= as_of]
            series = series.sort_index()
            return [float(v) for v in series.to_numpy()]
        except Exception as exc:  # noqa: BLE001 - a table must not crash
            logger.warning("sector_hm_score: %s unreadable (%r)", stem, exc)
            return None

    # ---- scoring ---------------------------------------------------------- #
    def score_one(self, stem: str, as_of: date) -> Dict[str, Any]:
        """HM reading for one sector. Always returns a dict."""
        from ..alpha.indicators.hilega_milega import ema, hilega_milega_rsi

        out: Dict[str, Any] = {
            "stem": stem,
            "name": SECTOR_INDEX_NAMES.get(stem, stem),
            "scored": False,
            "reason": "",
        }
        closes = self._closes(stem, as_of)
        if not closes:
            out["reason"] = "no usable price history"
            return out
        if len(closes) < self.min_sessions:
            out["reason"] = f"only {len(closes)} sessions (need {self.min_sessions})"
            return out

        rsi_series = hilega_milega_rsi(closes, rsi_period=self.rsi_period)
        ema_series = ema(rsi_series, self.ema_period)
        rsi = rsi_series[-1]
        ema_rsi = ema_series[-1]
        if rsi is None or ema_rsi is None:
            out["reason"] = "RSI/EMA not yet defined"
            return out

        rising = bool(rsi > ema_rsi)
        depth = max(0.0, min(1.0, 1.0 - (rsi / 100.0)))
        # Direction is a FLAG, not a multiplier: it must not be able to hide a
        # deeply discounted sector from the ranking.
        score = 100.0 * depth
        out.update({
            "scored": True,
            "rsi": round(rsi, 2),
            "ema_rsi": round(ema_rsi, 2),
            "hm_score": round(score, 2),
            "rising": rising,
            "below_level": rsi <= self.discount_level,
            # The indicator's real BUY rule: recovering AND still cheap.
            "hm_turn_buy": bool(rising and rsi <= self.discount_level),
            # Cheap but STILL FALLING. Reported so it is never mistaken for a
            # buy signal - this is the trap in "buy the most discounted".
            "falling_knife": bool((not rising) and rsi <= self.discount_level),
            "sessions": len(closes),
            "last_close": round(closes[-1], 2),
            "as_of": as_of.isoformat(),
        })
        return out

    def score_all(self, as_of: Optional[date] = None) -> Dict[str, Any]:
        """Full table, ranked most-discounted first."""
        as_of = as_of or date.today()
        rows = [self.score_one(stem, as_of) for stem in self._stems()]
        scored = [r for r in rows if r.get("scored")]
        scored.sort(key=lambda r: (-r["hm_score"], r["stem"]))
        unscored = [r for r in rows if not r.get("scored")]

        from .providers.vendor_sector import UNSCORABLE_NSE_SECTORS

        return {
            "as_of": as_of.isoformat(),
            "generated_at": datetime.now().astimezone().isoformat(),
            "lookback": {"rsi_period": self.rsi_period,
                         "ema_period": self.ema_period,
                         "discount_level": self.discount_level},
            "ranked": scored,
            "unscored": unscored,
            # Sectors the NSE lists as investable but no data source carries.
            "not_available": [{"stem": k, "name": SECTOR_INDEX_NAMES.get(k, k),
                               "reason": v}
                              for k, v in UNSCORABLE_NSE_SECTORS.items()],
            "counts": {
                "scored": len(scored),
                "unscored": len(unscored),
                "not_available": len(UNSCORABLE_NSE_SECTORS),
                "below_level": sum(1 for r in scored if r.get("below_level")),
                "rising": sum(1 for r in scored if r.get("rising")),
            },
        }

    # ---- selection -------------------------------------------------------- #
    def least_discounted(self, table: Optional[Dict[str, Any]] = None,
                         n: int = 5, *, require_turn: bool = True
                         ) -> Dict[str, Any]:
        """The ``n`` MOST discounted sectors, plus what was excluded and why.

        ``require_turn`` is the honest default: only sectors whose RSI has
        turned above its EMA qualify, which is the indicator's actual rule.
        With ``require_turn=False`` the raw lowest-RSI sectors are returned and
        any falling-knife names are flagged in ``warnings``.

        Returns a dict, never a bare list, so the caller can see WHY a sector
        was left out. An empty selection is a valid, reportable outcome.
        """
        table = table or self.score_all()
        scored = [r for r in table["ranked"] if r.get("scored")]
        warnings: List[str] = []

        if require_turn:
            eligible = [r for r in scored if r.get("hm_turn_buy")]
            held_back = [r for r in scored
                         if r.get("falling_knife") and r not in eligible]
            if held_back:
                warnings.append(
                    f"{len(held_back)} discounted sector(s) withheld because "
                    f"RSI is still below its EMA (falling, not recovering): "
                    + ", ".join(r["stem"] for r in held_back))
        else:
            eligible = list(scored)
            knives = [r for r in eligible if r.get("falling_knife")]
            if knives:
                warnings.append(
                    f"{len(knives)} selected sector(s) are still falling "
                    f"(RSI below EMA): "
                    + ", ".join(r["stem"] for r in knives))

        if len(eligible) < n:
            warnings.append(
                f"only {len(eligible)} sector(s) qualify; requested {n}. "
                f"Allocating to fewer than {n} is the correct response - "
                f"filling the gap would mean buying sectors that do not. "
                f"Consider n=len(qualified) or a momentum-regime fallback.")

        chosen = eligible[:n]
        return {
            "requested": n,
            "qualified": len(eligible),
            "selected": chosen,
            "warnings": warnings,
            "as_of": table.get("as_of"),
        }

    # ---- persistence ------------------------------------------------------ #
    def persist(self, table: Dict[str, Any]) -> Optional[Path]:
        """Append today's table to the score history.

        Daily history is the ONLY way a cross-sectional ML ranker becomes
        trainable later: the sector universe is ~15 names, so the sample grows
        by DATE, not by ticker. Writing it every run costs nothing.
        """
        try:
            history: Dict[str, Any]
            if self.score_path.is_file():
                history = json.loads(self.score_path.read_text(encoding="utf-8"))
            else:
                history = {"kind": "sector_hm_score_history", "days": []}
            days = [d for d in history.get("days", [])
                    if d.get("as_of") != table.get("as_of")]
            days.append(table)
            days.sort(key=lambda d: d.get("as_of") or "")
            history["days"] = days[-4000:]     # ~16 years of trading days
            history["latest_as_of"] = table.get("as_of")
            self.score_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.score_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(history, indent=1), encoding="utf-8")
            tmp.replace(self.score_path)
            return self.score_path
        except Exception as exc:  # noqa: BLE001 - persistence is best effort
            logger.warning("sector_hm_score: persist failed (%r)", exc)
            return None


def score_sectors(config: Optional[Dict[str, Any]] = None,
                  as_of: Optional[date] = None,
                  persist: bool = True) -> Dict[str, Any]:
    """Convenience wrapper: build the table, optionally persist it."""
    scorer = SectorHMScorer(config)
    table = scorer.score_all(as_of)
    if persist:
        path = scorer.persist(table)
        table["persisted_to"] = str(path) if path else None
    return table
