"""Temp probe: does the CROSS-SECTIONAL target carry signal where time-series did not?

This is the decisive test for step 2. It builds the real 5-instrument panel
from the price cache and measures whether any feature can rank instruments
against each other, using the SAME in-sample method as the earlier screens so
the numbers are comparable.

Read-only.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.ml.data_sources.price_cache import PriceCacheSource
from blue_engine.ml.evaluation import metrics as M
from blue_engine.ml.features import cross_section as cs

src = PriceCacheSource()

# Pull every symbol the cache holds. `fetch` needs a symbol, so enumerate the
# ones the earlier probe found and let the cache confirm each.
SYMBOLS = ["NIFTY", "BANKNIFTY", "IT", "PHARMA", "AUTO"]
series = {}
for s in SYMBOLS:
    res = src.fetch(s, None, None)
    if res.ok and res.rows:
        series[s] = res.rows
        print("fetched %-10s %5d rows" % (s, res.n))
    else:
        print("MISSING    %-10s (%s)" % (s, res.warnings[:1]))

print()
print("instruments with data:", len(series))
if len(series) < 2:
    print("cannot build a cross-section")
    sys.exit(0)

for h in (5, 10, 20):
    ds = cs.build_cross_sectional_dataset(series, horizon=h)
    print("=" * 72)
    print("cross-sectional target, horizon = %d" % h)
    if not ds["ok"]:
        print("  NOT BUILT:", ds["reason"])
        continue
    p = ds["panel"]
    m = ds["meta"]
    print("  panel    : %d instruments x %d dates (width %d)"
          % (len(p["symbols"]), p["n_dates"], p["width"]))
    print("  resolution: %s" % p["resolution"])
    print("  rows     : %d   base_rate=%s  unlabelled=%s"
          % (m["n_rows"], m["base_rate"], m["tier"]["n_unlabelled"]))
    print()
    print("  %-13s %9s %14s %10s" % ("feature", "AUC", "best_thr_acc", "skill"))
    print("  " + "-" * 50)
    X, y = ds["X"], ds["y"]
    names = ds["feature_names"]
    best = None
    for fi, name in enumerate(names):
        pairs = [(X[i][fi], y[i]) for i in range(len(X)) if y[i] is not None]
        xs = [a for a, _ in pairs]
        ts = [int(b) for _, b in pairs]
        a = M.auc(ts, xs)
        srt = sorted(xs)
        best_acc = 0.0
        for q in range(1, 10):
            thr = srt[int(len(srt) * q / 10)]
            pred = [1 if x > thr else 0 for x in xs]
            acc = sum(1 for p2, t in zip(pred, ts) if p2 == t) / len(ts)
            best_acc = max(best_acc, acc)
        base = sum(ts) / len(ts)
        maj = max(base, 1 - base)
        sk = best_acc - maj
        if best is None or sk > best[3]:
            best = (name, a, best_acc, sk)
        print("  %-13s %9.4f %14.4f %+10.4f"
              % (name, a if a else 0.0, best_acc, sk))
    print()
    if best:
        print("  STRONGEST: %-12s AUC=%.4f  in-sample skill=%+.4f"
              % (best[0], best[1] if best[1] else 0.0, best[3]))
    print()
    print("  compare: time-series direction at h=5 gave best in-sample skill +0.0062")
    print()