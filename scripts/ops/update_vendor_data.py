r"""
update_vendor_data.py - keep the pinned vendor repo current, in place.

WHY THIS IS A THIN WRAPPER
--------------------------
`Blue_engine/database/vendor/indian-market-historical-data` is a pinned upstream
clone with its own `scripts/update_indices.py`. Duplicating that logic here would
drift from upstream on the next pull, so this script only orchestrates it:
verify the checkout, run the vendor's own updater, then prove the data the
sector engine actually reads has moved forward.

The data stays IN the project (about 2.7 MB for INDICES) rather than being
fetched per run. That is the point: the sector engine must work offline, and a
trading cycle must never wait on a third-party download.

WHAT "UPDATE" MEANS HERE
------------------------
The vendor updater appends new daily rows to `data/INDICES/*.parquet`. This
wrapper reports the before/after `as_of` of every index it touches, so a silent
no-op (network failure, holiday, upstream breakage) is visible instead of being
mistaken for success.

SAFETY
------
* Writes only inside the vendor checkout's `data/` directory.
* Never runs by default anywhere: it is a manual or scheduled command, not part
  of `start_all.py`, because a data pull must not delay a market open.
* Exits non-zero when no index advanced, which is what a cron/Task Scheduler
  job needs to raise an alert.

USAGE
-----
    python scripts\ops\update_vendor_data.py
    python scripts\ops\update_vendor_data.py --check     # report only
    python scripts\ops\update_vendor_data.py --sectors-only
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[2]
VENDOR_DIR = (ROOT / "Blue_engine" / "database" / "vendor"
              / "indian-market-historical-data")
INDICES_DIR = VENDOR_DIR / "data" / "INDICES"
UPDATER = VENDOR_DIR / "scripts" / "update_indices.py"

#: The indices the sector engine consumes, resolved from the ONE registry
#: (sectors/providers/vendor_sector.py:INDEX_TICKERS) rather than a copy here.
#: This used to be a hard-coded 5-name tuple, which silently disagreed with the
#: engine as soon as the engine gained sectors - the exact drift this module's
#: header warns about.
def _sector_stems() -> tuple:
    sys.path.insert(0, str(ROOT))
    from blue_engine.sectors.providers.vendor_sector import (  # noqa: E402
        DEFAULT_BENCHMARK, DEFAULT_SECTORS)

    return tuple(dict.fromkeys([DEFAULT_BENCHMARK, *DEFAULT_SECTORS.values()]))

SECTOR_INDICES = _sector_stems()


def _as_of(stem: str) -> Optional[str]:
    """Last date present in one index parquet, or None if unreadable."""
    path = INDICES_DIR / f"{stem}.parquet"
    if not path.is_file():
        return None
    try:
        import pandas as pd

        frame = pd.read_parquet(path, columns=None)
        if "Date" in frame.columns:
            dates = pd.to_datetime(frame["Date"], errors="coerce")
        else:
            dates = pd.to_datetime(frame.index, errors="coerce")
        dates = dates.dropna()
        return dates.max().date().isoformat() if len(dates) else None
    except Exception:  # noqa: BLE001 - a report must never crash the job
        return None


def snapshot(stems) -> Dict[str, Optional[str]]:
    return {stem: _as_of(stem) for stem in stems}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update the pinned vendor market history.")
    parser.add_argument("--check", action="store_true",
                        help="Report the current as_of dates and exit.")
    parser.add_argument("--sectors-only", action="store_true",
                        help="Only report/verify the indices the sector engine uses.")
    args = parser.parse_args()

    if not VENDOR_DIR.is_dir():
        print(f"[FAIL] vendor repo not found at {VENDOR_DIR}", file=sys.stderr)
        return 2

    stems = SECTOR_INDICES if args.sectors_only else None
    if stems is None:
        # Every parquet the vendor repo actually ships, which is now derived
        # from the registry so a new index is reported as soon as it lands.
        try:
            from blue_engine.sectors.providers.vendor_sector import INDEX_TICKERS
            stems = tuple(INDEX_TICKERS)
        except Exception:  # noqa: BLE001 - fall back to whatever is on disk
            stems = tuple(sorted(p.stem for p in INDICES_DIR.glob("*.parquet")))
    if not stems:
        print(f"[FAIL] no parquet files under {INDICES_DIR}", file=sys.stderr)
        return 2

    before = snapshot(stems)
    print(f"vendor: {VENDOR_DIR.name}")
    for stem, as_of in before.items():
        print(f"  {stem:<18} {as_of or '<missing>'}")

    if args.check:
        return 0

    if not UPDATER.is_file():
        print(f"[FAIL] upstream updater missing: {UPDATER}", file=sys.stderr)
        return 2

    print("\nrunning the vendor's own updater (scripts/update_indices.py) ...")
    rc = subprocess.call([sys.executable, str(UPDATER)], cwd=str(VENDOR_DIR))
    if rc != 0:
        print(f"[WARN] updater exited {rc}; reporting whatever advanced",
              file=sys.stderr)

    after = snapshot(stems)
    advanced = {s: after[s] for s in stems
                if after[s] and after[s] != before.get(s)}
    unchanged = [s for s in stems if s not in advanced]

    print("\nafter:")
    for stem in stems:
        mark = "UPDATED" if stem in advanced else "unchanged"
        print(f"  {stem:<18} {after.get(stem) or '<missing>':<12} {mark}")

    if not advanced:
        print("\n[WARN] no index advanced - possible network failure, a market "
              "holiday, or an upstream change. Do not assume the data is fresh.",
              file=sys.stderr)
        return 1

    print(f"\n[ok] {len(advanced)} index(es) advanced; "
          f"{len(unchanged)} unchanged.")
    print("The sector engine reads these files directly - no rebuild needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
