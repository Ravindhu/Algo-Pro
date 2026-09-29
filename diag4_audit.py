"""Probe 4: run the project's OWN leakage audit on the real features.

WHY
---
The predictions are essentially noise (mean p = 0.4957, AUC = 0.4638). Before
concluding "no edge exists", rule out the opposite failure: that the features
are silently broken (a shifted/garbage column would also produce AUC below
0.5). The leakage auditor's shift test is the mechanical check for this, and
it already exists in the codebase -- we are only exercising it.

Also report the simplest honest benchmark: does a single-feature threshold
rule beat always-up anywhere on the real data? If nothing does, the feature
set carries no exploitable information at this horizon, full stop.

Read-only.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.ml.audit import audit_features
from blue_engine.ml.data_sources.price_cache import PriceCacheSource
from blue_engine.ml.evaluation import metrics as M
from blue_engine.ml.features import engineering, labels as labels_mod

src = PriceCacheSource()
rows = src.fetch("NIFTY", None, None).rows
lab = labels_mod.make_labels(rows, horizon=5)
fwd = lab["forward_returns"]

print("=== 1. project leakage audit on the real feature builder ===")
try:
    report = audit_features(rows, build_features=engineering.build_price_features,
                            forward_returns=fwd)
    d = report.to_dict() if hasattr(report, "to_dict") else report
    fatal = d.get("fatal") or []
    degraded = d.get("degraded") or []
    print("ok        =", d.get("ok"))
    print("fatal     =", len(fatal))
    for f in fatal[:5]:
        print("   FATAL:", f)
    print("degraded  =", len(degraded))
    for g in degraded[:5]:
        print("   WARN :", g)
    if not fatal:
        print("-> the feature builder did NOT leak: it is not reading the future.")
except Exception as exc:
    print("audit raised: %r" % exc)

print()
print("=== 2. strongest single-feature rule (in-sample ceiling) ===")
feats = engineering.build_price_features(rows)
y = lab["labels"]
best = []
for name in sorted(feats):
    col = feats[name]
    pairs = [(float(a), int(b)) for a, b in zip(col, y)
             if a is not None and b is not None]
    if len(pairs) < 300:
        continue
    xs = [a for a, _ in pairs]
    ts = [b for _, b in pairs]
    a_auc = M.auc(ts, xs)
    # best threshold on a decile grid -> the in-sample ceiling of a 1-feature rule
    srt = sorted(xs)
    best_acc, best_thr = 0.0, None
    for q in range(1, 10):
        thr = srt[int(len(srt) * q / 10)]
        up = [1 if x > thr else 0 for x in xs]
        acc = sum(1 for p, t in zip(up, ts) if p == t) / len(ts)
        if acc > best_acc:
            best_acc, best_thr = acc, thr
    base = sum(ts) / len(ts)
    maj = max(base, 1 - base)
    best.append((name, a_auc, best_acc, best_acc - maj))
    print("  %-12s AUC=%.4f  best_thr_acc=%.4f  skill=%+.4f"
          % (name, a_auc if a_auc else 0.0, best_acc, best_acc - maj))

print()
if best:
    b = max(best, key=lambda r: r[3])
    print("best in-sample single feature: %s  skill=%+.4f" % (b[0], b[3]))
    print("NOTE: this is IN-SAMPLE (threshold chosen on the same data).")
    print("      An in-sample best of ~0 is still a ceiling, not a signal:")
    print("      it means even WITH hindsight no feature separates up from down.")

print()
print("=== 3. the honest benchmark table (h=5) ===")
ys = [int(t) for t in y if t is not None]
n = len(ys)
pos = sum(ys)
print("  always-UP          acc=%.4f  skill=%+.4f" % (pos / n, M.skill(ys, [1] * n)))
print("  always-DOWN        acc=%.4f  skill=%+.4f" % (1 - pos / n, M.skill(ys, [0] * n)))
print("  majority class     acc=%.4f  skill=%+.4f"
      % (max(pos, n - pos) / n, max(pos, n - pos) / n - max(pos, n - pos) / n))
print()
print("Any model must EXCEED %.4f accuracy to score skill > 0." % (max(pos, n - pos) / n))
print("The ridge model scores %.4f. It is not close." % 0.4732)