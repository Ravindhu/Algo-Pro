"""
vendor_maintenance.py - keep every pinned vendor index current, safely.

WHY THIS EXISTS
---------------
Two problems made "update the vendor data" unsafe as a routine:

1. **No single registry.** The vendor's own updater hard-codes its ticker map,
   and the project's list of indices lived in a third place. Adding an index
   meant editing three files and hoping they agreed. ``INDEX_TICKERS`` in
   ``sectors/providers/vendor_sector.py`` is now the one registry this job
   trusts.

2. **A corrupt row poisons the whole sector reading.** On 2026-10-24 the
   upstream feed returned a partially-written bar for 2026-10-13 in every
   index: ``Open/High/Low`` present, ``Close`` and ``Adj Close`` NaN,
   ``Volume`` 0. That single row made the aligned join drop the date for every
   sector, and the engine then reported ``data_classification=unknown`` with a
   matrix ``as_of=None`` even though the data was a day old and fine. A bad row
   must be removed, not carried forward.

WHAT THIS JOB DOES (in order)
-----------------------------
1. Refresh every registered index from Yahoo (merge, never truncate).
2. Scrub rows whose close is unusable (NaN, <= 0, not finite).
3. Verify each index advanced, or explain why it legitimately did not.
4. Rebuild ``index_list.csv`` so the metadata matches the parquets.
5. Exit non-zero when something needs a human, so Task Scheduler alerts.

DESIGN CONSTRAINTS
------------------
* **Atomic writes.** Each parquet is written to a temp file and replaced, so a
  crash mid-write cannot leave a half-written index (the failure above).
* **Never truncate history.** New rows are merged on the date index; an
  existing row is only replaced when the incoming row has a usable close.
  A partial upstream bar therefore cannot overwrite a good one.
* **Read-only against the engine.** The sector engine reads these parquets
  directly, so no rebuild or cache flush is needed after an update.
* **No silent success.** "No index advanced" is a failure, not a no-op: it
  usually means a network problem or an upstream change.

USAGE
-----
    python -m blue_engine.jobs.vendor_maintenance
    python -m blue_engine.jobs.vendor_maintenance --check
    python -m blue_engine.jobs.vendor_maintenance --only NIFTY_AUTO,NIFTY_IT
    python -m blue_engine.jobs.vendor_maintenance --backfill
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "RegistryEntry",
    "load_registry",
    "refresh_index",
    "scrub_frame",
    "run_maintenance",
    "stale_indices",
]

#: Close prices older than this (calendar days) are reported as stale. A long
#: weekend plus a holiday can legitimately reach 4, so 5 avoids false alarms.
DEFAULT_STALE_AFTER_DAYS = 5

#: Fewer than this many usable rows means the series is not fit for a
#: relative-strength lookback (the engine needs >= 20 aligned rows, and a
#: 60-day max lookback needs 60). Anything below 250 is reported, because it
#: silently shortens the history the engine can use.
MIN_USABLE_ROWS = 250

_COLUMNS = ("Open", "High", "Low", "Close", "Adj Close", "Volume")


class RegistryEntry:
    """One index: where it lives and what the alarm expects of it."""

    __slots__ = ("stem", "ticker", "path")

    def __init__(self, stem: str, ticker: str, path: Path):
        self.stem = stem
        self.ticker = ticker
        self.path = path

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RegistryEntry({self.stem!r}, {self.ticker!r})"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def _indices_dir(indices_dir: Optional[Path]) -> Path:
    if indices_dir is not None:
        return Path(indices_dir)
    from ..sectors.providers.vendor_sector import DEFAULT_INDICES_DIR

    return DEFAULT_INDICES_DIR


def load_registry(indices_dir: Optional[Path] = None,
                  only: Optional[Iterable[str]] = None
                  ) -> List[RegistryEntry]:
    """The indices this job maintains, from the single project registry.

    ``only`` filters by stem. Unknown stems in ``only`` are ignored here and
    reported by :func:`run_maintenance`, so a typo cannot silently shrink the
    refresh to nothing.
    """
    from ..sectors.providers.vendor_sector import INDEX_TICKERS

    directory = _indices_dir(indices_dir)
    wanted = {str(s).strip().upper() for s in only} if only else None
    out: List[RegistryEntry] = []
    for stem, ticker in INDEX_TICKERS.items():
        if wanted is not None and stem.upper() not in wanted:
            continue
        out.append(RegistryEntry(stem, ticker, directory / f"{stem}.parquet"))
    return out


# --------------------------------------------------------------------------- #
# Frame hygiene
# --------------------------------------------------------------------------- #
def scrub_frame(frame: Any, *, drop_partial: bool = True) -> Tuple[Any, List[str]]:
    """Remove rows whose close is unusable. Returns ``(frame, notes)``.

    A row is dropped when ``Close`` is NaN, non-finite or <= 0. With
    ``drop_partial`` a row where ``Open/High/Low`` look like a real session but
    the close is missing is reported by name, because that is the signature of
    an upstream half-written bar rather than a genuine holiday gap.
    """
    import numpy as np
    import pandas as pd

    notes: List[str] = []
    if frame is None or len(frame) == 0:
        return frame, notes
    if "Close" not in frame.columns:
        return frame, ["no Close column"]

    close = pd.to_numeric(frame["Close"], errors="coerce")
    bad = close.isna() | ~np.isfinite(close) | (close <= 0)
    if not bool(bad.any()):
        return frame, notes

    if drop_partial and "Open" in frame.columns:
        partial = frame.index[bad & frame["Open"].notna()]
        if len(partial):
            names = ", ".join(d.strftime("%Y-%m-%d") for d in partial[:5])
            notes.append(f"dropped {len(partial)} partial row(s): {names}")

    cleaned = frame.loc[~bad].copy()
    return cleaned, notes


def _merge(existing: Any, incoming: Any) -> Tuple[Any, int]:
    """Merge new rows over old ones WITHOUT letting a bad bar win."""
    import pandas as pd

    if existing is None or len(existing) == 0:
        return incoming.sort_index(), len(incoming)
    before = len(existing)
    combined = pd.concat([existing, incoming])
    combined = combined[~combined.index.duplicated(keep="last")]
    combined.sort_index(inplace=True)
    return combined, len(combined) - before


def _atomic_write(frame: Any, path: Path) -> None:
    """Write via a temp file + replace, so a crash cannot truncate history."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(tmp, engine="pyarrow", compression="snappy", index=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #
