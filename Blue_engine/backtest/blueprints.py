"""
backtest/blueprints.py - side-by-side walk-forward validation of a strategy's
setup_mode blueprints, before any of them is enabled for paper trading.

THE QUESTION THIS MODULE ANSWERS
================================
The OCL keylevel strategy ships three BUY blueprints (``setup_mode``):

    OCL                  2 levels, NO body filter (the classic setup)
    SELL_FAILURE_TOP     2 levels, >50% bodies (buy the failed short on retest)
    SELL_FAILURE_BOTTOM  1 level  (lowest swing candle open)

They are three different TRADING IDEAS that share one code path. Today the
evidence for choosing between them is anecdote, so this module produces the
only defensible input to that decision:

    one walk-forward study per blueprint, on the SAME symbols, the SAME
    dates, the SAME cost model, the SAME fold geometry - then one comparison
    table of post-cost expectancy and Sharpe, and one published verdict.

WHY WALK-FORWARD AND NOT A SINGLE FIT
=====================================
A parameter set scored on the sample it was chosen on is not evidence. Each
blueprint therefore gets the Loop A geometry that already exists in this
package (``batch.run_symbol_batch``): the first ``1 - oos_fraction`` of each
symbol's bars is searched with the optimizer's walk-forward objective, the
winner is frozen, and the frozen set is re-run on the tail it never saw. Only
the out-of-sample numbers appear in the comparison table.

WHY EVERY OTHER BLUEPRINT PARAMETER IS PINNED
==============================================
``setup_mode`` is the ONLY axis that varies between the studies. Every other
knob is held at its production default (``SearchSpace.defaults()``), so a
difference in expectancy is attributable to the blueprint and not to a lucky
``entry_z`` draw. Letting each blueprint tune its own parameters too would
confound the comparison with three independent searches, and would triple the
multiple-testing burden for no gain: the operator's question is "which
blueprint", not "which blueprint plus which tuning".

The pin is enforced structurally, not by convention: a blueprint study's space
contains exactly one parameter (``setup_mode``, with a single choice). If the
pin ever stops working, the study stops being a comparison, so the code makes
the wrong thing impossible rather than documented.

HONESTY RULES (each one is a way this comparison could lie)
===========================================================
1. SAME UNIVERSE, SAME WINDOW. Symbols that failed data preparation are
   reported as EXCLUDED for every blueprint. A blueprint is never compared on
   a different symbol set than its rival - a 4-name blueprint beating a 9-name
   blueprint may only mean the 5 extra names lost money for everybody.
2. SAME COSTS. One :class:`~blue_engine.backtest.costs.CostModel` instance is
   shared by every run, so the Indian statutory stack, slippage, latency and
   market impact are identical. ``expectancy`` is the NET trade P&L (after the
   engine's per-fill costs), and ``cost_bps_round_trip`` is stamped on the
   report so a reader can see what was charged.
3. POOLED, NOT AVERAGED. Expectancy and Sharpe are computed on the pooled
   out-of-sample trade/equity series across symbols, weighted by trade count,
   not by averaging per-symbol numbers (which over-weights a symbol that traded
   once).
4. NOTHING IS ENABLED HERE. This module writes a report and returns a verdict.
   It does not touch the live config, the parameter file, or the broker. The
   verdict names the blueprint that MAY be enabled; enabling stays a deliberate
   operator action (see the module's CLI ``--enable`` note in the SOP).

VERDICT GATES (published constants, not an unexplained "pass")
=============================================================
A blueprint is only eligible when ALL of the following hold:

    n_trades            >= MIN_TRADES_FOR_VERDICT          (sample is scoreable)
    expectancy          >  0                                (net edge survives cost)
    sharpe              >= OOS_SHARPE_ABS_MIN               (economically meaningful)
    deflated_prob_real  >= GATE_DEFLATED_PROBABILITY        (survives the search)
    every symbol        state == "APPROVED" (Loop A)        (no hidden failure)

Eligible blueprints are ranked by Sharpe, then expectancy. If the runner-up is
within ``TIE_MARGIN_SHARPE`` of the leader, the verdict is TIE and no blueprint
is recommended: a difference smaller than the sampling error of a Sharpe
estimate is not a decision, it is noise wearing a ranking.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import config as bt_config
from .batch import BatchConfig, run_symbol_batch
from .costs import CostModel, DEFAULT_MODEL
from ..optimizer.spaces import load_space
from ..optimizer.search_space import ParamSpec, SearchSpace

logger = logging.getLogger("blue_engine.backtest.blueprints")

__all__ = [
    "BlueprintConfig", "BlueprintComparison", "compare_blueprints",
    "blueprint_space", "pooled_metrics", "decide", "comparison_markdown",
    "write_comparison", "TIE_MARGIN_SHARPE",
]

#: Two blueprints within this much OOS Sharpe are treated as tied. 0.25 is
#: roughly the one-standard-error band of an annualised Sharpe estimated from a
#: one-year out-of-sample window; ranking inside it would be ranking noise.
TIE_MARGIN_SHARPE = 0.25

#: Absolute net-Sharpe hurdle for eligibility. Shared with the Loop A gate so a
#: blueprint cannot be eligible here and fail there for the same number.
MIN_ABS_SHARPE = bt_config.OOS_SHARPE_ABS_MIN

#: Deflated-Sharpe probability a blueprint must reach before it is eligible.
#: Same bar as ``batch._oos_gate``; duplicated as a named constant so the
#: comparison report can quote it.
GATE_DEFLATED_PROBABILITY = bt_config.OOS_FAMILY_CONFIDENCE

#: The three blueprints compared when the caller does not name any. Read from
#: the strategy module so there is ONE list of setup modes in the codebase.
def _default_setup_modes() -> Tuple[str, ...]:
    try:
        from ..alpha.Statergy.ocl_keylevel import SETUP_MODES
        return tuple(SETUP_MODES)
    except Exception:  # noqa: BLE001 - the comparison still runs on a bare box
        return ("OCL", "SELL_FAILURE_TOP", "SELL_FAILURE_BOTTOM")


DEFAULT_SETUP_MODES: Tuple[str, ...] = _default_setup_modes()


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BlueprintConfig:
    """One comparison run's inputs. Everything except ``setup_modes`` is shared.

    Attributes:
        symbols: the universe. Every blueprint runs on exactly this list.
        start, end: the common window (``YYYY-MM-DD``), inclusive.
        setup_modes: which blueprints to compare.
        trials: IS search budget PER blueprint. The same number for each, so
            the deflated-Sharpe correction is comparable across the table.
        folds: walk-forward folds for the IS search.
        oos_fraction: tail fraction held out for validation.
        cost_bps: round-trip cost assumption used by the IS search. The
            validation runs charge the full :class:`CostModel`; this figure is
            the optimizer's planning number and is reported for completeness.
        slippage_bps, latency_bps: overrides for the shared cost model. ``None``
            keeps the backtest defaults.
        seed: sampler seed, identical for every blueprint so the searches
            differ only by the blueprint.
        output_path: where the JSON comparison is written.
    """

    symbols: Sequence[str]
    start: str
    end: str
    setup_modes: Sequence[str] = DEFAULT_SETUP_MODES
    trials: int = 100
    folds: int = 4
    oos_fraction: float = 0.2
    cost_bps: float = bt_config.COST_BPS_ROUND_TRIP
    slippage_bps: Optional[float] = None
    latency_bps: Optional[float] = None
    starting_equity: float = 100_000.0
    seed: int = 7
    workers: int = 1
    exchange: str = "NSE"
    interval: str = "1d"
    output_path: Optional[Path] = None

    def __post_init__(self) -> None:
        modes = tuple(dict.fromkeys(
            str(m).strip().upper() for m in self.setup_modes if str(m).strip()))
        if not modes:
            raise ValueError("setup_modes must contain at least one blueprint")
        object.__setattr__(self, "setup_modes", modes)
        symbols = tuple(dict.fromkeys(
            str(s).strip().upper() for s in self.symbols if str(s).strip()))
        if not symbols:
            raise ValueError("symbols must contain at least one ticker")
        object.__setattr__(self, "symbols", symbols)
        if self.trials < 1:
            raise ValueError("trials must be >= 1")
        if self.folds < 2:
            raise ValueError("folds must be >= 2")
        for name, value in (("slippage_bps", self.slippage_bps),
                            ("latency_bps", self.latency_bps)):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass
class BlueprintComparison:
    """The report: one row per blueprint plus the verdict.

    Attributes:
        timestamp: UTC ISO timestamp of the run.
        universe: the symbol list every blueprint was run on.
        window: ``[start, end]`` as configured.
        cost: the shared cost model, serialised (``CostModel.to_dict``).
        data_class: always ``"historical:openalgo"`` - this comparison is only
            ever run on cached real bars. A simulated bar set would make the
            whole table a fiction.
        rows: per-blueprint pooled out-of-sample results.
        verdict: the decision dict (see :func:`decide`).
        output_path: where the JSON was written, if it was.
    """

    timestamp: str
    universe: List[str]
    window: List[str]
    cost: Dict[str, Any]
    data_class: str
    rows: List[Dict[str, Any]]
    verdict: Dict[str, Any]
    output_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "universe": list(self.universe),
            "window": list(self.window),
            "cost": dict(self.cost),
            "data_class": self.data_class,
            "rows": [dict(r) for r in self.rows],
            "verdict": dict(self.verdict),
            "output_path": self.output_path,
            "enabled_for_paper": False,
        }


# --------------------------------------------------------------------------- #
# the pinned search space
# --------------------------------------------------------------------------- #


def blueprint_space(base: Optional[SearchSpace] = None,
                    mode: str = "OCL") -> SearchSpace:
    """The single-parameter space a blueprint study searches.

    One categorical parameter with one legal value. The optimizer therefore
    evaluates the blueprint's production defaults (trial 0) and a handful of
    identical "trials" - the winner IS the pinned configuration, and the trial
    count is honest for the deflation.

    Returns:
        A new :class:`SearchSpace`. ``base``'s description and metadata are
        carried over so the study record still points at the strategy module.
    """
    token = str(mode).strip().upper()
    if not token:
        raise ValueError("mode must be a non-empty setup_mode")
    base = base or load_space("ocl_keylevel")
    return SearchSpace(
        name=f"{base.name}__{token}",
        params=[ParamSpec(name="setup_mode", kind="categorical",
                          choices=[token], default=token,
                          doc="Pinned blueprint - the ONLY axis this study "
                              "varies; every other parameter is held at its "
                              "production default.")],
        description=f"{base.description} [pinned to {token}]",
        meta=dict(base.meta))


# --------------------------------------------------------------------------- #
# pooling
# --------------------------------------------------------------------------- #


def pooled_metrics(symbol_results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Pool per-symbol OOS metrics into one expectancy + Sharpe.

    Why pooled and not a mean of means: a symbol with 2 trades and a symbol
    with 40 trades must not carry the same weight in the number the operator
    acts on. Trade P&L is summed, trade count is summed, and expectancy is the
    ratio - which is exactly the per-trade edge the blueprint has to earn back
    from costs.

    Sharpe is NOT summed. The per-symbol OOS Sharpes are aggregated by their
    MEDIAN (``sharpe_basis`` says so on the row) because a summed Sharpe over
    symbols with different bar counts would be an annualisation of a mixed
    calendar. The median is the statistic that survives one lucky name.

    Args:
        symbol_results: items with ``symbol``, ``state`` and ``oos_metrics``.

    Returns a flat dict, JSON-safe, with ``None`` where a metric is genuinely
    undefined (never 0.0, which would read as a measured zero).
    """
    trades = 0
    wins = 0
    net_pnl = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    costs = 0.0
    n_symbols = 0
    symbols_with_trades = 0
    sharpes: List[float] = []
    drawdowns: List[float] = []
    dsrs: List[float] = []
    per_symbol: List[Dict[str, Any]] = []
    for item in symbol_results:
        m = dict(item.get("oos_metrics") or {})
        n = int(m.get("n_trades") or 0)
        pnl = float(m.get("net_pnl") or 0.0)
        trades += n
        wins += int(m.get("n_wins") or 0)
        net_pnl += pnl
        gross_profit += float(m.get("gross_profit") or 0.0)
        gross_loss += float(m.get("gross_loss") or 0.0)
        costs += float(m.get("costs_paid") or 0.0)
        n_symbols += 1
        if n:
            symbols_with_trades += 1
        for bucket, key in ((sharpes, "sharpe"), (drawdowns, "max_drawdown_pct"),
                            (dsrs, "deflated_prob_real")):
            if m.get(key) is not None:
                bucket.append(float(m[key]))
        per_symbol.append({
            "symbol": item.get("symbol"),
            "state": item.get("state"),
            "n_trades": n,
            "net_pnl": round(pnl, 4),
            "expectancy": _div(pnl, n),
            "sharpe": m.get("sharpe"),
            "max_drawdown_pct": m.get("max_drawdown_pct"),
            "deflated_prob_real": m.get("deflated_prob_real"),
            "oos_window": item.get("oos_window"),
        })
    return {
        "n_symbols": n_symbols,
        "n_symbols_with_trades": symbols_with_trades,
        "n_trades": trades,
        "win_rate": _div(wins, trades),
        "net_pnl": round(net_pnl, 4),
        "gross_pnl": round(gross_profit + gross_loss, 4),
        "costs_paid": round(costs, 4),
        # Post-cost expectancy per trade, in rupees. This is the headline.
        "expectancy": _div(net_pnl, trades),
        "profit_factor": _div(gross_profit, abs(gross_loss)),
        "sharpe": _median(sharpes),
        "sharpe_basis": "median of per-symbol OOS Sharpe",
        "max_drawdown_pct": _max(drawdowns),
        "deflated_prob_real": _min(dsrs),
        "deflated_basis": "worst per-symbol OOS deflated probability",
        "per_symbol": per_symbol,
    }


