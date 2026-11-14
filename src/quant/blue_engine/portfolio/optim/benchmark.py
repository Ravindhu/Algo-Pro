"""
optim/benchmark.py - walk-forward optimiser comparison, after India costs.

The question this answers
-------------------------
"A quant says HRP/ERC should beat inverse-vol. Do *my* sleeves, on *my* prices,
after *my* costs, actually show that?" Until that has a number, switching the
production optimiser is a belief, not a decision.

Method (deliberately strict)
----------------------------
1. **Walk-forward**, never a single fit. At each rebalance date ``t``:
   estimate the covariance from returns **strictly before** ``t``, solve the
   weights, then apply those weights **forward** until the next rebalance.
2. **Costs are always charged.** Every turnover between rebalances pays the
   round-trip bps from ``cost_aware`` (STT, brokerage, GST, stamp, slippage).
   A backtest that ignores Indian costs flatters high-turnover optimisers.
3. **No look-ahead.** The covariance window ends at ``t``; the realised return
   uses ``t -> t+step``. Weights are applied to the *next* period only.
4. **Same universe, same schedule, same costs** for every optimiser, so the
   comparison isolates the optimiser choice.

Outputs are the numbers a PM decides on: annualised return, vol, Sharpe, max
drawdown, turnover, total cost drag, and a net-of-cost comparison table with the
winner flagged.

This module is pure (no I/O): feed it a ``{sleeve: closes}`` panel.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import registry as optim_registry
from . import cost_aware
from ..attribution import perf
from ..risk import covariance as cov_mod
from ...core.results import mean, safe_float, stdev

logger = logging.getLogger(__name__)

__all__ = ["returns_from_closes", "run_walk_forward", "compare_optimisers",
           "DEFAULT_OPTIMISERS"]


#: The shortlist worth comparing on a sleeve universe.
DEFAULT_OPTIMISERS: List[str] = ["inverse_vol", "risk_parity", "hrp",
                                 "min_variance", "max_sharpe", "nco",
                                 "min_cvar", "black_litterman"]


def returns_from_closes(closes: Sequence[float]) -> List[float]:
    """Simple daily returns from a close series."""
    cs = [safe_float(c) for c in (closes or [])]
    out: List[float] = []
    for a, b in zip(cs, cs[1:]):
        if a is None or b is None or a <= 0:
            continue
        out.append(b / a - 1.0)
    return out


def _align(panel: Dict[str, List[float]]) -> Tuple[List[str], List[List[float]]]:
    """Align a ``{sleeve: closes}`` panel to a common length (most recent)."""
    names = [k for k, v in panel.items() if v and len(v) > 2]
    if len(names) < 2:
        return [], []
    tmin = min(len(panel[n]) for n in names)
    series = [[safe_float(x) or 0.0 for x in panel[n][-tmin:]] for n in names]
    return names, series


def run_walk_forward(panel: Dict[str, List[float]], optimiser: str, *,
                     window: int = 120, step: int = 20, start: int = 120,
                     cost_config: Optional[Dict[str, Any]] = None,
                     constraints: Optional[Dict[str, Any]] = None,
                     cov_method: str = "ledoit_wolf",
                     periods: float = 252.0,
                     cost_band_pct: float = 0.0,
                     optimiser_config: Optional[Dict[str, Any]] = None
                     ) -> Dict[str, Any]:
    """Walk-forward one optimiser over a closes panel, net of Indian costs.

    Parameters
    ----------
    panel         : ``{sleeve: daily closes}`` (oldest first).
    window        : covariance look-back in trading days (trains on the past).
    step          : rebalance frequency in trading days.
    start         : first index at which a rebalance may occur (needs ``window``).
    cost_config   : cost rates for ``cost_aware`` (defaults to India delivery).
    cost_band_pct : no-trade band (%). Sleeves drifting less than this keep the
                    *previous* weight, so no order is generated for them and no
                    cost is paid. ``0`` means rebalance every sleeve to target.

    Returns a result dict with the equity curve, per-period rows and the summary
    performance block (all net of cost).
    """
    names, series = _align(panel)
    if not names:
        return {"ok": False, "optimiser": optimiser,
                "warnings": ["panel too short to align"]}
    n_obs = len(series[0])
    if n_obs <= start + step:
        return {"ok": False, "optimiser": optimiser,
                "warnings": [f"need more than {start + step} rows, have {n_obs}"]}

    # Daily returns per sleeve (aligned to the same rows as `series`).
    rets = [[(series[i][t] / series[i][t - 1] - 1.0)
             if series[i][t - 1] > 0 else 0.0
             for t in range(1, n_obs)] for i in range(len(names))]

    equity = 1.0
    curve: List[float] = [1.0]
    rows: List[Dict[str, Any]] = []
    weights = {n: 0.0 for n in names}
    prev_weights = {n: 0.0 for n in names}
    total_cost_bps = 0.0
    total_turnover = 0.0

    t = start
    while t + step <= n_obs:
        # --- train: covariance from returns strictly BEFORE t (no leakage) ---
        lo = max(0, t - window)
        train = [rets[i][lo:t] for i in range(len(names))]
        est = cov_mod.estimate(train, method=cov_method, periods=periods)
        if not est.get("ok"):
            t += step
            continue
        cov = est["cov"]
        vols_map, _ = cov_mod.cov_to_dicts(cov, names)

        problem: Dict[str, Any] = {
            "universe": names, "cov": cov, "vols": vols_map,
            "market_weights": {n: 1.0 for n in names},
            "constraints": constraints or {"max_weight": 0.40},
        }
        sol = optim_registry.optimise(optimiser, problem,
                                      config=optimiser_config or {})
        new_w = sol.get("weights") or {}
        if not new_w:
            t += step
            continue

        # --- no-trade band: leave in-band sleeves at their PREVIOUS weight ---
        # A sleeve whose drift is smaller than the band keeps the old weight, so
        # no order is generated for it and no cost is paid. This is the lever the
        # sweep test measures: fewer, larger, cost-justified trades.
        if cost_band_pct and cost_band_pct > 0:
            banded = cost_aware.apply_band(new_w, prev_weights,
                                           band_pct=cost_band_pct,
                                           costs_config=cost_config)
            new_w = banded.get("effective_target", new_w)

        # --- cost: charge the turnover into the new target ---
        cost = cost_aware.rebalance_cost(new_w, prev_weights,
                                         config=cost_config)
        total_cost_bps += cost.get("cost_bps", 0.0)
        total_turnover += cost.get("turnover", 0.0)

        # --- test: apply the new weights to the NEXT `step` days ---
        period_ret = 0.0
        for k in range(t, min(t + step, n_obs - 1)):
            day = 0.0
            for i, name in enumerate(names):
                w = safe_float(new_w.get(name)) or 0.0
                day += w * rets[i][k]
            period_ret = (1.0 + period_ret) * (1.0 + day) - 1.0

        net_ret = period_ret - (cost.get("cost_bps", 0.0) / 10000.0)
        equity *= (1.0 + net_ret)
        curve.append(equity)
        rows.append({
            "t": t, "gross_return": round(period_ret, 8),
            "cost_bps": round(cost.get("cost_bps", 0.0), 4),
            "net_return": round(net_ret, 8),
            "turnover": round(cost.get("turnover", 0.0), 6),
            "weights": {k: round(v, 6) for k, v in new_w.items()},
        })
        prev_weights = dict(new_w)
        weights = dict(new_w)
        t += step

    if not rows:
        return {"ok": False, "optimiser": optimiser,
                "warnings": ["no valid rebalance produced weights"]}

    net_series = [r["net_return"] for r in rows]
    gross_series = [r["gross_return"] for r in rows]
    stats = perf.summarise(net_series, periods=periods / step)

    # Reduce the annualisation over `periods` from a daily basis to the
    # rebalance frequency, so Sharpe is comparable across `step`.
    return {
        "ok": True,
        "optimiser": optimiser,
        "n_periods": len(rows),
        "step_days": step,
        "window_days": window,
        "cost_band_pct": cost_band_pct,
        "universe": names,
        "rows": rows,
        "equity_curve": curve,
        "final_equity": round(equity, 6),
        "total_return": round(equity - 1.0, 6),
        "turnover_total": round(total_turnover, 6),
        "cost_bps_total": round(total_cost_bps, 4),
        "gross": perf.summarise(gross_series, periods=periods / step),
        "net": stats,
        "warnings": [],
    }


def compare_optimisers(panel: Dict[str, List[float]],
                       optimisers: Optional[Sequence[str]] = None, *,
                       window: int = 120, step: int = 20,
                       cost_config: Optional[Dict[str, Any]] = None,
                       constraints: Optional[Dict[str, Any]] = None,
                       periods: float = 252.0) -> Dict[str, Any]:
    """Run every optimiser on the same panel/costs and rank them net-of-cost.

    Returns ``{"ok", "results": {name: {...}}, "ranking": [...], "winner",
    "table": [...], "warnings"}`` where ``table`` is a list of flat rows ready
    for a Dash table / DataFrame.
    """
    names = list(optimisers or DEFAULT_OPTIMISERS)
    results: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    for name in names:
        try:
            res = run_walk_forward(panel, name, window=window, step=step,
                                   cost_config=cost_config,
                                   constraints=constraints, periods=periods)
        except Exception as exc:  # noqa: BLE001 - one bad optimiser must not stop
            warnings.append(f"{name}: failed ({exc!r})")
            continue
        if res.get("ok"):
            results[name] = res
        else:
            warnings.extend([f"{name}: {w}" for w in res.get("warnings", [])])

    if not results:
        return {"ok": False, "results": {}, "ranking": [], "table": [],
                "winner": None,
                "warnings": warnings + ["no optimiser produced a result"]}

    # Rank by net Sharpe, then by net max-drawdown (lower is better).
    def _key(item):
        net = item[1]["net"]
        sharpe = net.get("sharpe")
        mdd = net.get("max_drawdown")
        return (-(sharpe if sharpe is not None else -1e9),
                mdd if mdd is not None else 1e9)

    ranking = [name for name, _ in sorted(results.items(), key=_key)]

    table: List[Dict[str, Any]] = []
    for name in ranking:
        r = results[name]
        net, gross = r["net"], r["gross"]
        table.append({
            "optimiser": name,
            "net_return_pct": _pct(net.get("annual_return")),
            "net_vol_pct": _pct(net.get("annual_vol")),
            "net_sharpe": _r(net.get("sharpe")),
            "net_sortino": _r(net.get("sortino")),
            "net_max_dd_pct": _pct(net.get("max_drawdown")),
            "gross_sharpe": _r(gross.get("sharpe")),
            "turnover_total": r["turnover_total"],
            "cost_bps_total": r["cost_bps_total"],
            "n_periods": r["n_periods"],
        })

    return {
        "ok": True,
        "results": results,
        "ranking": ranking,
        "winner": ranking[0],
        "table": table,
        "universe": next(iter(results.values()))["universe"],
        "warnings": warnings,
    }


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 4)


def _pct(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v * 100.0, 3)# --------------------------------------------------------------------------- #
# P0 - is the winner actually better? (significance)
# --------------------------------------------------------------------------- #

def compare_optimisers_significant(panel: Dict[str, List[float]],
                                   optimisers: Optional[Sequence[str]] = None, *,
                                   baseline: str = "inverse_vol",
                                   window: int = 120, step: int = 20,
                                   n_boot: int = 2000,
                                   cost_config: Optional[Dict[str, Any]] = None,
                                   constraints: Optional[Dict[str, Any]] = None,
                                   periods: float = 252.0,
                                   seed: int = 12345) -> Dict[str, Any]:
    """``compare_optimisers`` plus a *paired bootstrap* verdict vs the baseline.

    The plain ranking is a point estimate; this adds the error bars. For every
    optimiser we run ``significance.compare_series`` against ``baseline`` on the
    **period net-return series** (paired, so the shared market days are
    preserved), and report whether the Sharpe difference is statistically
    distinguishable from zero.

    Returns the ``compare_optimisers`` payload plus:

    * ``significance`` : ``{optimiser: <compare_series result>}``
    * ``table`` rows gain ``sharpe_ci_low``, ``sharpe_ci_high``, ``p_beats_base``,
      ``significant_vs_base`` and a plain-English ``verdict``.
    * ``real_winners`` : optimisers that beat the baseline *significantly*.
      Usually empty — which is itself the most valuable finding.
    """
    from . import significance

    base = compare_optimisers(panel, optimisers, window=window, step=step,
                              cost_config=cost_config,
                              constraints=constraints, periods=periods)
    if not base.get("ok"):
        return base

    results = base["results"]
    # Paired series: per-period NET returns, in the same period order. Every
    # optimiser runs the identical schedule, so index k is the same market
    # window for all of them - that is what makes the pairing valid.
    series = {name: [row["net_return"] for row in res["rows"]]
              for name, res in results.items()}

    sig: Dict[str, Any] = {}
    base_series = series.get(baseline)
    if base_series is None:
        base["warnings"].append(
            f"baseline '{baseline}' produced no result; significance skipped")
    else:
        for name in results:
            if name == baseline:
                continue
            sig[name] = significance.compare_series(
                series[name], base_series, label_a=name, label_b=baseline,
                n_boot=n_boot, seed=seed,
                periods=periods / max(step, 1))

    # Annotate the table.
    for row in base["table"]:
        name = row["optimiser"]
        if name == baseline:
            row["p_beats_base"] = None
            row["significant_vs_base"] = None
            row["sharpe_ci_low"] = None
            row["sharpe_ci_high"] = None
            row["verdict"] = "baseline"
            continue
        s = sig.get(name) or {}
        sh = s.get("sharpe") or {}
        row["p_beats_base"] = (None if sh.get("p_a_beats_b") is None
                               else round(sh["p_a_beats_b"], 4))
        row["significant_vs_base"] = sh.get("significant")
        row["sharpe_ci_low"] = _r(sh.get("ci_low"))
        row["sharpe_ci_high"] = _r(sh.get("ci_high"))
        row["verdict"] = s.get("verdict", "no verdict")

    real_winners = [name for name, s in sig.items()
                    if (s.get("sharpe") or {}).get("significant")
                    and (s.get("sharpe") or {}).get("diff", 0) > 0]

    return {
        **base,
        "baseline": baseline,
        "significance": sig,
        "real_winners": real_winners,
        "robust_verdict": _robust_verdict(base["winner"], baseline, real_winners,
                                          sig),
    }


def _robust_verdict(winner: str, baseline: str, real_winners: List[str],
                    sig: Dict[str, Any]) -> str:
    """Plain-English one-liner a PM can put in a decision memo."""
    if real_winners:
        return (f"{', '.join(real_winners)} beat the {baseline} baseline "
                f"significantly - switching is justified.")
    if winner == baseline:
        return (f"{baseline} is both the point-estimate winner and the "
                f"baseline: nothing to change.")
    return (f"{winner} leads on point estimate but is NOT significantly better "
            f"than {baseline}. The difference is within sampling noise - "
            f"do not switch on Sharpe alone; prefer the lower-cost option.")


# --------------------------------------------------------------------------- #
# P0 - the cost/turnover sweep (the lever that actually moves P&L)
# --------------------------------------------------------------------------- #

def sweep_rebalance_params(panel: Dict[str, List[float]],
                           optimiser: str = "risk_parity", *,
                           windows: Sequence[int] = (120,),
                           steps: Sequence[int] = (5, 20, 60),
                           bands: Sequence[float] = (0.5, 1.0, 2.0, 3.0, 5.0),
                           cost_config: Optional[Dict[str, Any]] = None,
                           constraints: Optional[Dict[str, Any]] = None,
                           periods: float = 252.0
                           ) -> Dict[str, Any]:
    """Sweep rebalance frequency x no-trade band, net of India costs.

    This is the P0 finding that mattered most in the first real run: the
    *optimiser* choice was worth ~0.04 Sharpe, while the *cost* spread between
    rebalance policies was ~310 bps. So we sweep the cheap lever.

    ``bands`` is the no-trade band in percent (sleeves drifting less than the
    band are left alone, so no order is generated). It is applied by passing
    ``rebalance_band_pct`` through ``cost_aware.apply_band`` inside the
    walk-forward loop — i.e. the band changes *which* trades happen, not just
    how they are accounted.

    Returns ``{"ok", "grid", "best", "table", "warnings"}`` where ``table`` is a
    flat list ready for Plotly (``step`` x ``band`` -> metrics).
    """
    table: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for step in steps:
        for band in bands:
            res = run_walk_forward(
                panel, optimiser, window=(windows[0] if windows else 120),
                step=step, cost_config=cost_config,
                constraints=constraints, periods=periods,
                cost_band_pct=band)
            if not res.get("ok"):
                warnings.extend([f"step={step} band={band}: {w}"
                                 for w in res.get("warnings", [])])
                continue
            net = res["net"]
            table.append({
                "step": step,
                "band_pct": band,
                "rebalances_per_year": round(periods / max(step, 1), 2),
                "net_return_pct": _pct(net.get("annual_return")),
                "net_vol_pct": _pct(net.get("annual_vol")),
                "net_sharpe": _r(net.get("sharpe")),
                "net_max_dd_pct": _pct(net.get("max_drawdown")),
                "turnover": res["turnover_total"],
                "cost_bps": res["cost_bps_total"],
            })
    if not table:
        return {"ok": False, "grid": {}, "best": None, "table": [],
                "warnings": warnings + ["no sweep point produced a result"]}

    def _key(r):
        s = r["net_sharpe"]
        return (-(s if s is not None else -1e9), r["cost_bps"])

    table.sort(key=_key)
    grid = {(r["step"], r["band_pct"]): r for r in table}
    best = table[0]
    # Cost-lever headline: how much Sharpe per 100bps of cost.
    lo = min(table, key=lambda r: r["cost_bps"])
    hi = max(table, key=lambda r: r["cost_bps"])
    spread_bps = hi["cost_bps"] - lo["cost_bps"]
    spread_sharpe = ((hi["net_sharpe"] or 0) - (lo["net_sharpe"] or 0))
    return {
        "ok": True,
        "grid": grid,
        "best": best,
        "table": table,
        "swept": {"windows": list(windows), "steps": list(steps),
                  "bands": list(bands), "optimiser": optimiser},
        "cost_range_bps": round(spread_bps, 2),
        "sharpe_range": round(spread_sharpe, 4),
        "cost_per_sharpe_bps": (round(abs(spread_bps / spread_sharpe), 1)
                                if abs(spread_sharpe) > 1e-9 else None),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# P0 - universe robustness (does the winner survive a different sleeve set?)
# --------------------------------------------------------------------------- #

def universe_robustness(panel: Dict[str, List[float]],
                        optimisers: Optional[Sequence[str]] = None, *,
                        window: int = 120, step: int = 20,
                        max_subsets: int = 12, min_sleeves: int = 3,
                        cost_config: Optional[Dict[str, Any]] = None,
                        seed: int = 7, periods: float = 252.0) -> Dict[str, Any]:
    """Re-run the comparison on many sleeve subsets and count how often each
    optimiser wins.

    Found the hard way: HRP ranked 4th of 8 on 5 sleeves and 1st of 8 on 4
    sleeves. A winner that changes with the universe is a *fragile* winner. This
    function quantifies that fragility — if the top optimiser wins on only 3 of
    12 subsets, it has not earned a production switch.

    Returns ``{"ok", "win_counts", "win_share", "stable_winner", "n_subsets",
    "table", "warnings"}``.
    """
    import itertools
    from . import significance

    names = sorted(panel.keys())
    if len(names) < min_sleeves:
        return {"ok": False, "win_counts": {}, "win_share": {},
                "stable_winner": None, "n_subsets": 0, "table": [],
                "warnings": [f"need >= {min_sleeves} sleeves, have {len(names)}"]}

    subsets: List[tuple] = []
    # Always include the full universe first (the baseline answer), then a spread
    # of smaller ones. Leave-one-out subsets are the most informative small case:
    # "does the winner hold if I drop the sleeve it leans on most?"
    subsets.append(tuple(names))
    for drop in itertools.combinations(names, len(names) - 1):
        if len(names) - 1 >= min_sleeves and drop not in subsets:
            subsets.append(drop)
    for k in range(len(names) - 2, min_sleeves - 1, -1):
        for combo in itertools.combinations(names, k):
            if combo not in subsets:
                subsets.append(combo)
            if len(subsets) >= max_subsets:
                break
        if len(subsets) >= max_subsets:
            break
    subsets = subsets[:max_subsets]

    win_counts: Dict[str, int] = {}
    table: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for combo in subsets:
        sub_panel = {k: panel[k] for k in combo}
        res = compare_optimisers_significant(
            sub_panel, optimisers, window=window, step=step, n_boot=200,
            cost_config=cost_config, seed=seed, periods=periods)
        if not res.get("ok"):
            warnings.extend([f"{combo}: {w}" for w in res.get("warnings", [])])
            continue
        winner = res["winner"]
        win_counts[winner] = win_counts.get(winner, 0) + 1
        table.append({
            "sleeves": len(combo),
            "subset": ",".join(combo),
            "winner": winner,
            "winner_sharpe": next((r["net_sharpe"] for r in res["table"]
                                   if r["optimiser"] == winner), None),
            "significant": bool(res.get("real_winners")),
        })

    n = len(table)
    if n == 0:
        return {"ok": False, "win_counts": {}, "win_share": {},
                "stable_winner": None, "n_subsets": 0, "table": [],
                "warnings": warnings + ["no subset produced a result"]}
    win_share = {k: round(v / n, 4) for k, v in win_counts.items()}
    stable = max(win_counts, key=win_counts.get)
    return {
        "ok": True,
        "win_counts": win_counts,
        "win_share": win_share,
        "stable_winner": stable,
        "stable_share": win_share.get(stable),
        "n_subsets": n,
        "table": table,
        "robust": win_share.get(stable, 0) >= 0.5,
        "warnings": warnings,
    }


def _sleeve_exclusions(universe: Sequence[str],
                       sleeve: str) -> Optional[set]:
    """Names in ``universe`` that belong to sleeve ``sleeve`` (for integrity)."""
    up = str(sleeve or "").upper()
    if not up:
        return None
    hits = {u for u in universe if up in u.upper()}
    return hits or None


# --------------------------------------------------------------------------- #
# P0 - data integrity (price vs total-return, gaps, outliers)
# --------------------------------------------------------------------------- #

def check_price_integrity(panel: Dict[str, List[float]], *,
                          periods: float = 252.0,
                          gap_threshold_pct: float = 25.0,
                          stale_run: int = 5) -> Dict[str, Any]:
    """Sanity-check a closes panel before trusting any backtest on it.

    Catches the three ways a price panel silently lies to you:

    1. **Stale runs** - N identical consecutive closes (a feed that stopped
       updating, or a holiday filled forward). Inflates Sharpe by killing vol.
    2. **Extreme single-day moves** - a >``gap_threshold_pct`` jump usually means
       a split/bonus adjustment, not a real move. Left uncorrected it manufactures
       alpha.
    3. **Short history / level drift** - sleeves of very different length or
       magnitude, which breaks correlation estimates.

    Also reports the **price-vs-total-return caveat**: we cannot detect dividend
    reinvestment from closes alone, so the report states the *assumption* and its
    likely direction of bias rather than silently ignoring it.

    Returns ``{"ok", "sleeves": {name: {...}}, "warnings", "notes"}``.
    """
    out: Dict[str, Any] = {}
    warnings: List[str] = []
    for name, closes in (panel or {}).items():
        cs = [safe_float(c) for c in (closes or [])]
        cs = [c for c in cs if c is not None and c > 0]
        if len(cs) < 2:
            out[name] = {"n": len(cs), "ok": False}
            warnings.append(f"{name}: too few usable closes ({len(cs)})")
            continue
        rets = [b / a - 1.0 for a, b in zip(cs, cs[1:]) if a > 0]
        max_jump = max((abs(r) for r in rets), default=0.0)
        jumps = [i for i, r in enumerate(rets)
                 if abs(r) * 100.0 > gap_threshold_pct]
        # Stale runs: longest stretch of identical consecutive closes.
        longest_stale = 1
        run = 1
        for a, b in zip(cs, cs[1:]):
            run = run + 1 if a == b else 1
            longest_stale = max(longest_stale, run)
        daily_vol = stdev(rets)
        out[name] = {
            "n": len(cs),
            "first": round(cs[0], 4),
            "last": round(cs[-1], 4),
            "total_return_pct": round((cs[-1] / cs[0] - 1.0) * 100.0, 2),
            "annual_vol_pct": (round((daily_vol or 0.0) * math.sqrt(periods)
                                     * 100.0, 2)),
            "max_daily_move_pct": round(max_jump * 100.0, 2),
            "n_extreme_moves": len(jumps),
            "longest_stale_run": longest_stale,
            "ok": True,
        }
        if longest_stale >= stale_run:
            warnings.append(
                f"{name}: {longest_stale} identical consecutive closes - "
                f"suspected stale/forward-filled data (understates volatility)")
        if jumps:
            warnings.append(
                f"{name}: {len(jumps)} daily move(s) beyond "
                f"{gap_threshold_pct}% - check for unadjusted splits/bonuses")

    # Cross-sleeve length consistency.
    lens = [v["n"] for v in out.values() if v.get("ok")]
    if lens and (max(lens) - min(lens)) > max(20, 0.25 * max(lens)):
        warnings.append(
            f"sleeve histories differ a lot ({min(lens)}..{max(lens)} rows); "
            f"the aligned panel will truncate to the shortest")

    notes = [
        "Price indices (e.g. Yahoo ^CNXIT) EXCLUDE dividends; a total-return "
        "index INCLUDES them. If your broker serves TR and Yahoo serves price, "
        "the backtest understates the TR sleeve's return by roughly the dividend "
        "yield (~1-2%/yr for Indian sector indices).",
        "This check cannot detect that mismatch from closes alone - verify by "
        "comparing a known window against your broker's own history.",
    ]
    return {"ok": not warnings, "sleeves": out, "warnings": warnings,
            "notes": notes}


__all__ = [
    "returns_from_closes", "run_walk_forward", "compare_optimisers",
    "compare_optimisers_significant", "sweep_rebalance_params",
    "universe_robustness", "check_price_integrity", "DEFAULT_OPTIMISERS",
]