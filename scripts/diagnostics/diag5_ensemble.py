"""Show why the ensemble candidate fails every fold.

The ensemble's fit/ predict contract is deliberately NOT the generic
(X, y) -> probs one: it needs the MEMBER probability vectors, which the
walk-forward runner does not produce. So it cannot be a peer candidate in the
same search list without a wrapper that first fits the members.

This probe prints the error to confirm the diagnosis before deciding the fix.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.ml.data_sources.price_cache import PriceCacheSource
from blue_engine.ml.evaluation import run_walk_forward
from blue_engine.ml.features import engineering, labels as labels_mod
from blue_engine.ml.models import build_candidate

rows = PriceCacheSource().fetch("NIFTY", None, None).rows
feats = engineering.build_price_features(rows)
names = sorted(feats)
lab = labels_mod.make_labels(rows, horizon=5)

X, y, keep = [], [], []
for i in range(len(rows)):
    vals = [feats[k][i] for k in names]
    if all(v is not None for v in vals) and lab["labels"][i] is not None:
        X.append([float(v) for v in vals])
        y.append(int(lab["labels"][i]))
        keep.append(i)

spec = build_candidate("ensemble")
print("ensemble ok =", spec.ok, " reason =", spec.reason)
print("fit_fn requires :", spec.fit_fn.__code__.co_varnames[:2])
print("predict_fn expects:", spec.predict_fn.__code__.co_varnames[:2])
print()

# The generic runner calls fit_fn(X, y) -> that is the mismatch.
try:
    spec.fit_fn(X[:300], y[:300])
    print("fit(X, y) unexpectedly succeeded")
except Exception as exc:
    print("fit(X, y) raises: %s: %s" % (type(exc).__name__, exc))

print()
rep = run_walk_forward([{"as_of_ts": rows[i]["as_of_ts"]} for i in keep],
                       X, y, fit_fn=spec.fit_fn, predict_fn=spec.predict_fn,
                       forward_returns=[lab["forward_returns"][i] for i in keep],
                       horizon=5, n_splits=5, min_train=250)
print("walk-forward verdict:", rep.verdict)
print("n_folds:", rep.n_folds, " n_errors:", rep.n_errors)
for f in rep.folds[:2]:
    print("  fold:", f)