"""Temp probe: does the VOLATILITY target carry signal where direction did not?

The diagnosis found no feature could beat the majority class for the SIGN of a
5-day return. Volatility clustering is a different (and well-documented) fact,
so this measures the same feature set against two new targets:

  kind="vol_ratio"           forward realised vol vs its trailing norm
  kind="triple_barrier_cost" barriers net of charges

Same method as the earlier screens, so the numbers are directly comparable:
rank AUC, and the best single-threshold rule chosen IN-SAMPLE (an upper bound,
not a performance claim).

Read-only.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.ml.data_sources.price_cache import PriceCacheSource
from blue_engine.ml.evaluation import metrics as M
from blue_engine.ml.features import engineering, labels as labels_mod

rows = PriceCacheSource().fetch("NIFTY", None, None).rows
feats = engineering.build_price_features(rows)


def screen(kind, lab, title):
    y = lab["labels"]
    bal = lab["balance"]
    print("=" * 72)
    print("%s  (kind=%s)" % (title, kind))
    print("  n=%d  base_rate=%s  majority bar=%s"
          % (bal["n"], bal["base_rate"],
             round(max(bal["base_rate"], 1 - bal["base_rate"]), 4)
             if bal["base_rate"] is not None else None))
    print("  %s" % bal["note"][:70])
    print()
    print("  %-13s %9s %14s %10s" % ("feature", "AUC", "best_thr_acc", "skill"))
    print("  " + "-" * 50)
    best = None
    for name in sorted(feats):
        col = feats[name]
        pairs = [(float(a), int(b)) for a, b in zip(col, y)
                 if a is not None and b is not None]
        if len(pairs) < 300:
            continue
        xs = [a for a, _ in pairs]
        ts = [b for _, b in pairs]
        a_auc = M.auc(ts, xs)
        srt = sorted(xs)
        best_acc = 0.0
        for q in range(1, 10):
            thr = srt[int(len(srt) * q / 10)]
            up = [1 if x > thr else 0 for x in xs]
            acc = sum(1 for p, t in zip(up, ts) if p == t) / len(ts)
            best_acc = max(best_acc, acc)
        base = sum(ts) / len(ts)
        maj = max(base, 1 - base)
        sk = best_acc - maj
        row = (name, a_auc, best_acc, sk)
        if best is None or sk > best[3]:
            best = row
        print("  %-13s %9.4f %14.4f %+10.4f"
              % (name, a_auc if a_auc else 0.0, best_acc, sk))
    print()
    if best:
        print("  STRONGEST in-sample feature: %s  AUC=%.4f  skill=%+.4f"
              % (best[0], best[1] if best[1] else 0.0, best[3]))
    print()


# Direction, for reference (this is the target that failed).
screen("binary", labels_mod.make_labels(rows, horizon=5, kind="binary"),
       "REFERENCE: sign of 5-day return (the target that failed)")

# Volatility: forward realised vol vs its trailing 20d norm.
for h in (5, 10):
    screen("vol_ratio",
           labels_mod.make_labels(rows, horizon=h, kind="vol_ratio"),
           "TARGET: forward vol > trailing norm, h=%d" % h)

# Cost-aware barriers.
screen("triple_barrier_cost",
       labels_mod.make_labels(rows, horizon=10, kind="triple_barrier_cost",
                              profit_pct=0.02, stop_pct=0.01),
       "TARGET: triple barrier net of 10bps costs, h=10")