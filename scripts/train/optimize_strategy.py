"""
optimize_strategy.py - CLI entry point for the parameter optimiser.

WHAT THIS SCRIPT IS FOR
-----------------------
Pick ONE strategy, search its parameters, and get back:

    1. the parameter set that maximises out-of-sample (deflated) Sharpe,
    2. the three visuals an operator needs to trust it
       (importance -> slice -> parallel coordinates),
    3. a JSON + Markdown report on disk, and
    4. a study journal that the dashboard can re-render without re-running.

It is the only supported way to launch a study, so that every study lands in
the same place with the same provenance. A notebook that calls the engine
directly produces a result nobody can find later.

USAGE
-----
    # list what can be searched
    python scripts/train/optimize_strategy.py --list

    # the shipped synthetic fixture (known optimum - proves the search works)
    python scripts/train/optimize_strategy.py --space synthetic_demo --strategy synthetic

    # a real strategy's parameters, 300 trials
    python scripts/train/optimize_strategy.py --space ocl_keylevel --strategy mypkg:fn --trials 300

    # print the 3-step visual workflow as text (no plotly needed)
    python scripts/train/optimize_strategy.py --space synthetic_demo --strategy synthetic --workflow

SAFETY
------
Read-only with respect to capital. This script never places an order and never
writes to a live configuration file. The output is a RECOMMENDATION; adopting
it is a separate, deliberate human action.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

# --- make the package importable when run as a plain script --------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "Blue_engine"))
sys.path.insert(0, _ROOT)

from blue_engine.optimizer import __version__, config, ensure_dirs, logger  # noqa: E402
from blue_engine.optimizer.analysis import visual_workflow  # noqa: E402
from blue_engine.optimizer.engine import EngineConfig, OptimizerEngine  # noqa: E402
from blue_engine.optimizer.report import build_report, render_markdown  # noqa: E402
from blue_engine.optimizer.spaces import available_spaces, load_space  # noqa: E402
from blue_engine.optimizer.store import StudyStore, write_report  # noqa: E402
from blue_engine.optimizer.strategies import available_builtin_strategies  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="optimize_strategy.py",
        description="Search one strategy's parameters for maximum "
                    "out-of-sample Sharpe (read-only; places no orders).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true",
                   help="list available spaces and built-in strategies, then exit")
    p.add_argument("--space", default="synthetic_demo",
                   help="search space name (default: synthetic_demo)")
    p.add_argument("--strategy", default="synthetic",
                   help="strategy: built-in name, 'module:function', or dotted path")
    p.add_argument("--trials", type=int, default=200,
                   help="hard trial budget (default: 200)")
    p.add_argument("--seed", type=int, default=7, help="RNG seed (default: 7)")
    p.add_argument("--periods", type=int, default=600,
                   help="bars handed to the strategy per trial (default: 600)")
    p.add_argument("--folds", type=int, default=4,
                   help="walk-forward folds (default: 4; minimum 2)")
    p.add_argument("--cost-bps", type=float, default=config.COST_BPS_ROUND_TRIP,
                   help=f"round-trip cost in bps (default: "
                        f"{config.COST_BPS_ROUND_TRIP})")
    p.add_argument("--name", default=None,
                   help="study name (default: <space>_<strategy>)")
    p.add_argument("--top-k", type=int, default=config.DEFAULT_TOP_K,
                   help=f"parameters carried into slice/parallel steps "
                        f"(default: {config.DEFAULT_TOP_K})")
    p.add_argument("--workflow", action="store_true",
                   help="print the importance -> slice -> parallel workflow as text")
    p.add_argument("--no-save", action="store_true",
                   help="do not write the journal/report to data/optimizer/")
    p.add_argument("--independent-sampler", action="store_true",
                   help="model parameters independently (the old behaviour; "
                        "worse on coupled spaces - see audit finding C1d)")
    p.add_argument("--json", action="store_true",
                   help="print the report payload as JSON instead of a summary")

    # --- Phase 4: real-data study surface ---------------------------------
    # The plan (Temp/OPTIMIZER_HEADER_PLAN.md, section 5.3) called for
    # six new flags. They are all OPTIONAL. A study can be run with
    # only the existing flags (synthetic data) or with the new ones
    # (real cached bars from OpenAlgo). The flags are independent of
    # each other: ``--symbol`` alone is meaningless unless
    # ``--bars-from-cache`` is also set.
    p.add_argument("--symbol", default="RELIANCE",
                   help="symbol to study (default: RELIANCE; NSE cash; "
                        "Phase 4)")
    p.add_argument("--exchange", default="NSE",
                   help="exchange for --symbol (default: NSE; Phase 4)")
    p.add_argument("--interval", default="1d",
                   help="bar interval: 1m / 5m / 15m / 1h / 1d "
                        "(default: 1d; Phase 4)")
    p.add_argument("--from", dest="from_date", default=None,
                   help="start date YYYY-MM-DD (default: end - 365 days; "
                        "Phase 4)")
    p.add_argument("--to", dest="to_date", default=None,
                   help="end date YYYY-MM-DD (default: today; Phase 4)")
    p.add_argument("--data-source", default="cache",
                   choices=["cache", "synthetic"],
                   help="bars source for the study (default: cache; "
                        "'synthetic' skips the download and uses the "
                        "engine's built-in fixture; Phase 4)")
    p.add_argument("--bars-from-cache", action="store_true",
                   help="load real bars via OptimizerDataCache and pass "
                        "them to engine.search(bars=...). Without this "
                        "flag the engine uses the synthetic series. "
                        "Phase 4")
    return p


def _cmd_list() -> int:
    print("Available search spaces:")
    for name in available_spaces():
        try:
            sp = load_space(name)
            print(f"  {name:<22} {sp.n_params:>2} params  {sp.description}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:<22} UNREADABLE: {exc}")
    print()
    print("Built-in strategies:")
    for name, path in sorted(available_builtin_strategies().items()):
        print(f"  {name:<22} {path}")
    print()
    print("Any 'module:function' or 'module.function' path also works, provided")
    print("the callable matches:  fn(params, ctx) -> {'ok': bool, 'returns': [...]}")
    return 0


def _print_workflow(wf: dict) -> None:
    """The three steps, in the order they must be read."""
    imp = wf.get("step1_importance") or {}
    print()
    print("=" * 72)
    print("STEP 1  Which parameters are driving the ship?")
    print("=" * 72)
    ranking = imp.get("ranking") or []
    if not ranking:
        print("  not enough scoreable trials for an importance ranking")
    else:
        vals = imp.get("importance") or {}
        for i, name in enumerate(ranking):
            bar = "#" * max(0, int(round(vals.get(name, 0.0) * 40)))
            mark = "  <- top-k" if name in (wf.get("important_params") or []) else ""
            print(f"  {i+1:>2}. {name:<20} {vals.get(name, 0.0):>6.3f} {bar}{mark}")
        if imp.get("under_budget"):
            print(f"  WARNING: only {imp.get('n_trials_used')} scoreable trials - "
                  f"importance is provisional (want >= "
                  f"{config.MIN_TRIALS_FOR_IMPORTANCE})")

    print()
    print("=" * 72)
    print("STEP 2  Each important parameter's individual sweet spot")
    print("=" * 72)
    sl = (wf.get("step2_slice") or {}).get("slices") or []
    if not sl:
        print("  no slice data (too few trials)")
    for s in sl:
        print(f"  {s['name']}: {s.get('note', '-')}")

    print()
    print("=" * 72)
    print("STEP 3  How they weave together at peak score")
    print("=" * 72)
    pc = wf.get("step3_parallel") or {}
    lines = pc.get("lines") or []
    if not lines:
        print("  no parallel data (too few trials)")
    else:
        sr = pc.get("score_range") or {}
        print(f"  top {len(lines)} trials, score "
              f"{sr.get('cutoff', 0):.3f} .. {sr.get('best', 0):.3f}")
        print()
        names = [a["name"] for a in (pc.get("axes") or [])]
        print("    " + "".join(f"{n:>12}" for n in names))
        for row in lines[:8]:
            cells = []
            for n in names:
                v = row["params"].get(n)
                cells.append(f"{str(v):>12}")
            print(f"    " + "".join(cells) + f"   score={row['score']:.3f}")
        if len(lines) > 8:
            print(f"    ... and {len(lines) - 8} more")


def _maybe_load_bars(args) -> Optional[list]:
    """Load real bars for the study, or return ``None`` for synthetic.

    The CLI mirrors the dashboard's behaviour:
      * ``--data-source synthetic`` → ``None`` (engine uses the built-in
        synthetic series; the existing 37 tests still pass).
      * ``--bars-from-cache`` set → call
        ``OptimizerDataCache.load(exchange, symbol, interval, from, to)``
        and return the bar list. Failures degrade to ``None`` with a
        printed warning so the engine still runs (synthetic fallback).
      * Otherwise → ``None`` (no real data, synthetic by default).

    The default date range is the last 365 days (or the 30-day cap,
    whichever is smaller). The user can override with ``--from`` / ``--to``.
    """
    # Phase 4 contract: synthetic is opt-out, not opt-in.
    if args.data_source == "synthetic":
        return None
    if not args.bars_from_cache:
        return None

    # Lazy import: the data cache pulls in the broker providers, which
    # may not be importable in every dev environment.
    try:
        from blue_engine.optimizer.data_cache import OptimizerDataCache
    except Exception as exc:  # noqa: BLE001
        print(f"warning: OptimizerDataCache not importable ({exc!r}); "
              f"falling back to synthetic data",
              file=sys.stderr)
        return None

    # Default date range: last 365 days OR last 30 days (the SDK cap).
    from datetime import date, timedelta
    today = date.today()
    end = args.to_date or today.isoformat()
    if args.from_date:
        start = args.from_date
    else:
        # Cap at 30 days to match OpenAlgo's rolling window.
        start = (today - timedelta(days=30)).isoformat()

    cache = OptimizerDataCache()
    load = cache.load(exchange=args.exchange, symbol=args.symbol,
                       interval=args.interval, start=start, end=end)
    for w in load.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if not load.ok:
        print(f"warning: could not load bars for {args.symbol}/"
              f"{args.exchange}/{args.interval} in {start}..{end}: "
              f"{load.reason or 'unknown'}; falling back to synthetic",
              file=sys.stderr)
        return None
    print(f"  bars: {load.n_bars} rows ({load.first_ts} .. {load.last_ts}) "
          f"source={load.source}")
    return load.bars


def _summary(result, payload: dict) -> str:
    v = result.verdict or {}
    st = payload["study"]
    lines = []
    lines.append("")
    lines.append("=" * 72)
    lines.append(f"STUDY {st['name']}")
    lines.append("=" * 72)
    lines.append(f"  space/strategy   : {st['space']} / {st['strategy']}")
    lines.append(f"  backend          : {result.backend}")
    lines.append(f"  trials           : {st['n_ok']} scoreable / {st['n_trials']} run"
                 f" ({st['n_failed']} failed)")
    lines.append(f"  wall time        : {st['wall_seconds']}s")
    lines.append("")
    lines.append(f"  VERDICT          : {v.get('verdict')}")
    if v.get("reason"):
        lines.append(f"  reason           : {v['reason']}")
    if v.get("best_trial") is not None:
        lines.append(f"  best trial       : #{v.get('best_trial')}")
    if v.get("best_mean_sharpe") is not None:
        lines.append(f"  best mean Sharpe : {v['best_mean_sharpe']}")
    d = v.get("deflation") or {}
    if d.get("prob_real") is not None:
        lines.append(f"  deflated prob    : {d['prob_real']} "
                     f"(bar {v.get('prob_real_bar')})")
        lines.append(f"  n_obs            : {d.get('n_obs')}")
    imp = result.improvement()
    if imp is not None:
        lines.append(f"  improvement      : {imp:+.4f} vs production defaults")
    lines.append("")
    rec = payload["parameters"].get("recommended") or {}
    base = payload["parameters"].get("baseline") or {}
    if rec:
        lines.append("  RECOMMENDED PARAMETERS")
        for k, val in rec.items():
            cur = base.get(k)
            flag = "   <- changed" if val != cur else ""
            lines.append(f"    {k:<22} {str(val):>12}   (current: {cur}){flag}")
    else:
        lines.append("  RECOMMENDED PARAMETERS: none - the verdict does not "
                     "justify a change")
    lines.append("")
    lines.append("  This engine places no orders. Applying these values to the")
    lines.append("  live configuration is a separate, deliberate human action.")
    return "\n".join(lines)


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list:
        return _cmd_list()

    try:
        space = load_space(args.space)
    except Exception as exc:  # noqa: BLE001
        print(f"error: cannot load space {args.space!r}: {exc}", file=sys.stderr)
        print(f"available: {available_spaces()}", file=sys.stderr)
        return 2

    name = args.name or f"{args.space}_{args.strategy}".replace(":", "_")
    cfg = EngineConfig(name=name, n_trials=args.trials, seed=args.seed,
                       periods=args.periods, n_folds=args.folds,
                       cost_bps=args.cost_bps,
                       multivariate=not args.independent_sampler)

    print(f"optimising space={args.space!r} strategy={args.strategy!r} "
          f"trials={cfg.n_trials} seed={cfg.seed} "
          f"sampler={'joint' if cfg.multivariate else 'independent'}")

    try:
        engine = OptimizerEngine(space, args.strategy, cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        print(f"built-ins: {sorted(available_builtin_strategies())}",
              file=sys.stderr)
        return 2

    result = engine.search(bars=_maybe_load_bars(args))

    wf = visual_workflow(result.study, k=args.top_k)
    payload = build_report(result, visual=wf)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(_summary(result, payload))
        if args.workflow:
            _print_workflow(wf)

    if not args.no_save:
        try:
            paths = ensure_dirs()
            store = StudyStore(name)
            for rec in result.study.trials:
                store.append(rec)
            md = render_markdown(payload)
            out = write_report(name, payload, md)
            print()
            print(f"  journal : {store.path}")
            print(f"  report  : {out['json']}")
            print(f"  markdown: {out['md']}")
        except Exception as exc:  # noqa: BLE001
            # Persistence failing must not hide the result the user just paid for.
            print(f"warning: could not persist study: {exc}", file=sys.stderr)

    logger.info("optimize_strategy finished: %s", result.verdict.get("verdict"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())