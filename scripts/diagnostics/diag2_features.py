"""Probe 2: single-feature AUC screen — does ANY feature carry signal?

THE LOGIC
---------
Before fitting a model, check whether the raw features have any rank
relationship with the forward return AT ALL. This separates two very different
diagnoses:

  A) one or more features have |AUC-0.5| meaningfully > 0, but the MODEL still
     scores negative  -> the model/fit is the problem (bug, scale, inversion).
  B) every feature sits at AUC ~ 0.5 -> there is simply no linear/rank signal
     in this feature set at this horizon; the model is innocent and the
     features/horizon are the problem.

Both are actionable, but they demand opposite fixes, so we must know which.

AUC here uses the project's own metric (ml.evaluation.metrics.auc) so the
number is directly comparable with the walk-forward report.

Read-only.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.ml.data_sources.price_cache import PriceCacheSource
from blue_engine.ml.evaluation.metrics import auc, hit_rate, skill
from blue_engine.ml.features import engineering, labels as labels_mod


def screen(rows, horizon, feats, y, fwd):
    print("\n=== horizon %d ===" % horizon)
    print("%-14s %8s %8s %9s %9s" % ("feature", "AUC", "sign", "acc@>0.5", "skill"))
    print("-" * 54)
    results = []
    for name in sorted(feats):
        col = feats[name]
        usable = [(f, t) for f, t in zip(col, y) if f is not None and t is not None]
        if len(usable) < 200:
            continue
        xs = [float(f) for f, _ in usable]
        ts = [int(t) for _, t in usable]
        # Raw value as a score: AUC>0.5 means higher feature -> up label.
        a = auc(ts, xs)
        if a is None:
            continue
        # A model predicting "up when feature is above its median" — the
        # simplest possible honest use of the feature.
        med = sorted(xs)[len(xs) // 2]
        preds = [1 if x > med else 0 for x in xs]
        acc = sum(1 for p, t in zip(preds, ts) if p == t) / len(ts)
        sk = skill(ts, preds)
        results.append((name, a, acc, sk))
        print("%-14s %8.4f %8s %9.4f %9.4f"
              % (name, a, "+" if a >= 0.5 else "-", acc, sk))
    return results


src = PriceCacheSource()
rows = src.fetch("NIFTY", None, None).rows
feats = engineering.build_price_features(rows)

print("feature count =", len(feats), " rows =", len(rows))

for h in (1, 5, 10, 20):
    lab = labels_mod.make_labels(rows, horizon=h)
    y, fwd = lab["labels"], lab["forward_returns"]
    res = screen(rows, h, feats, y, fwd)
    if res:
        best = max(res, key=lambda r: abs(r[1] - 0.5))
        print("-> strongest |AUC-0.5| = %s  AUC=%.4f  (edge %.4f)"
              % (best[0], best[1], abs(best[1] - 0.5)))

print("\n=== reference points at h=5 ===")
lab = labels_mod.make_labels(rows, horizon=5)
y = [t for t in lab["labels"] if t is not None]
always_up = [1] * len(y)
always_dn = [0] * len(y)
print("always-UP   : acc=%.4f  skill=%+.4f" % (sum(y) / len(y), skill(y, always_up)))
print("always-DOWN : acc=%.4f  skill=%+.4f" % (1 - sum(y) / len(y), skill(y, always_dn)))
print("base_rate   : %.4f  (majority class = %.4f)"
      % (sum(y) / len(y), max(sum(y), len(y) - sum(y)) / len(y)))