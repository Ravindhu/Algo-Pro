"""
sector_dataset.py - historical cross-sectional dataset for sector rotation.

WHY THIS EXISTS
---------------
The live HM table answers "which sectors are cheap TODAY". One day is not a
dataset: with ~15 sectors, a single snapshot is 15 rows, and no model can be
fitted or honestly validated on that.

The price parquets already contain **years** of daily bars, so the cross-section
can be reconstructed retroactively: for every past trading date, score every
sector as of that date and record what each sector actually returned over the
next horizon. That turns 15 rows into thousands, today, with no waiting.

WHAT ONE ROW IS
---------------
    (as_of_date, sector) -> { features..., forward_return, label }

The target is CROSS-SECTIONAL, per `ml/features/cross_section.py`: within one
date, rank the sectors by forward return and label the top cohort. A
time-series label ("did this sector rise?") is dominated by market drift and is
near-unlearnable; a rank label subtracts the common market move and asks the
only question that matters for rotation:

    "which sectors will outperform the others over the next N days?"

NO LOOK-AHEAD (the whole difficulty)
------------------------------------
Three separate precautions, because a leak here silently inflates every
backtest number:

1. **Features at t use bars up to and including t only.** HM/RSI is computed on
   the truncated series, never the full one.
2. **Labels read t -> t+horizon.** That is a label's job, and the final
   `horizon` rows carry `None` so no fabricated label can be fitted.
3. **Cross-sectional ranking is PER DATE ONLY.** Grouping is done on `as_of`,
   so no statistic ever crosses dates.

The walk-forward layer then adds a purge/embargo, so train and test do not
overlap by `horizon` bars. That is enforced there, not here, but the dataset
records `horizon` in its provenance so the split can use it.

DELIBERATE LIMITS
-----------------
* Only SECTOR indices are included (benchmarks and VIX are excluded), so a
  ranking is never polluted by a market-cap bucket - see sector_hm_score.
* A date needs `min_width` sectors with valid features to form a cross-section.
  With 2 names a "top half" is 1 name and the label is noise; such dates are
  dropped and counted, not silently kept.
* Survivorship: the universe is whatever parquets exist today. An index added
  later has no history before its start date. This is recorded as
  `first_date` per sector, so the bias is visible rather than assumed away.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_HORIZONS",
    "DEFAULT_DATASET_PATH",
    "SectorDatasetBuilder",
    "build_dataset",
]

#: Forward horizons, in trading sessions, to label. 5 and 20 cover the two
#: natural rotation speeds (weekly, monthly) and cost the same to compute.
DEFAULT_HORIZONS = (5, 20)

#: Below this many sectors on a date, a cross-section is not a ranking.
MIN_WIDTH = 5

DEFAULT_DATASET_PATH = (Path(__file__).resolve().parents[1] / "configs"
                        / "sector_dataset.json")


class SectorDatasetBuilder:
    """Builds the (date x sector) feature/label panel from vendor parquets."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = dict(config or {})
        self.config = cfg
        from .sector_hm_score import SectorHMScorer
        # BUG FIXED 2026-10-24: `indices_dir` on THIS config was ignored, so a
        # caller pointing the builder at a fixture directory silently read the
        # real vendor data instead - the panel had 14 sectors and 5,313 rows
        # when the fixture held 6. Tests caught it; production would have read
        # the wrong directory just as quietly.
        explicit_dir = cfg.get("indices_dir")
        hm_cfg = dict(cfg.get("hm") or {})
        if explicit_dir and "indices_dir" not in hm_cfg:
            hm_cfg["indices_dir"] = explicit_dir
        self._hm = SectorHMScorer(hm_cfg)
        self.indices_dir = self._hm.indices_dir
        self.horizons: Sequence[int] = tuple(
            cfg.get("horizons") or DEFAULT_HORIZONS)
        self.rsi_period = self._hm.rsi_period
        self.ema_period = self._hm.ema_period
        self.discount_level = self._hm.discount_level
        self.min_width = int(cfg.get("min_width", MIN_WIDTH))
        #: Rows of history required before a sector's features are meaningful.
        self.warmup = int(cfg.get("warmup", 30))
        #: Tail length used for the RSI/EMA features. RSI(9) and EMA(3) are
        #: local, but ret_60 needs 61 bars and _dist_ema(20) needs 20, so this
        #: must exceed the longest lookback by a comfortable margin.
        self.rsi_window = int(cfg.get("rsi_window", 120))
        self.dataset_path = Path(
            cfg.get("dataset_path") or DEFAULT_DATASET_PATH)

    # ---- loading ---------------------------------------------------------- #
    def _load_panel(self) -> Any:
        """Date x sector close panel, sector indices only."""
        import pandas as pd

        frames = {}
        for stem in self._hm._stems():
            path = self.indices_dir / f"{stem}.parquet"
            if not path.is_file():
                continue
            try:
                frame = pd.read_parquet(path, columns=["Close"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("sector_dataset: %s unreadable (%r)", stem, exc)
                continue
            series = pd.Series(
                pd.to_numeric(frame["Close"], errors="coerce").to_numpy(),
                index=pd.to_datetime(frame.index, errors="coerce"))
            series = series[series.index.notna()]
            series = series[(series > 0) & series.notna()]
            series = series[~series.index.duplicated(keep="last")]
            if len(series):
                frames[stem] = series.sort_index()
        if not frames:
            return None
        panel = pd.DataFrame(frames).sort_index()
        # Drop dates where NO sector traded (holidays in a partial file).
        return panel.dropna(how="all")

    # ---- features --------------------------------------------------------- #
    @staticmethod
    def _features(closes: List[float], rsi_period: int, ema_period: int,
                  discount_level: float = 40.0,
                  full: Optional[List[float]] = None
                  ) -> Optional[Dict[str, float]]:
        """Point-in-time features from a series ending at t. None if too short.

        Every feature is a function of history up to and including the last
        element - there is no forward reference anywhere in this function.
        """
        from ..alpha.indicators.hilega_milega import ema, hilega_milega_rsi
        n = len(closes)
        if n < max(rsi_period + ema_period + 2, 30):
            return None
        rsi_series = hilega_milega_rsi(closes, rsi_period=rsi_period)
        ema_series = ema(rsi_series, ema_period)
        rsi = rsi_series[-1]
        ema_rsi = ema_series[-1]
        if rsi is None or ema_rsi is None:
            return None
        if discount_level is None:
            discount_level = 40.0

        def ret(lag: int) -> float:
            # Uses `full` so a lookback longer than the tail is still exact.
            src = full if (full is not None and len(full) > n) else closes
            if len(src) <= lag or src[-1 - lag] <= 0:
                return 0.0
            return src[-1] / src[-1 - lag] - 1.0

        # Realised volatility of the last 20 returns (a scale feature).
        window = closes[-21:]
        rets = [window[i] / window[i - 1] - 1.0
                for i in range(1, len(window)) if window[i - 1] > 0]
        if rets:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
            vol20 = var ** 0.5
        else:
            vol20 = 0.0

        return {
            "rsi": float(rsi),
            "ema_rsi": float(ema_rsi),
            # The HM score, identical to the live table's definition.
            "hm_score": float(100.0 * max(0.0, min(1.0, 1.0 - rsi / 100.0))),
            "rising": 1.0 if rsi > ema_rsi else 0.0,
            "below_level": 1.0 if rsi <= discount_level else 0.0,
            "ret_5": ret(5),
            "ret_20": ret(20),
            "ret_60": ret(60),
            "vol_20": float(vol20),
            "dist_ema20": SectorDatasetBuilder._dist_ema(closes, 20),
        }

    @staticmethod
    def _dist_ema(closes: Sequence[float], span: int = 20) -> float:
        """Close relative to its EMA, a simple trend-position feature."""
        if len(closes) < span:
            return 0.0
        k = 2.0 / (span + 1.0)
        ema_val = closes[0]
        for c in closes[1:]:
            ema_val = k * c + (1 - k) * ema_val
        return float(closes[-1] / ema_val - 1.0) if ema_val else 0.0

    # ---- cross-section ---------------------------------------------------- #
    def _rank_labels(self, rows: List[Dict[str, Any]], key: str) -> None:
        """Attach a per-date cross-sectional rank label. NO cross-date state.

        ``label_<h>`` = 1 for the top tercile of forward return that date, 0 for
        the bottom tercile, and None for the middle (deliberately excluded from
        training: it is neither a buy nor a clear avoid).
        """
        for row in rows:
            row.pop(f"label_{key}", None)
        for as_of, group in self._group_by_date(rows):
            valid = [r for r in group if r.get(f"fwd_{key}") is not None]
            if len(valid) < self.min_width:
                for r in group:
                    r[f"label_{key}"] = None
                continue
            valid.sort(key=lambda r: r[f"fwd_{key}"], reverse=True)
            n = len(valid)
            tier = max(1, n // 3)
            for i, r in enumerate(valid):
                r[f"label_{key}"] = 1 if i < tier else (0 if i >= n - tier
                                                        else None)
                r[f"rank_{key}"] = i + 1
            for r in group:
                if r.get(f"fwd_{key}") is None:
                    r[f"label_{key}"] = None

    @staticmethod
    def _group_by_date(rows: List[Dict[str, Any]]):
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            buckets.setdefault(r["as_of"], []).append(r)
        for key in sorted(buckets):
            yield key, buckets[key]

    # ---- build ------------------------------------------------------------ #
    def build(self) -> Dict[str, Any]:
        """Return the dataset document. Always returns a dict, never raises."""
        panel = self._load_panel()
        if panel is None or len(panel) == 0:
            return {"ok": False, "reason": "no sector parquets readable",
                    "rows": []}

        stems = list(panel.columns)
        dates = list(panel.index)
        closes_by = {s: [None if v != v else float(v)
                         for v in panel[s].tolist()] for s in stems}

        rows: List[Dict[str, Any]] = []
        dropped_width = 0
        per_date_width: List[int] = []

        # LINEAR, PER-SECTOR PASS. The previous shape was O(dates^2) per sector:
        # it rebuilt `hist = series[:i+1]` and then filtered it, for every date,
        # which is why a 4,701-date x 14-sector build did not finish. Here each
        # sector is walked once carrying a running count of usable bars, and
        # only the `rsi_window` tail is ever materialised.
        window = max(self.rsi_window, 61)
        by_date: Dict[str, List[Dict[str, Any]]] = {}

        for stem in stems:
            series = closes_by[stem]
            usable = 0
            for i, stamp in enumerate(dates):
                value = series[i]
                if value is None:
                    continue
                usable += 1                      # bars up to AND INCLUDING i
                if usable < self.warmup:
                    continue
                hist = [c for c in series[max(0, i + 1 - window): i + 1]
                        if c is not None]
                if len(hist) < 2:
                    continue
                feats = self._features(hist, self.rsi_period,
                                       self.ema_period, self.discount_level)
                if feats is None:
                    continue
                row: Dict[str, Any] = {
                    "as_of": stamp.date().isoformat(),
                    "as_of_ts": stamp.isoformat(),
                    "sector": stem,
                    "close": value,
                }
                row.update(feats)
                # FORWARD returns: the label. None near the tail, by design.
                for h in self.horizons:
                    j = i + h
                    fwd = series[j] if j < len(dates) else None
                    row[f"fwd_{h}d"] = ((fwd / value) - 1.0
                                        if (fwd is not None and value > 0)
                                        else None)
                by_date.setdefault(row["as_of"], []).append(row)

        for as_of in sorted(by_date):
            day_rows = by_date[as_of]
            if len(day_rows) < self.min_width:
                dropped_width += 1
                continue
            per_date_width.append(len(day_rows))
            rows.extend(day_rows)

        for h in self.horizons:
            self._rank_labels(rows, f"{h}d")

        first_date = {s: (panel[s].dropna().index[0].date().isoformat()
                          if panel[s].notna().any() else None) for s in stems}
        labelled = {f"label_{h}d": sum(1 for r in rows if r.get(f"label_{h}d")
                                       is not None) for h in self.horizons}

        return {
            "ok": bool(rows),
            "kind": "sector_cross_section_dataset",
            "version": "sector-ds-v1",
            "generated_at": datetime.now().astimezone().isoformat(),
            "horizons": list(self.horizons),
            "features": ["rsi", "ema_rsi", "hm_score", "rising", "below_level",
                         "ret_5", "ret_20", "ret_60", "vol_20", "dist_ema20"],
            "label_rule": (
                "Per date, rank sectors by forward return; top tercile = 1, "
                "bottom tercile = 0, middle = None (excluded from training)."),
            "provenance": {
                "sectors": stems,
                "n_sectors": len(stems),
                "n_dates": len(per_date_width),
                "n_rows": len(rows),
                "date_range": ([min(by_date), max(by_date)] if by_date else []),
                "min_width": self.min_width,
                "dates_dropped_narrow": dropped_width,
                "median_width": sorted(per_date_width)[len(per_date_width) // 2]
                if per_date_width else 0,
                "first_date_per_sector": first_date,
                "labelled_rows": labelled,
                "warmup_sessions": self.warmup,
                "note": ("Survivorship: the universe is today's parquets, so a "
                         "sector's history starts at first_date_per_sector."),
            },
            "rows": rows,
        }

    # ---- persistence ------------------------------------------------------ #
    def persist(self, doc: Dict[str, Any]) -> Optional[Path]:
        try:
            self.dataset_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.dataset_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            tmp.replace(self.dataset_path)
            return self.dataset_path
        except Exception as exc:  # noqa: BLE001
            logger.warning("sector_dataset: persist failed (%r)", exc)
            return None


def build_dataset(config: Optional[Dict[str, Any]] = None,
                  persist: bool = True) -> Dict[str, Any]:
    """Build (and optionally save) the sector cross-section dataset."""
    builder = SectorDatasetBuilder(config)
    doc = builder.build()
    if persist and doc.get("ok"):
        path = builder.persist(doc)
        doc["persisted_to"] = str(path) if path else None
    return doc
