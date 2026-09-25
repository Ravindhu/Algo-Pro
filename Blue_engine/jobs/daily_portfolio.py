"""
jobs/daily_portfolio.py - the post-close portfolio persistence job.

What it does (once per trading day, after the close)
---------------------------------------------------
1. Fetch the live provider (SDK history + account) -> this **grows the price
   cache** by one day per sleeve, automatically.
2. Run the portfolio engine (via the full macro pipeline, or alone) so the
   target book, risk block and sizing are computed.
3. Persist the run to ``data/portfolio_runs/YYYY-MM-DD.jsonl`` (drift history,
   gross path, turnover) via ``portfolio_state``.
4. Write a small status file so the dashboard can show job health.
5. Exit non-zero on failure so Task Scheduler / cron can alert.

Why after the close
-------------------
History is end-of-day data: fetching intraday would cache a partial bar and
poison the covariance window. This job deliberately waits for the session to end.

Why a separate job (not inside the engine)
------------------------------------------
The engine is **pure by contract** — it must not do I/O in ``_run()``. Persistence
is a *job* concern, so it lives here. This mirrors the split between
``economics/run_history.py`` (storage) and the engine (computation).

Usage
-----
    # normal daily run (after 15:35 IST)
    python -m blue_engine.jobs.daily_portfolio

    # force: ignore the market-hours guard (for testing/catch-up)
    python -m blue_engine.jobs.daily_portfolio --force

    # dry: compute + report, but do not write anything
    python -m blue_engine.jobs.daily_portfolio --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.results import safe_float

logger = logging.getLogger(__name__)

# <repo>/data/job_status/
_REPO_ROOT = Path(__file__).resolve().parents[2]
STATUS_DIR = _REPO_ROOT / "data" / "job_status"
STATUS_FILE = STATUS_DIR / "daily_portfolio.json"

#: NSE cash session close (IST). The job waits until after this.
MARKET_CLOSE = dtime(15, 30)
#: When the job is allowed to start (gives the broker time to settle the EOD bar).
JOB_EARLIEST = dtime(15, 35)
#: NSE holidays are not enumerated here; a no-data day is treated as a holiday.

__all__ = ["run", "is_market_closed", "write_status", "read_status",
           "STATUS_FILE"]


def is_market_closed(now: Optional[datetime] = None) -> bool:
    """True when it is at/after the job window on a weekday (IST assumed).

    Weekends are excluded. Exchange holidays are not enumerated: on a holiday the
    provider simply returns no new bar, which the job reports as ``no_new_data``
    rather than an error.
    """
    now = now or datetime.now()
    if now.weekday() >= 5:          # Sat=5, Sun=6
        return False
    return now.time() >= JOB_EARLIEST


def write_status(payload: Dict[str, Any]) -> Optional[Path]:
    """Write the job status file (best-effort; never raises)."""
    try:
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        with STATUS_FILE.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        return STATUS_FILE
    except Exception as exc:  # noqa: BLE001 - status is best-effort
        logger.warning("could not write job status: %r", exc)
        return None


def read_status() -> Optional[Dict[str, Any]]:
    """Read the last job status (None when absent/unreadable)."""
    try:
        if STATUS_FILE.is_file():
            with STATUS_FILE.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None
    return None


def run(*, force: bool = False, dry_run: bool = False,
        config_path: Optional[str] = None,
        log: Optional[Any] = None) -> Dict[str, Any]:
    """Execute the daily job once.

    Returns a result dict (also written to the status file)::

        {"ok": bool, "ts": float, "date": "YYYY-MM-DD",
         "stage": "done"|"skipped"|"failed",
         "cached_rows": int, "sleeves": [...], "target_weights": {...},
         "gross": float, "turnover": float, "cost_bps": float,
         "run_file": str|None, "warnings": [...], "error": str|None}

    Never raises: a failure is captured in ``ok=False`` + ``error`` (so a
    scheduler sees a clean exit code and a readable status file).
    """

    def _log(msg: str) -> None:
        if callable(log):
            log(msg)
        else:
            logger.info("%s", msg)

    started = time.time()
    stamp = datetime.now()
    result: Dict[str, Any] = {
        "ok": False, "ts": started,
        "date": stamp.strftime("%Y-%m-%d"),
        "stage": "started", "cached_rows": 0, "rows_available": 0,
        "sleeves": [],
        "target_weights": {}, "gross": None, "turnover": None,
        "cost_bps": None, "run_file": None, "warnings": [], "error": None,
        "dry_run": bool(dry_run), "forced": bool(force),
    }

    # ---- 1. market-hours guard ------------------------------------------ #
    if not force and not is_market_closed(stamp):
        result.update(stage="skipped",
                      warnings=["market not closed yet; rerun after 15:35 IST "
                                "or pass --force"])
        _log("skipped: market not closed")
        write_status(result)
        return result

    try:
        # ---- 2. resolve the live provider -------------------------------- #
        from ..portfolio.providers import factory
        if config_path:
            import os
            os.environ["BLUE_MACRO_CONFIG_PATH"] = str(config_path)
        inputs = factory.portfolio_inputs()
        meta = inputs.get("meta") or {}
        result["sleeves"] = list(inputs.get("universe") or [])
        # ``cached_rows`` counts bars *written* by the live provider this run.
        # ``rows_available`` is what the price-cache source could *read* - the
        # honest measure of whether we are working from stored history.
        result["cached_rows"] = int(meta.get("cached_rows") or 0)
        result["rows_available"] = int(inputs.get("n_obs") or 0)
        result["warnings"].extend(meta.get("warnings") or [])
        result["sources"] = meta.get("sources") or []
        _log(f"provider: {len(result['sleeves'])} sleeves, "
             f"{result['rows_available']} rows available, "
             f"{result['cached_rows']} newly cached, sources={result['sources']}")

        if not result["sleeves"]:
            result.update(ok=True, stage="no_new_data",
                          warnings=result["warnings"] +
                          ["no sleeves had data (holiday, or broker down)"])
            _log("no data today (treated as a holiday)")
            write_status(result)
            return result

        # ---- 3. run the engine on those inputs --------------------------- #
        # Running the engine directly (not the whole pipeline) keeps the job
        # focused: we want the portfolio stage's book, not an order path.
        from ..portfolio.portfolio_engine import PortfolioEngine
        cfg = factory._load_config()  # noqa: SLF001 - same package by design
        engine = PortfolioEngine(cfg, log=_log)
        res = engine.run({"portfolio_inputs": inputs})
        data = res.data or {}
        result["target_weights"] = data.get("target_weights") or {}
        result["gross"] = data.get("gross")
        result["cost_bps"] = (data.get("cost") or {}).get("cost_bps")
        result["turnover"] = (data.get("cost") or {}).get("turnover")
        result["optimiser"] = data.get("optimiser")
        result["warnings"].extend(res.warnings or [])
        _log(f"engine: optimiser={data.get('optimiser')} "
             f"gross={data.get('gross')} trades="
             f"{len(data.get('rebalance_trades') or [])}")

        # ---- 4. persist the run + freeze the daily equity row ------------ #
        #
        # Two different records, deliberately kept apart:
        #   * portfolio_state  -> the ALLOCATION history (what we wanted to hold)
        #   * tracker/store_nav -> the EQUITY history (what it was actually worth)
        # Phases 2-5 (Sharpe/VaR/attribution) read the second one; the dashboard
        # drift tab reads the first. Mixing them would give us two definitions of
        # "return", which is how desks end up disagreeing with their own P&L.
        if not dry_run:
            from ..portfolio import portfolio_state
            run_file = portfolio_state.record({
                "regime": data.get("regime"),
                "optimiser": data.get("optimiser"),
                "target_weights": data.get("target_weights"),
                "actual_weights": inputs.get("current_weights") or {},
                "gross": data.get("gross"),
                "cash_weight": data.get("cash_weight"),
                "risk": data.get("risk"),
                "sizing": data.get("sizing"),
                "cost": data.get("cost"),
                "job": "daily_portfolio",
            }, ts=started)
            result["run_file"] = str(run_file) if run_file else None

            nav = _snapshot_equity(inputs, data, started, _log, result)
            if nav:
                result["nav"] = nav.get("nav")
                result["nav_file"] = nav.get("stored")
        else:
            result["warnings"].append("dry-run: nothing written")

        result.update(ok=True, stage="done")
        _log(f"done in {time.time() - started:.1f}s")

    except Exception as exc:  # noqa: BLE001 - the job must always exit cleanly
        logger.exception("daily_portfolio job failed")
        result.update(stage="failed", error=repr(exc))
        _log(f"FAILED: {exc!r}")

    result["elapsed_s"] = round(time.time() - started, 2)
    write_status(result)
    return result


def _snapshot_equity(inputs: Dict[str, Any], data: Dict[str, Any],
                     started: float, log: Any,
                     result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Freeze today's equity row from the live book (Phase 1).

    Uses the *broker's* view of the position (``inputs['current_weights']`` +
    ``inputs['capital']``) rather than replaying the order ledger, because the
    broker is the source of truth for what we actually hold after corporate
    actions, partial fills and rejects. The tracker's fill-level accounting is
    used for cost basis and P&L.

    Returns the NAV snapshot dict, or None when there is nothing to record.
    Never raises — a bookkeeping miss must not fail the job.
    """
    try:
        from ..portfolio import tracker as tracker_mod

        capital = safe_float(inputs.get("capital"))
        if not capital or capital <= 0:
            log("equity snapshot skipped: no capital from the provider")
            return None

        tracker = tracker_mod.EquityTracker(
            starting_cash=float(capital),
            charges_bps=(safe_float(inputs.get("charges_bps"))
                         if safe_float(inputs.get("charges_bps")) is not None
                         else tracker_mod.DEFAULT_CHARGES_BPS),
            log=log)

        # Book the sleeve book as if entered at the provider's marks: we are
        # valuing an EXISTING book, not replaying history, so the "fill" price
        # is the current mark and cost basis starts at the mark.
        marks: Dict[str, float] = {}
        weights = inputs.get("current_weights") or {}
        for sleeve, w in weights.items():
            wt = safe_float(w) or 0.0
            notional = wt * float(capital)
            if notional <= 0:
                continue
            marks[sleeve] = 1.0          # unit marks: notional == qty * 1.0
            tracker.on_fill(sleeve, "SLEEVE", "BUY", int(round(notional)),
                            1.0, ts=started, charges_bps=0.0)

        snap = tracker.snapshot(
            marks, ts=started,
            extra={
                "job": "daily_portfolio",
                "regime": data.get("regime"),
                "optimiser": data.get("optimiser"),
                "gross": data.get("gross"),
                "cash_weight": data.get("cash_weight"),
                "turnover": (data.get("cost") or {}).get("turnover"),
                "cost_bps": (data.get("cost") or {}).get("cost_bps"),
                "portfolio_vol": (data.get("risk") or {}).get("portfolio_vol"),
            })
        stored_to = tracker_mod.store_nav(
            snap, directory=inputs.get("nav_dir"))
        log(f"equity: nav={snap['nav']:,.0f} gross={snap['gross_exposure']:,.0f} "
            f"date={snap['date']}")
        return {**snap, "stored": str(stored_to) if stored_to else None}
    except Exception as exc:  # noqa: BLE001 - bookkeeping is never fatal
        logger.warning("equity snapshot failed: %r", exc)
        result.setdefault("warnings", []).append(f"equity snapshot failed: {exc}")
        return None


def main(argv: Optional[list] = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        description="Post-close portfolio persistence job "
                    "(fetch -> engine -> persist).")
    parser.add_argument("--force", action="store_true",
                        help="ignore the market-hours guard")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and report, but write nothing")
    parser.add_argument("--config", default=None,
                        help="path to macro_master.json (or a portfolio config)")
    parser.add_argument("--json", action="store_true",
                        help="print the result as JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    out = run(force=args.force, dry_run=args.dry_run,
              config_path=args.config)
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"[daily_portfolio] stage={out['stage']} ok={out['ok']} "
              f"sleeves={len(out['sleeves'])} rows_available="
              f"{out.get('rows_available')} newly_cached={out['cached_rows']} "
              f"optimiser={out.get('optimiser')} gross={out.get('gross')}")
        for w in out["warnings"]:
            print(f"  warn: {w}")
        if out["error"]:
            print(f"  ERROR: {out['error']}")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())