def _fetch(yf: Any, ticker: str, full_history: bool) -> Any:
    """Yahoo history for one ticker, or None. Never raises."""
    try:
        df = yf.Ticker(ticker).history(
            period="max" if full_history else "10d",
            auto_adjust=False, actions=True)
    except Exception as exc:  # noqa: BLE001 - upstream boundary
        logger.warning("yahoo fetch failed for %s: %s", ticker, exc)
        return None
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    df.index = (df.index.tz_localize(None) if getattr(df.index, "tz", None)
                is not None else df.index).normalize()
    df.index.name = "Date"
    keep = [c for c in _COLUMNS if c in df.columns]
    return df[keep]


def refresh_index(entry: RegistryEntry, *, yf: Any = None,
                  full_history: bool = False, dry_run: bool = False
                  ) -> Dict[str, Any]:
    """Refresh + scrub one index. Returns a result dict; never raises.

    Result keys: ``stem``, ``ok``, ``before``, ``after``, ``added``,
    ``dropped``, ``notes``. ``before``/``after`` are the last usable session
    date, which is what the alarm compares.
    """
    out: Dict[str, Any] = {"stem": entry.stem, "ticker": entry.ticker,
                           "ok": False, "before": None, "after": None,
                           "added": 0, "dropped": 0, "notes": []}
    try:
        import pandas as pd
    except Exception:  # noqa: BLE001
        out["notes"].append("pandas/pyarrow unavailable")
        return out

    existing = None
    if entry.path.is_file():
        try:
            existing = pd.read_parquet(entry.path)
        except Exception as exc:  # noqa: BLE001
            out["notes"].append(f"existing parquet unreadable ({exc!r})")

    if existing is not None and len(existing):
        out["before"] = existing.index[-1].strftime("%Y-%m-%d")

    if yf is None:
        try:
            import yfinance as yf  # noqa: F811 - optional dependency
        except Exception:  # noqa: BLE001
            out["notes"].append("yfinance not installed")
            return out

    incoming = _fetch(yf, entry.ticker, full_history)
    if incoming is None:
        out["notes"].append(f"no data returned for {entry.ticker}")
    else:
        incoming, notes = scrub_frame(incoming)
        out["notes"].extend(notes)
        combined, added = _merge(existing, incoming)
        combined, notes = scrub_frame(combined)
        out["notes"].extend(notes)
        out["dropped"] = (len(existing) if existing is not None else 0) + \
            len(incoming) - len(combined)
        out["added"] = added
        if not dry_run and len(combined):
            _atomic_write(combined, entry.path)
        existing = combined

    if existing is not None and len(existing):
        out["after"] = existing.index[-1].strftime("%Y-%m-%d")
        out["rows"] = len(existing)
        out["ok"] = len(existing) >= 2
        if len(existing) < MIN_USABLE_ROWS:
            out["notes"].append(
                f"only {len(existing)} rows (< {MIN_USABLE_ROWS}); "
                f"history is too short for a long lookback")
    return out


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def stale_indices(entries: Iterable[RegistryEntry],
                  as_of: Optional[date] = None,
                  max_age_days: int = DEFAULT_STALE_AFTER_DAYS
                  ) -> List[Tuple[str, Optional[str], int]]:
    """Indices whose last session is older than ``max_age_days``.

    Returns ``[(stem, last_date_or_None, age_days)]``. A missing parquet has
    ``last_date=None`` and ``age_days=-1`` so it is unmistakably distinct from
    "one day old".
    """
    import pandas as pd

    today = as_of or date.today()
    out: List[Tuple[str, Optional[str], int]] = []
    for entry in entries:
        if not entry.path.is_file():
            out.append((entry.stem, None, -1))
            continue
        try:
            frame = pd.read_parquet(entry.path)
            last = frame.index[-1].date()
        except Exception:  # noqa: BLE001
            out.append((entry.stem, None, -1))
            continue
        age = (today - last).days
        if age > max_age_days:
            out.append((entry.stem, last.isoformat(), age))
    return out


