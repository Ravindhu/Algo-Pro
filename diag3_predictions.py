"""Probe 3: what does the walk-forward ACTUALLY predict? (mean probability)

THE QUESTION THIS ANSWERS
-------------------------
The model scores skill ~ -0.10. Two explanations look identical in the folded
skill number but are opposite in meaning:

  (A) NOISE    the model predicts ~50/50, so on a ~54% base rate it lands just
               below the majority class and skill goes slightly negative.
  (B) INVERTED the model is systematically anti-predictive -> a real bug.

Distinguished by mean predicted probability, accuracy and AUC:
  mean p ~ 0.50, acc just under base, AUC ~ 0.50 -> (A) no edge exists
  mean p far from 0.50, acc far below base       -> (B) hunt for a bug

Read-only.
"""
import os
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.ml.data_sources.price_cache import PriceCacheSource
from blue_engine.ml.evaluation import metrics as M
from blue_engine.ml.evaluation.splits import walk_forward
from blue_engine.ml.features import engineering, labels as labels_mod
from blue_engine.ml.models.ridge import fit_ridge, ridge_predict


def fmt_row(fold, n, base, meanp, acc, sk, a, longpct):
    return "{:<5} {:<8} {:<7} {:<8} {:<8} {:<9} {:<8} {:<7}".format(
        fold, n, "%.3f" % base, "%.4f" % meanp, "%.4f" % acc,
        "%+.4f" % sk, "%.4f" % a, "%.1f" % longpct)


src = PriceCacheSource()
rows = src.fetch("NIFTY", None, None).rows
feats = engineering.build_price_features(rows)
names = sorted(feats)

H = 5
lab = labels_mod.make_labels(rows, horizon=H)
y_all = lab["labels"]

X, y, keep = [], [], []
for i in range(len(rows)):
    vals = [feats[k][i] for k in names]
    if all(v is not None for v in vals) and y_all[i] is not None:
        X.append([float(v) for v in vals])
        y.append(int(y_all[i]))
        keep.append(i)

print("usable rows =", len(X), " features =", len(names))
print("overall base rate = %.4f" % (sum(y) / len(y)))

splits = walk_forward([{"as_of_ts": rows[i]["as_of_ts"]} for i in keep],
                      n_splits=5, horizon=H, min_train=250)
print("folds =", len(splits))
print()
print("{:<5} {:<8} {:<7} {:<8} {:<8} {:<9} {:<8} {:<7}".format(
    "fold", "n_test", "base", "mean_p", "acc", "skill", "auc", "long%"))
print("-" * 62)

all_p, all_t, failures = [], [], 0
for s in splits:
    try:
        fitted = fit_ridge([X[i] for i in s.train_idx],
                           [y[i] for i in s.train_idx], ridge_lambda=1.0)
        if fitted is None:
            print("fold %s: fit returned None" % s.fold)
            failures += 1
            continue
        yte = [y[i] for i in s.test_idx]
        p = ridge_predict(fitted, [X[i] for i in s.test_idx])
        preds = [1 if q >= 0.5 else 0 for q in p]
        acc = sum(1 for a, b in zip(preds, yte) if a == b) / len(yte)
        base = sum(yte) / len(yte)
        maj = max(base, 1 - base)
        a = M.auc(yte, p)
        all_p.extend(p)
        all_t.extend(yte)
        print(fmt_row(s.fold, len(yte), base, statistics.mean(p), acc,
                      acc - maj, a if a is not None else 0.0,
                      100.0 * sum(preds) / len(preds)))
    except Exception as exc:
        print("fold %s ERROR: %r" % (s.fold, exc))
        failures += 1

print()
print("fold failures =", failures)
if not all_p:
    print("no predictions produced — cannot continue")
    sys.exit(1)

pooled_auc = M.auc(all_t, all_p)
pooled_acc = sum(1 for q, t in zip(all_p, all_t)
                 if (q >= 0.5) == (t == 1)) / len(all_t)
print("-- pooled across folds --")
print("mean predicted p  = %.4f" % statistics.mean(all_p))
print("stdev predicted p = %.4f" % statistics.stdev(all_p))
print("actual base rate  = %.4f" % (sum(all_t) / len(all_t)))
print("pooled accuracy   = %.4f" % pooled_acc)
print("pooled AUC        = %s" % ("%.4f" % pooled_auc if pooled_auc is not None else "n/a"))
print("fraction p>0.5    = %.4f" % (sum(1 for q in all_p if q > 0.5) / len(all_p)))
if pooled_auc is not None:
    print("AUC-0.5           = %+.4f  (|.| < 0.02 => no rank information)"
          % (pooled_auc - 0.5))

print()
print("-- would ALWAYS-UP have won in these same folds? --")
print("always-up accuracy = %.4f  (skill 0.0000 by definition)"
      % (sum(all_t) / len(all_t)))
print("model accuracy     = %.4f" % pooled_acc)
print("verdict: a model with no rank skill cannot beat always-up; it can only")
print("         tie it or LOSE to it by guessing the minority class sometimes.")