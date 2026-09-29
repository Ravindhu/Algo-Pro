"""Probe 1: is the cached NIFTY close series valid, and what is the label balance?

WHY THIS FIRST
--------------
Before blaming a model, confirm the data is a sane price series. A huge class
imbalance would explain a negative *skill* without any bug: skill is measured
against the MAJORITY class, so a model that predicts the minority class
accurately can still score negative.

Read-only. Nothing is written.
"""
import os
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.ml.data_sources.price_cache import PriceCacheSource
from blue_engine.ml.features import engineering, labels as labels_mod

src = PriceCacheSource()
res = src.fetch("NIFTY", None, None)
print("fetch ok =", res.ok, " n =", res.n, " origin =", res.origin)
rows = res.rows

closes = [float(r["close"]) for r in rows if r.get("close") is not None]
print("\n-- close series --")
print("n           =", len(closes))
print("first/last  = %.2f .. %.2f" % (closes[0], closes[-1]))
print("min/max     = %.2f .. %.2f" % (min(closes), max(closes)))
print("mean        = %.2f   median = %.2f" % (statistics.mean(closes), statistics.median(closes)))

# Simple daily returns — sign sanity, and the classic data-error tells.
rets = [b / a - 1.0 for a, b in zip(closes, closes[1:]) if a > 0]
print("\n-- daily returns --")
print("n           =", len(rets))
print("mean        = %+.5f (%.2f%%/yr simple)" % (statistics.mean(rets),
                                                   statistics.mean(rets) * 252 * 100))
print("stdev       = %.5f (%.1f%% annualised)" % (statistics.stdev(rets),
                                                   statistics.stdev(rets) * (252 ** 0.5) * 100))
print("min/max     = %+.4f / %+.4f" % (min(rets), max(rets)))
big = [r for r in rets if abs(r) > 0.10]
print("moves >10%% = %d  %s" % (len(big), ["%+.3f" % b for b in big[:5]]))

# Monotonic sequences => unadjusted / stale / duplicated data
runs, cur = 1, 1
for a, b in zip(closes, closes[1:]):
    if a == b:
        cur += 1
        runs = max(runs, cur)
    else:
        cur = 1
print("longest identical-close run =", runs)
dupes = sum(1 for a, b in zip(closes, closes[1:]) if a == b)
print("identical consecutive closes =", dupes)

# EMA of closes would be a red flag (a smoothed series, not real prices).
from collections import Counter
diffs = Counter(len(str(c).split(".")[-1]) for c in closes[:200])
print("decimal-place distribution (first 200):", dict(diffs))

print("\n-- label balance by horizon (binary, 10bps cost) --")
for h in (1, 2, 3, 5, 10, 20):
    lab = labels_mod.make_labels(rows, horizon=h)
    bal = lab["balance"]
    fwd = [f for f in lab["forward_returns"] if f is not None]
    pos = sum(1 for f in fwd if f > 0) / len(fwd) if fwd else 0
    print("  h=%2d  n=%4d  base_rate=%.4f (%s)  raw_up=%.4f  mean_fwd=%+.5f"
          % (h, bal["n"], bal["base_rate"], bal["note"][:22], pos,
             statistics.mean(fwd) if fwd else 0))

print("\n-- feature availability (non-None per feature) --")
feats = engineering.build_price_features(rows)
for k in sorted(feats):
    vals = feats[k]
    ok = sum(1 for v in vals if v is not None)
    print("  %-12s %4d / %d" % (k, ok, len(vals)))