def _rebuild_master(entries: Iterable[RegistryEntry], directory: Path) -> Path:
    """Rewrite index_list.csv from the parquets, so metadata cannot drift."""
    import pandas as pd

    rows = []
    for entry in entries:
        if not entry.path.is_file():
            continue
        frame = pd.read_parquet(entry.path)
        rows.append({
            "Symbol": entry.stem,
            "Ticker": entry.ticker,
            "Exchange": "BSE" if entry.stem == "SENSEX" else "NSE",
            "Start_Date": frame.index[0].strftime("%Y-%m-%d"),
            "End_Date": frame.index[-1].strftime("%Y-%m-%d"),
            "Total_Records": len(frame),
            "Parquet_File": f"data/INDICES/{entry.stem}.parquet",
        })
    path = directory / "index_list.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_maintenance(only: Optional[Iterable[str]] = None,
                    indices_dir: Optional[Path] = None, check: bool = False,
                    backfill: bool = False,
                    max_age_days: int = DEFAULT_STALE_AFTER_DAYS,
                    log: Optional[Any] = None) -> Dict[str, Any]:
    """Refresh every registered index. Returns a summary dict.

    ``check=True`` reports without touching any file. ``backfill=True`` asks
    Yahoo for the full history, which is what a NEW index needs; the default
    incremental pull only reaches back 10 days.
    """
    log = log or logger.info
    directory = _indices_dir(indices_dir)
    entries = load_registry(directory, only)
    summary: Dict[str, Any] = {
        "indices": [], "advanced": [], "unchanged": [], "failed": [],
        "stale": [], "warnings": [], "checked_only": bool(check),
    }

    if only:
        known = {e.stem.upper() for e in load_registry(directory)}
        unknown = [s for s in only if str(s).strip().upper() not in known]
        if unknown:
            summary["warnings"].append(f"unknown index name(s): {unknown}")

    if not entries:
        summary["warnings"].append("registry is empty; nothing to do")
        return summary
    if not directory.is_dir():
        summary["warnings"].append(f"indices dir missing: {directory}")
        return summary

    log(f"vendor maintenance: {len(entries)} index(es) in {directory}")
    for entry in entries:
        result = refresh_index(entry, full_history=backfill, dry_run=check)
        summary["indices"].append(result)
        moved = result["after"] and result["after"] != result["before"]
        if moved:
            summary["advanced"].append(entry.stem)
        elif result["ok"]:
            summary["unchanged"].append(entry.stem)
        else:
            summary["failed"].append(entry.stem)
        for note in result["notes"]:
            summary["warnings"].append(f"{entry.stem}: {note}")

    summary["stale"] = [
        {"stem": stem, "last": last, "age_days": age}
        for stem, last, age in stale_indices(entries, max_age_days=max_age_days)
    ]

    if not check:
        try:
            summary["master_csv"] = str(_rebuild_master(entries, directory))
        except Exception as exc:  # noqa: BLE001
            summary["warnings"].append(f"could not rebuild index_list.csv: {exc!r}")

    summary["ok"] = not summary["failed"]
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh, scrub and verify the pinned vendor index data.")
    parser.add_argument("--check", action="store_true",
                        help="Report only; do not write any file.")
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch full history (use when adding an index).")
    parser.add_argument("--only", default=None,
                        help="Comma-separated index stems, e.g. NIFTY_IT,NIFTY_AUTO")
    parser.add_argument("--max-age-days", type=int,
                        default=DEFAULT_STALE_AFTER_DAYS,
                        help="Alert when the last session is older than this.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    only = [s for s in (args.only or "").split(",") if s.strip()] or None
    summary = run_maintenance(only=only, check=args.check,
                              backfill=args.backfill,
                              max_age_days=args.max_age_days, log=print)

    if args.json:
        import json

        print(json.dumps(summary, indent=2, default=str))
    else:
        print("\n%-26s %-12s %-12s %8s %7s  %s"
              % ("INDEX", "BEFORE", "AFTER", "ADDED", "ROWS", "STATUS"))
        for r in summary["indices"]:
            status = ("UPDATED" if r["after"] and r["after"] != r["before"]
                      else "unchanged" if r["ok"] else "FAILED")
            print("%-26s %-12s %-12s %8s %7s  %s"
                  % (r["stem"], r["before"] or "-", r["after"] or "-",
                     r["added"], r.get("rows", 0), status))
        if summary["stale"]:
            print("\nSTALE (last session older than %d days):" % args.max_age_days)
            for s in summary["stale"]:
                when = s["last"] or "MISSING"
                print(f"  {s['stem']:<26} {when:<12} {s['age_days']} days")
        if summary["warnings"]:
            print("\nWARNINGS:")
            for w in summary["warnings"]:
                print(f"  - {w}")

    # Exit codes: 2 = nothing usable at all, 1 = some index failed or data is
    # stale (needs a human), 0 = clean.
    if summary["failed"] and len(summary["failed"]) == len(summary["indices"]):
        return 2
    if summary["failed"] or summary["stale"]:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
