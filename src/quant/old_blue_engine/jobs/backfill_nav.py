"""
jobs/backfill_nav.py — reconstruct an equity curve from stored daily prices.

Why this exists
---------------
Phases 1–6 are built and tested, but ``data/portfolio_nav/`` holds almost no
history, so every analytics panel is empty. Rather than wait weeks for the daily
job to accumulate rows, this tool walks the **price cache** (Phase-1's dataset,
already populated for ~11 years of NSE sleeves) and rebuilds a NAV path using
the *actual* portfolio engine at each rebalance date.

What it produces
----------------
For each rebalance date ``t``:

1. estimate the covariance from returns **strictly before** ``t`` (walk-forward —
   no look-ahead),
2. run the real optimiser (+ sizing, + cost model) onmate,
3. hold those weights until the next rebalance, marking the book to market and
   charging the India cost model on turnover,
4. write the end-of-day NAV row that Phase 2–6 read.

So the curve is not a simulation of a *different* strategy — it is a faithful
replay of the allocation logic the desk runs, on the desk's own stored prices.

What it is NOT
--------------
* **Not a live result.** It is a *historical reconstruction*: real prices, real
  engine, but no real fills, no impact model, no borrow costs on shorts.
* **Not a forward test.** A backfilled curve will always look cleaner than live
  trading. Treat it as the floor of what the panels need, not as evidence of
  performance.

Every row it writes is tagged ``"source": "backfill"`` so the dashboard and any
later analysis can tell reconstructed history from live history. That distinction
matters: a panel that silently blends the two is a panel that lies.

Usage
-----
    # default: all sleeves in the price cache, monthly rebalance
    python -m blue_engine.jobs.backfill_nav

    # explicit window and cadence
    python -m blue_engine.jobs.backfill_nav --start 2024-01-01 --step 20

    # show what would happen without writing
    python -m blue_engine.jobs.backfill_nav --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = ["run", "main"]


def _load_panels(cache_db: Optional[str], sleeves: Optional[List[str]]
                 ) -> Dict[str, List[Tuple[str, float]]]:
    """Read ``{sleeve: [(date, close), ...]}`` from the price cache.

    Returns dated series (not bare closes) because the equity curve needs real
    DATES — a NAV row without a date cannot be plotted or ordered, and the
    month-file layout depends on it.
    """
    from ..portfolio.providers import price_cache as pc_mod

    cache = pc_mod.PriceCache(cache_db)
    series: Dict[str, List[Tuple[str, float]]] = {}
    for row in cache.summary():
        sym = row.get("symbol")
        exch = row.get("exchange")
        if sleeves and sym not in sleeves:
            continue
        rows = cache.rows(sym, exch, row.get("interval") or "D")
        dated = [(str(r["ts"])[:10], safe_float(r["close"]))
                 for r in rows if safe_float(r.get("close")) is not None]
        if len(dated) >= 40:
            series[sym] = dated
    return series


def _align(series: Dict[str, List[Tuple[str, float]]], start: Optional[str],
           end: Optional[str]) -> Tuple[List[str], List[str], List[List[float]]]:
    """Intersect every sleeve's dates so the panel is rectangular.

    The intersection (not the union) is deliberate: a sleeve missing a date
    would otherwise need a fill-forward, which fabricates a zero-return day and
    understates volatility — exactly the stale-data trap the policy layer halts
    on. Dropping the date is the honest choice.
    """
    names = sorted(series)
    if not names:
        return [], [], []
    maps = [{d: c for d, c in series[n]} for n in names]
    common = set(maps[0])
    for m in maps[1:]:
        common &= set(m)
    dates = sorted(d for d in common
                   if (not start or d >= start) and (not end or d <= end))
    if not dates:
        return [], [], []
    prices = [[float(m[d]) for d in dates] for m in maps]
    return names, dates, prices


def run(*, dry_run: bool = False, start: Optional[str] = None,
        end: Optional[str] = None, step: int = 20, window: int = 120,
        capital: float = 1_000_000.0, optimiser: Optional[str] = None,
        sleeves: Optional[List[str]] = None,
        cache_db: Optional[str] = None, nav_dir: Optional[str] = None,
        log: Optional[Any] = None) -> Dict[str, Any]:
    """Reconstruct the equity curve into ``data/portfolio_nav/``.

    Walk-forward, cost-aware, and honest about its own provenance. Never raises —
    a failure is reported in the returned dict.
    """

    def _log(msg: str) -> None:
        if callable(log):
            log(msg)
        else:
            logger.info("%s", msg)

    started = time.time()
    out: Dict[str, Any] = {
        "ok": False, "stage": "started", "dry_run": bool(dry_run),
        "rows_written": 0, "rebalances": 0, "sleeves": [], "warnings": [],
        "error": None,
    }
    try:
        from ..portfolio import tracker as tr_mod
        from ..portfolio import optim as optim_pkg
        from ..portfolio.providers import universe as uni_mod
        # Imported ONCE, outside the walk-forward loop. Repeatedly importing
        # inside the loop is not just wasteful — on a 3,000-date backfill it
        # dominates the runtime and makes the job look hung.
        from ..portfolio.risk import covariance as cov_mod

        series = _load_panels(cache_db, sleeves)
        if len(series) < 2:
            out["warnings"].append(
                f"price cache holds {len(series)} usable sleeve(s); need >= 2. "
                f"Backfill prices first (yfinance_provider.backfill_cache).")
            out["stage"] = "no_data"
            return out
        names, dates, prices = _align(series, start, end)
        out["sleeves"] = names
        if len(dates) < window + step + 2:
            out["warnings"].append(
                f"aligned window has {len(dates)} dates; need > "
                f"{window + step + 2} for a walk-forward")
            out["stage"] = "too_short"
            return out

        # Daily returns per sleeve, aligned to `dates`.
        rets: List[List[float]] = []
        for i in range(len(names)):
            row = []
            for t in range(1, len(dates)):
                a, b = prices[i][t - 1], prices[i][t]
                row.append((b / a - 1.0) if a > 0 else 0.0)
            rets.append(row)

        # The optimiser choice is explicit here, not regime-driven: the backfill
        # replays ONE construction method across all history so the resulting
        # curve is comparable to itself. Regime switching would confound the
        # curve with the regime model's own behaviour.
        chosen = optimiser or "risk_parity"
        constraints = {"max_weight": 0.30, "max_gross": 1.0}

        equity = float(capital)
        weights = {n: 0.0 for n in names}
        prev_row: Optional[Dict[str, Any]] = None

        # Write the INCEPTION row before the first rebalance. Without it the
        # curve starts at the first rebalance date with a NAV that already
        # reflects the warm-up window, so the first row looks like a baseline
        # when it is not: every metric (total return, drawdown, vol) is then
        # measured from an invisible mid-flight point. A curve must start where
        # the capital was committed.
        #
        # NOTE the date is ``dates[window]``, NOT ``dates[0]``. ``dates[0]`` is
        # the start of the TRAINING window — no capital is at risk during it
        # (the optimiser is being warmed up), so writing it as an inception row
        # injects a fake 211-day gap into the series. That distorted the
        # inferred spacing to 10.25 obs/year instead of ~12, which then mis-scaled
        # every annualised metric.
        first_rebalance = min(window, len(dates) - 1)
        inception = {
            "ts": datetime.strptime(dates[first_rebalance], "%Y-%m-%d").replace(
                hour=16, tzinfo=tr_mod.IST).timestamp(),
            "date": dates[first_rebalance], "nav": round(float(capital), 2),
            "cash": round(float(capital), 2), "gross_exposure": 0.0,
            "net_exposure": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
            "total_fees": 0.0, "total_pnl": 0.0,
            "starting_cash": round(float(capital), 2),
            "return_since_start": 0.0, "n_positions": 0, "positions": [],
            "source": "backfill", "optimiser": chosen,
            "window": window, "step_days": step,
            "inception": True,
        }
        if not dry_run:
            tr_mod.store_nav(
                inception, directory=Path(nav_dir) if nav_dir else None)
        out["rows_written"] += 1
        prev_row = inception

        t = window
        while t < len(dates):
            # --- train on returns STRICTLY before t (no look-ahead) -------- #
            lo = max(0, t - window)
            train = [rets[i][lo:t] for i in range(len(names))]
            est = cov_mod.estimate(train, method="ledoit_wolf")
            if not est.get("ok"):
                t += step
                continue
            vols_map, _corr = cov_mod.cov_to_dicts(est["cov"], names)
            problem = {"universe": names, "cov": est["cov"], "vols": vols_map,
                       "market_weights": {n: 1.0 for n in names},
                       "constraints": constraints}
            sol = optim_pkg.registry.optimise(chosen, problem)
            new_w = sol.get("weights") or {}
            if not new_w:
                t += step
                continue
            out["rebalances"] += 1

            # --- cost on the turnover into the new target ------------------ #
            from ..portfolio.optim import cost_aware
            cost = cost_aware.rebalance_cost(new_w, weights)
            equity *= (1.0 - cost.get("cost_bps", 0.0) / 10000.0)
            weights = dict(new_w)

            # --- hold until the next rebalance, marking to market ---------- #
            nxt = min(t + step, len(dates) - 1)
            for k in range(t, nxt):
                day = sum((safe_float(new_w.get(names[i])) or 0.0) * rets[i][k]
                          for i in range(len(names)))
                equity *= (1.0 + day)

            # --- write the NAV row for this date --------------------------- #
            # NOTE `nxt` is clamped below, so the guard `if nxt <= t: break`
            # further down is load-bearing: without it, once t reaches the last
            # index, nxt == t, the mark-to-market loop is empty, t never
            # advances, and the job spins forever instead of finishing.
            date = dates[nxt]
            ts = datetime.strptime(date, "%Y-%m-%d").replace(
                hour=16, tzinfo=tr_mod.IST).timestamp()
            row = {
                "ts": ts, "date": date, "nav": round(equity, 2),
                "cash": 0.0, "gross_exposure": 0.0, "net_exposure": 0.0,
                "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                "total_fees": round(cost.get("cost_amount") or 0.0, 2),
                "total_pnl": round(equity - capital, 2),
                "starting_cash": round(capital, 2),
                "return_since_start": round(equity / capital - 1.0, 6),
                "n_positions": sum(1 for v in new_w.values() if v),
                "positions": [{"symbol": k, "weight": round(v, 6)}
                              for k, v in new_w.items() if v],
                # Provenance: reconstructed history, NOT live trading.
                "source": "backfill", "optimiser": chosen,
                "window": window, "step_days": step,
                "turnover": cost.get("turnover"),
                "cost_bps": cost.get("cost_bps"),
            }
            if not dry_run:
                tr_mod.store_nav(
                    row, directory=Path(nav_dir) if nav_dir else None)
            out["rows_written"] += 1
            prev_row = row
            if nxt <= t:            # see the NOTE above: guarantees progress
                break
            t = nxt

        out["ok"] = out["rows_written"] > 0
        out["stage"] = "done" if out["ok"] else "no_rows"
        out["final_nav"] = None if not prev_row else prev_row["nav"]
        out["total_return"] = None if not prev_row else prev_row["return_since_start"]
        _log(f"backfill: {out['rows_written']} NAV rows, "
             f"{out['rebalances']} rebalances, "
             f"final NAV {out['final_nav']}")
    except Exception as exc:  # noqa: BLE001 - a job must exit cleanly
        logger.exception("backfill_nav failed")
        out["stage"] = "failed"
        out["error"] = repr(exc)
    out["elapsed_s"] = round(time.time() - started, 2)
    return out


def main(argv: Optional[list] = None) -> int:
    """CLI entry point."""
    p = argparse.ArgumentParser(
        description="Reconstruct the portfolio equity curve from cached prices.")
    p.add_argument("--start", default=None, help="YYYY-MM-DD (default: earliest)")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (default: latest)")
    p.add_argument("--step", type=int, default=20,
                   help="rebalance every N trading days (default 20)")
    p.add_argument("--window", type=int, default=120,
                   help="covariance look-back in trading days (default 120)")
    p.add_argument("--capital", type=float, default=1_000_000.0)
    p.add_argument("--optimiser", default=None,
                   help="optimiser name (default: risk_parity)")
    p.add_argument("--sleeves", default=None,
                   help="comma-separated sleeve names (default: all in cache)")
    p.add_argument("--cache-db", default=None)
    p.add_argument("--nav-dir", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cv = ([s.strip() for s in args.sleeves.split(",") if s.strip()]
          if args.sleeves else None)
    res = run(dry_run=args.dry_run, start=args.start, end=args.end,
              step=args.step, window=args.window, capital=args.capital,
              optimiser=args.optimiser, sleeves=cv, cache_db=args.cache_db,
              nav_dir=args.nav_dir)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(f"[backfill_nav] stage={res['stage']} ok={res['ok']} "
              f"rows={res['rows_written']} rebalances={res['rebalances']} "
              f"final_nav={res.get('final_nav')}")
        for w in res["warnings"]:
            print(f"  warn: {w}")
        if res["error"]:
            print(f"  ERROR: {res['error']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())