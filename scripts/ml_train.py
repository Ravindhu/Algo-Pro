"""ml_train.py - run an offline ML training pass and report the honest verdict.

WHAT THIS IS
------------
The one supported way to train a model in this system. Everything else
(notebooks, ad-hoc `python -c` snippets) duplicates the orchestrator's ordering
and risks fitting before auditing. This script only *invokes* the orchestrator
in `blue_engine.ml.training`, which owns the enforced order:

    fetch -> audit -> features -> labels -> split -> fit -> score -> register

WHY IT PRINTS A VERDICT, NOT A SCORE
------------------------------------
The single most dangerous output of a training script is a number that looks
good. This script's job is to say, in one line, whether the model should be
trusted near capital — and to say "no edge" loudly when there is none. A run
that finds nothing is a SUCCESSFUL run: it cost an afternoon instead of money.

Read `docs/ML_SKILL_DIAGNOSIS.md` before acting on a negative verdict. A
negative skill is usually not a bug — it frequently means the feature set has
no information at that horizon, which is a finding worth having.

USAGE
-----
    cd C:\\Users\\KanagalakshmiM\\my-trading-system
    .\\openalgo\\.venv\\Scripts\\python.exe Scripts\\ml_train.py
    .\\openalgo\\.venv\\Scripts\\python.exe Scripts\\ml_train.py --symbol NIFTY --horizon 5
    .\\openalgo\\.venv\\Scripts\\python.exe Scripts\\ml_train.py --candidates ridge,gbm --json
    .\\openalgo\\.venv\\Scripts\\python.exe Scripts\\ml_train.py --no-register   # dry run

EXIT CODES
----------
    0  the run completed (whether or not it found an edge — see --require-edge)
    1  the run was blocked (no data, failed gate) — nothing was trained
    2  bad usage
    3  --require-edge was passed and no candidate cleared the bar

CREDENTIALS
-----------
No credential is read, printed or stored here. The registry's signing key comes
from the BLUE_ML_SIGNING_KEY environment variable only; when it is unset the
script refuses to register rather than writing an unsigned artifact.

There is NO scheduling, NO broker call and NO order path in this file.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="ml_train.py",
        description="Train and evaluate an offline ML model (walk-forward, "
                    "leak-audited, cost-gated). Read-only w.r.t. trading.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n"
               "  python Scripts\\ml_train.py --symbol NIFTY --horizon 5\n",
    )
    p.add_argument("--symbol", default="NIFTY",
                   help="instrument to train on (default: NIFTY)")
    p.add_argument("--sources", default="price_cache",
                   help="comma-separated data sources, in priority order "
                        "(default: price_cache)")
    p.add_argument("--candidates", default=None,
                   help="comma-separated model families; default is the "
                        "built-in search set (ridge, gbm, rf, ensemble)")
    p.add_argument("--horizon", type=int, default=5,
                   help="label horizon in bars; also the purge length "
                        "(default: 5)")
    p.add_argument("--target", default="binary",
                   choices=["binary", "triple_barrier", "triple_barrier_cost",
                            "vol_ratio"],
                   help="what to predict (default: binary = sign of the forward "
                        "return). 'vol_ratio' predicts whether forward "
                        "realised volatility exceeds its trailing norm, which "
                        "is materially more forecastable on index data — see "
                        "docs/ML_SKILL_DIAGNOSIS.md. 'triple_barrier_cost' nets "
                        "the barriers of the round-trip cost.")
    p.add_argument("--cross-section", default=None,
                   help="comma-separated instruments to train ACROSS, instead "
                        "of on one time series. The label becomes each "
                        "instrument's rank against its peers on the same date, "
                        "which cancels the market component. Needs loosely-"
                        "correlated instruments to be meaningful.")
    p.add_argument("--splits", type=int, default=5,
                   help="number of walk-forward folds (default: 5)")
    p.add_argument("--min-train", type=int, default=250,
                   help="minimum training rows for the first fold "
                        "(default: 250, about one trading year)")
    p.add_argument("--trials", type=int, default=None,
                   help="HONEST count of variants searched, for the deflated "
                        "Sharpe correction. Under-reporting defeats the "
                        "correction. Default = number of candidates.")
    p.add_argument("--no-register", action="store_true",
                   help="evaluate but do not write an artifact to the registry")
    p.add_argument("--require-edge", action="store_true",
                   help="exit 3 unless at least one candidate clears its gates")
    p.add_argument("--json", action="store_true",
                   help="emit the full run result as JSON on stdout")
    p.add_argument("--quiet", action="store_true",
                   help="suppress the human-readable report (with --json)")
    return p.parse_args(argv)


def _split_csv(value):
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _fmt(value, digits=4):
    """Format a metric, rendering None as '-' (never as 0.0)."""
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _print_report(out, symbol):
    """Human-readable report. Deliberately leads with the verdict."""
    print()
    print("=" * 78)
    print(f"ML TRAINING RESULT - {symbol}")
    print("=" * 78)

    if not out.get("ok"):
        print("STATUS   : BLOCKED - no model was trained")
        print(f"REASON   : {out.get('error', 'unknown')}")
        warns = out.get("warnings") or []
        for w in warns[:10]:
            sev = str(w.get("severity", "")).upper()
            print(f"  [{sev}] {w.get('stage', '?')}/{w.get('source', '?')}: "
                  f"{w.get('reason', '')} - {w.get('detail', '')}")
        print()
        print("Nothing was registered. Fix the cause above and re-run.")
        return

    m = out.get("manifest") or {}
    reports = m.get("reports") or {}

    print(f"Run ID   : {m.get('run_id')}")
    print(f"Rows     : {m.get('n_rows')}   Features: "
          f"{len(m.get('feature_names') or [])}   Horizon: {m.get('horizon')}d   "
          f"Folds: {m.get('n_splits')}")
    print(f"Sources  : {', '.join(m.get('sources') or [])}")
    print(f"Commit   : {m.get('git_commit')}   ML version: {m.get('ml_version')}")
    labels = m.get("labels") or {}
    print(f"Label    : kind={labels.get('kind')} base_rate="
          f"{_fmt(labels.get('balance', {}).get('base_rate') if isinstance(labels.get('balance'), dict) else None)}")
    print()
    print(f"{'CANDIDATE':<12} {'FAMILY':<14} {'SKILL (mean+/-std)':<22} "
          f"{'AUC':<8} {'DEFL p':<8} {'NET bps':<9} VERDICT")
    print("-" * 78)
    for name in sorted(reports):
        r = reports[name]
        sk = (r.get("summary") or {}).get("skill") or {}
        au = (r.get("summary") or {}).get("auc") or {}
        df = r.get("deflation") or {}
        cc = r.get("cost_check") or {}
        fam = (r.get("spec") or {}).get("family", "-")
        print(f"{name:<12} {fam:<14} "
              f"{(_fmt(sk.get('mean')) + ' +/- ' + _fmt(sk.get('std'))):<22} "
              f"{_fmt(au.get('mean')):<8} {_fmt(df.get('prob_real')):<8} "
              f"{_fmt(cc.get('net_bps'), 2):<9} {r.get('verdict', '-')}")

    saved = out.get("saved") or {}
    print()
    if saved:
        print(f"ARTIFACT : {saved.get('address', '')[:16]}  "
              f"status={saved.get('status')}")
        print("           Registered as DRAFT. Promotion to 'validated' is a")
        print("           deliberate, signed action - it is NOT a side effect of")
        print("           training, and live code will refuse a draft artifact.")
    else:
        print("ARTIFACT : not registered (--no-register)")

    # The bottom line, stated the same way the walk-forward runner states it.
    print()
    print("BOTTOM LINE")
    print("-" * 78)
    any_edge = False
    for name in sorted(reports):
        r = reports[name]
        v = r.get("verdict", "")
        print(f"  {name}: {v}")
        if v.startswith("ESTABLISHED"):
            any_edge = True
        elif v.startswith("EDGE, but UNVERIFIED"):
            # A real edge whose deflation/cost checks could not run is NOT a
            # pass, but it is not a failure either. --require-edge deliberately
            # does not accept it: an unverified edge must not satisfy a gate.
            print("        ^ not counted as cleared: an unverified edge must "
                  "not satisfy a gate")
    if not any_edge:
        print()
        print("  No candidate cleared its gates. This is a valid, useful result:")
        print("  it means no model here should be traded. See")
        print("  docs/ML_SKILL_DIAGNOSIS.md for what a negative skill usually")
        print("  means and which changes are worth trying next.")
    return any_edge


def main(argv=None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING,  # the report below is the output; logs stay quiet
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Import AFTER arg parsing so --help works even if the ML layer cannot
    # import (e.g. a broken optional dependency) and so the error is clean.
    try:
        from blue_engine.ml.training import run_training
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: cannot import the ML training layer: {exc!r}",
              file=sys.stderr)
        return 1

    candidates = _split_csv(args.candidates)
    sources = _split_csv(args.sources) or ["price_cache"]
    cross_section = _split_csv(args.cross_section)

    # Registering needs a signing key. Without one the registry refuses to
    # sign, so fail EARLY with a clear message rather than after a long fit.
    if not args.no_register and not os.environ.get("BLUE_ML_SIGNING_KEY"):
        print("FATAL: BLUE_ML_SIGNING_KEY is not set, so an artifact cannot be "
              "signed.", file=sys.stderr)
        print("       The registry refuses to store unsigned artifacts, because "
              "an unsigned model cannot be verified later.", file=sys.stderr)
        print("       Set it in the environment (never in a file), or pass "
              "--no-register to evaluate only.", file=sys.stderr)
        return 1

    try:
        out = run_training(
            args.symbol,
            sources=sources,
            horizon=args.horizon,
            n_splits=args.splits,
            min_train=args.min_train,
            candidates=candidates,
            n_trials=args.trials,
            target=args.target,
            cross_section=cross_section,
            register=not args.no_register,
        )
    except KeyboardInterrupt:
        print("\ninterrupted - nothing was committed", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    if not args.quiet:
        any_edge = _print_report(out, args.symbol)
    else:
        any_edge = any(
            str(r.get("verdict", "")).startswith("ESTABLISHED")
            for r in ((out.get("manifest") or {}).get("reports") or {}).values()
        )

    if not out.get("ok"):
        return 1
    if args.require_edge and not any_edge:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())