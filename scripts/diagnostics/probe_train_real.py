"""Temp probe: can the ML pipeline train on the REAL cached NIFTY bars?

Read-only w.r.t. the repo: writes its artifact into Temp/, not data/.
Answers the only question that matters for prioritisation:
does the pipeline produce a real model on real data, or does it fail?
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Throwaway artifact root so we do not touch the real registry.
os.environ["BLUE_ML_DATA_ROOT"] = os.path.join(_HERE, "ml_probe_artifacts")
os.environ["BLUE_ML_SIGNING_KEY"] = "temp-probe-key-not-a-real-secret"

from blue_engine.ml.training import run_training

out = run_training("NIFTY", sources=["price_cache"], candidates=["ridge"],
                   horizon=5, n_splits=5, min_train=250, register=True)

print("TRAIN ok =", out["ok"])
print("saved    =", out.get("saved"))
if not out["ok"]:
    print("ERROR    =", out.get("error"))
    sys.exit(1)

rep = out["manifest"]["reports"]["ridge_l1"]
print("verdict  =", rep["verdict"])
print("n_folds  =", rep["n_folds"], " errors =", rep["n_errors"])
sk = rep["summary"].get("skill") or {}
print("skill    = %s +/- %s  (min %s)" % (sk.get("mean"), sk.get("std"), sk.get("min")))
d = rep["deflation"] or {}
print("deflated p =", d.get("prob_real"), " trials =", d.get("n_trials"))
c = rep["cost_check"] or {}
print("net bps  =", c.get("net_bps"), " survives =", c.get("survives"))
print("rows     =", out["manifest"]["n_rows"], " features =",
      len(out["manifest"]["feature_names"]))
for f in rep["folds"]:
    print("  fold %s: skill=%s auc=%s %s..%s" % (
        f.get("fold"), f.get("skill"), f.get("auc"),
        f.get("train_end_date"), f.get("test_start_date")))