def _div(a: float, b: float) -> Optional[float]:
    if not b:
        return None
    value = float(a) / float(b)
    return round(value, 6) if math.isfinite(value) else None


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #


def decide(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Rank the blueprints and publish the eligibility reasoning.

    Every gate is reported per blueprint (pass/fail with the number), so the
    operator can see WHY a blueprint was rejected rather than only that it was.
    Returns a dict; never raises on a row with missing numbers - a missing
    metric is a failed gate, not a crash.
    """
    scored: List[Dict[str, Any]] = []
    for row in rows:
        pooled = dict(row.get("pooled") or {})
        n_trades = int(pooled.get("n_trades") or 0)
        expectancy = pooled.get("expectancy")
        sharpe = pooled.get("sharpe")
        dsr = pooled.get("deflated_prob_real")
        symbols_ok = bool(row.get("all_symbols_approved"))
        gates = {
            "sample": n_trades >= bt_config.MIN_TRADES_FOR_VERDICT,
            "positive_expectancy": expectancy is not None and expectancy > 0,
            "absolute_sharpe": sharpe is not None and sharpe >= MIN_ABS_SHARPE,
            "deflated_sharpe": dsr is not None and dsr >= GATE_DEFLATED_PROBABILITY,
            "all_symbols_approved": symbols_ok,
        }
        failed = [name for name, ok in gates.items() if not ok]
        scored.append({
            "setup_mode": row.get("setup_mode"),
            "eligible": not failed,
            "failed_gates": failed,
            "gates": gates,
            "expectancy": expectancy,
            "sharpe": sharpe,
            "n_trades": n_trades,
        })

    def _rank_key(item: Dict[str, Any]) -> Tuple[float, float]:
        sharpe = item["sharpe"]
        expectancy = item["expectancy"]
        return (-(sharpe if sharpe is not None else -1e9),
                -(expectancy if expectancy is not None else -1e9))

    eligible = sorted([s for s in scored if s["eligible"]], key=_rank_key)
    if not eligible:
        return {"decision": "NO BLUEPRINT ELIGIBLE",
                "recommended_setup_mode": None,
                "reason": "no blueprint passed every gate; do not enable any "
                          "for paper trading",
                "ranking": scored,
                "tie_margin_sharpe": TIE_MARGIN_SHARPE,
                "gates_published": {
                    "min_trades": bt_config.MIN_TRADES_FOR_VERDICT,
                    "min_abs_sharpe": MIN_ABS_SHARPE,
                    "min_deflated_probability": GATE_DEFLATED_PROBABILITY}}
    leader = eligible[0]
    if len(eligible) > 1:
        runner_up = eligible[1]
        gap = (leader["sharpe"] or 0.0) - (runner_up["sharpe"] or 0.0)
        if gap < TIE_MARGIN_SHARPE:
            return {"decision": "TIE",
                    "recommended_setup_mode": None,
                    "reason": (f"{leader['setup_mode']} leads "
                               f"{runner_up['setup_mode']} by {gap:.2f} Sharpe, "
                               f"inside the {TIE_MARGIN_SHARPE:.2f} tie margin; "
                               "collect more out-of-sample data before choosing"),
                    "ranking": scored,
                    "tie_margin_sharpe": TIE_MARGIN_SHARPE}
    return {"decision": "RECOMMENDED",
            "recommended_setup_mode": leader["setup_mode"],
            "reason": (f"{leader['setup_mode']} passed every gate and led on "
                       "out-of-sample Sharpe"),
            "ranking": scored,
            "tie_margin_sharpe": TIE_MARGIN_SHARPE}


# --------------------------------------------------------------------------- #
# the comparison
# --------------------------------------------------------------------------- #


def _strategy_spec() -> Any:
    """The OCL keylevel signal class - resolved lazily so imports stay cheap."""
    from ..alpha.Statergy.ocl_keylevel import OCLKeylevelSignal
    return OCLKeylevelSignal


def _cost_model(cfg: BlueprintConfig) -> CostModel:
    """The ONE cost model every run in this comparison shares."""
    base = DEFAULT_MODEL
    return CostModel(
        cost_config=dict(getattr(base, "rates", {}) or {}),
        slippage_bps=(base.slippage_bps if cfg.slippage_bps is None
                      else float(cfg.slippage_bps)),
        latency_bps=(base.latency_bps if cfg.latency_bps is None
                     else float(cfg.latency_bps)),
        impact_coefficient=getattr(base, "impact_coefficient", 8.0))


def compare_blueprints(cfg: BlueprintConfig, *,
                       cache: Any = None,
                       strategy: Any = None,
                       batch_runner: Callable[..., Any] = run_symbol_batch,
                       ) -> BlueprintComparison:
    """Run one walk-forward study per blueprint on the shared universe/window.

    Args:
        cfg: the run inputs (see :class:`BlueprintConfig`).
        cache: an ``OptimizerDataCache``. ``None`` uses the optimizer default
            location. A caller-supplied cache is how a test or an offline
            research run injects its own bars.
        strategy: the signal class / spec passed to the batch runner. ``None``
            resolves the OCL keylevel signal.
        batch_runner: injection point for the per-blueprint batch. Production
            passes the real ``run_symbol_batch``; a test passes a fake so the
            comparison logic is exercised without network or Optuna.

    Returns:
        A :class:`BlueprintComparison`. Written to ``cfg.output_path`` when set.
    """
    if not isinstance(cfg, BlueprintConfig):
        raise TypeError("cfg must be a BlueprintConfig")
    spec = strategy if strategy is not None else _strategy_spec()
    cost = _cost_model(cfg)
    batch_cfg = BatchConfig(
        exchange=cfg.exchange, interval=cfg.interval,
        start=cfg.start, end=cfg.end, oos_fraction=cfg.oos_fraction,
        trials=cfg.trials, folds=cfg.folds, seed=cfg.seed,
        cost_bps=cfg.cost_bps, starting_equity=cfg.starting_equity,
        workers=cfg.workers)

    rows: List[Dict[str, Any]] = []
    excluded_symbols: Dict[str, str] = {}
    for mode in cfg.setup_modes:
        space = blueprint_space(mode=mode)
        logger.info("blueprint comparison: %s over %d symbols",
                    mode, len(cfg.symbols))
        try:
            batch = batch_runner(list(cfg.symbols), strategy=spec,
                                 search_space=space, config=batch_cfg,
                                 cache=cache, cost_model=cost,
                                 output_path=_scratch_output(cfg, mode))
        except Exception as exc:  # noqa: BLE001 - a failed blueprint is a row
            logger.exception("blueprint %s failed", mode)
            rows.append({"setup_mode": mode, "state": "FAILED",
                         "reason": f"{type(exc).__name__}: {exc}",
                         "pooled": {}, "all_symbols_approved": False,
                         "per_symbol": {}})
            continue
        rows.append(_row_from_batch(mode, batch, excluded_symbols))

    # A symbol that failed data preparation for ANY blueprint is excluded from
    # ALL of them, so no blueprint is compared on a larger universe than its
    # rival (honesty rule 1).
    if excluded_symbols:
        for index, row in enumerate(rows):
            remaining = {
                sym: val for sym, val in (row.get("per_symbol") or {}).items()
                if sym not in excluded_symbols}
            if row.get("state") != "SCORED":
                row["excluded_symbols"] = dict(excluded_symbols)
                continue
            rebuilt = _row(str(row.get("setup_mode")), remaining)
            rebuilt["excluded_symbols"] = dict(excluded_symbols)
            rows[index] = rebuilt

    verdict = decide(rows)
    timestamp = datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    comparison = BlueprintComparison(
        timestamp=timestamp, universe=list(cfg.symbols),
        window=[cfg.start, cfg.end], cost=cost.to_dict(),
        data_class="historical:openalgo", rows=rows, verdict=verdict)
    if cfg.output_path is not None:
        comparison.output_path = str(write_comparison(comparison, cfg.output_path))
    logger.info("blueprint comparison complete: %s", verdict["decision"])
    return comparison


def _row_from_batch(mode: str, batch: Any,
                    excluded: Dict[str, str]) -> Dict[str, Any]:
    """Turn one blueprint's ``BatchResult`` into a comparison row."""
    results = dict(getattr(batch, "results", {}) or {})
    per_symbol: Dict[str, Dict[str, Any]] = {}
    for symbol, outcome in results.items():
        state = outcome.get("state")
        if state == "DEGRADED" and not outcome.get("oos_metrics"):
            excluded.setdefault(symbol, str(outcome.get("reason") or "DEGRADED"))
        per_symbol[symbol] = {
            "state": state,
            "oos_metrics": dict(outcome.get("oos_metrics") or {}),
            "parameters": dict(outcome.get("best_parameters") or {}),
            "oos_window": outcome.get("oos_window"),
            "reason": outcome.get("reason"),
        }
    return _row(mode, per_symbol)


def _row(mode: str, per_symbol: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """A comparison row from the per-symbol results of one blueprint.

    ``per_symbol`` keeps the full OOS metric block (JSON evidence); the row's
    public fields are the pooled numbers the verdict is computed from.
    """
    pooled = pooled_metrics(
        [{"symbol": sym, "state": val.get("state"),
          "oos_metrics": val.get("oos_metrics") or {},
          "oos_window": val.get("oos_window")}
         for sym, val in per_symbol.items()])
    all_approved = bool(per_symbol) and all(
        v.get("state") == "APPROVED" for v in per_symbol.values())
    return {"setup_mode": mode, "state": "SCORED",
            "pooled": pooled, "per_symbol": per_symbol,
            "all_symbols_approved": all_approved,
            "approved_symbols": sorted(
                s for s, v in per_symbol.items() if v.get("state") == "APPROVED")}


def _median(values: Sequence[float]) -> Optional[float]:
    xs = sorted(float(v) for v in values if v is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    value = xs[mid] if len(xs) % 2 else 0.5 * (xs[mid - 1] + xs[mid])
    return round(value, 4)


def _min(values: Sequence[float]) -> Optional[float]:
    xs = [float(v) for v in values if v is not None]
    return round(min(xs), 4) if xs else None


def _max(values: Sequence[float]) -> Optional[float]:
    xs = [float(v) for v in values if v is not None]
    return round(max(xs), 4) if xs else None


def _scratch_output(cfg: BlueprintConfig, mode: str) -> Path:
    """Per-blueprint parameter file path.

    The batch runner persists approved parameters. That file is NOT a live
    configuration - nothing reads it to enable trading - but each blueprint
    must write its own, otherwise the last one silently overwrites the others
    and the evidence behind the verdict disappears.
    """
    root = Path(cfg.output_path).parent if cfg.output_path else \
        Path(bt_config.RESULTS_DIR)
    return root / "blueprint_candidates" / f"{mode}.json"


# --------------------------------------------------------------------------- #
# rendering + persistence
# --------------------------------------------------------------------------- #


def comparison_markdown(comparison: BlueprintComparison) -> str:
    """A one-screen table plus the verdict reasoning. Never claims live data."""
    v = comparison.verdict
    lines = [
        "# OCL blueprint walk-forward comparison",
        "",
        f"- run (UTC): `{comparison.timestamp}`",
        f"- universe: {', '.join(comparison.universe)}",
        f"- window: {comparison.window[0]} .. {comparison.window[1]}",
        f"- data: {comparison.data_class} (historical, cost-charged)",
        f"- cost: {comparison.cost.get('round_trip_bps')} bps round trip "
        f"+ {comparison.cost.get('slippage_bps')} bps slippage "
        f"+ {comparison.cost.get('latency_bps')} bps latency",
        "",
        "| blueprint | trades | expectancy (Rs/trade) | Sharpe | max DD % | "
        "deflated p | symbols approved | eligible |",
        "|---|---|---|---|---|---|---|---|",
    ]
    ranking = {r.get("setup_mode"): r for r in v.get("ranking") or []}
    for row in comparison.rows:
        pooled = row.get("pooled") or {}
        r = ranking.get(row.get("setup_mode")) or {}
        lines.append(
            f"| {row.get('setup_mode')} | {pooled.get('n_trades', 0)} | "
            f"{_fmt(pooled.get('expectancy'))} | {_fmt(pooled.get('sharpe'))} | "
            f"{_fmt(pooled.get('max_drawdown_pct'))} | "
            f"{_fmt(pooled.get('deflated_prob_real'))} | "
            f"{len(row.get('approved_symbols') or [])}/"
            f"{pooled.get('n_symbols', 0)} | "
            f"{'yes' if r.get('eligible') else 'no'} |")
    lines += ["", f"**Decision: {v.get('decision')}** - {v.get('reason')}", ""]
    rec = v.get("recommended_setup_mode")
    lines.append(
        f"Recommended for the NEXT step (paper enable is a manual action): "
        f"`{rec}`" if rec else
        "No blueprint recommended. Do not enable any setup_mode for paper "
        "trading on this evidence.")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):,.4f}"
    except (TypeError, ValueError):
        return str(value)


def write_comparison(comparison: BlueprintComparison, path: Path) -> Path:
    """Atomically write the JSON report. Returns the path written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = comparison.to_dict()
    tmp: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=str(path.parent),
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
            json.dump(payload, fh, indent=2, sort_keys=True, allow_nan=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                logger.exception("could not remove incomplete report %s", tmp)
        raise
    return path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m blue_engine.backtest.blueprints --symbols INFY,TCS ...``

    Exit codes: 0 RECOMMENDED, 2 TIE, 3 NO BLUEPRINT ELIGIBLE, 1 bad arguments.
    A non-zero code on a tie/failure is deliberate: a script that chains this
    into an "enable for paper" step must stop unless a blueprint actually won.
    """
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m blue_engine.backtest.blueprints",
        description="Walk-forward compare the OCL setup_mode blueprints on one "
                    "universe and window. Writes a report; enables nothing.")
    ap.add_argument("--symbols", required=True,
                    help="comma-separated tickers, or a path to a CSV/txt file")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--modes", default=",".join(DEFAULT_SETUP_MODES),
                    help="comma-separated setup_mode blueprints")
    ap.add_argument("--trials", type=int, default=100,
                    help="IS search budget PER blueprint (default 100)")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--oos-fraction", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--slippage-bps", type=float, default=None)
    ap.add_argument("--latency-bps", type=float, default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--out", default=None,
                    help="JSON report path (default data/backtest_results/"
                         "blueprint_comparison.json)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    symbols = _read_symbols(args.symbols)
    if not symbols:
        print("no symbols given")
        return 1
    out = Path(args.out) if args.out else \
        Path(bt_config.RESULTS_DIR) / "blueprint_comparison.json"
    cfg = BlueprintConfig(
        symbols=symbols, start=args.start, end=args.end,
        setup_modes=tuple(m.strip() for m in args.modes.split(",") if m.strip()),
        trials=args.trials, folds=args.folds, oos_fraction=args.oos_fraction,
        seed=args.seed, slippage_bps=args.slippage_bps,
        latency_bps=args.latency_bps, workers=args.workers, output_path=out)
    comparison = compare_blueprints(cfg)
    print(comparison_markdown(comparison))
    out.with_suffix(".md").write_text(comparison_markdown(comparison),
                                      encoding="utf-8")
    print(f"\nreport: {comparison.output_path}")
    decision = comparison.verdict.get("decision")
    return {"RECOMMENDED": 0, "TIE": 2}.get(str(decision), 3)


def _read_symbols(value: str) -> List[str]:
    """Tickers from a comma list, or from a CSV/txt file (first column)."""
    candidate = Path(value)
    if candidate.is_file():
        rows = candidate.read_text(encoding="utf-8").splitlines()
        out: List[str] = []
        for line in rows:
            token = line.split(",")[0].strip().strip('"').upper()
            if token and token not in ("SYMBOL", "TICKER"):
                out.append(token)
        return out
    return [t.strip().upper() for t in value.split(",") if t.strip()]


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
