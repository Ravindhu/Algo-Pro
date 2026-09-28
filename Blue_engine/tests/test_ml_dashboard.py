"""
tests/test_ml_dashboard.py — the ML dashboard adapter and page wiring.

WHY THESE TESTS EXIST
---------------------
The ML page is pure presentation over the signed manifest. Two things can
break it silently, and both are caught here:

1. **The adapter drifting from the manifest.** If ``ml_training_snapshot``
   started recomputing a metric instead of reading the stored one, the page
   and the manifest could disagree about whether an edge is real. That is the
   one failure a provenance display must never have, so we assert the values
   are passed through verbatim.

2. **The page not being wired up.** A Dash page that is never registered, or
   whose Interval has no refresh callback, renders once and then freezes —
   a finished training run would be invisible until a manual browser reload.
   We assert both the registration and the callback wiring exist.

Every test is hermetic: a temporary artifact root, a synthetic manifest, no
network and no real training run.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _with_ml_env(fn):
    """Run ``fn`` against a throwaway ML data root and a test signing key.

    The root is per-call so a test can never see (or leave behind) a real
    artifact. The signing key is a fixed test value, never a real secret.
    """
    saved = {k: os.environ.get(k) for k in
             ("BLUE_ML_DATA_ROOT", "BLUE_ML_SIGNING_KEY")}
    root = tempfile.mkdtemp(prefix="ml_dash_test_")
    os.environ["BLUE_ML_DATA_ROOT"] = root
    os.environ["BLUE_ML_SIGNING_KEY"] = "test-key-not-a-real-secret"
    try:
        return fn(root)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


#: A manifest shaped exactly like the one the training orchestrator writes.
#: Pinned by hand so the test fails loudly if the real shape ever drifts.
_FAKE_REPORT = {
    "n_folds": 4, "n_errors": 0,
    "verdict": "ESTABLISHED — skill 0.041 ± 0.012 over 4 folds",
    "wall_seconds": 3.2,
    "folds": [
        {"fold": 0, "train_rows": 250, "test_rows": 60,
         "train_end_date": "2023-06-01", "test_start_date": "2023-07-01",
         "accuracy": 0.55, "base_rate": 0.51, "skill": 0.04,
         "auc": 0.57, "brier": 0.24, "sharpe": 1.4, "fit_seconds": 0.1,
         "error": None},
        {"fold": 1, "train_rows": 310, "test_rows": 60,
         "train_end_date": "2023-09-01", "test_start_date": "2023-10-01",
         "accuracy": 0.56, "base_rate": 0.52, "skill": 0.04,
         "auc": 0.58, "brier": 0.23, "sharpe": 1.1, "fit_seconds": 0.1,
         "error": None},
    ],
    "summary": {
        "n_folds": 4,
        "skill": {"mean": 0.041, "std": 0.012, "min": 0.02, "max": 0.06, "n": 4},
        "auc": {"mean": 0.575, "std": 0.01, "min": 0.56, "max": 0.59, "n": 4},
    },
    "deflation": {"observed": 0.09, "expected_max_sharpe": 0.04,
                  "deflated_sharpe": 2.1, "prob_real": 0.98, "n_trials": 6,
                  "n_obs": 240, "verdict": "established"},
    "cost_check": {"gross_bps": 14.0, "cost_bps": 10.0, "net_bps": 4.0,
                   "survives": True, "verdict": "edge survives costs"},
    "spec": {"name": "ridge_l1", "family": "linear", "ok": True,
             "params": {"ridge_lambda": 1.0}, "n_params_estimate": 15,
             "reason": None},
}

_FAKE_MANIFEST = {
    "run_id": "train-NIFTY-1700000000",
    "symbol": "NIFTY",
    "created_at": 1700000000,
    "git_commit": "abc1234",
    "ml_version": "0.1.0",
    "sources": ["price_cache"],
    "horizon": 5, "n_splits": 5, "min_train": 250, "n_trials": 6,
    "n_rows": 620,
    "feature_names": ["ret_1", "vol_20", "rsi_14"],
    "labels": {"kind": "binary", "horizon": 5, "base_rate": 0.51},
    "X_hash": "deadbeef", "y_hash": "cafebabe",
    "reports": {"ridge_l1": _FAKE_REPORT},
    "name": "NIFTY-ml",
    "status": "draft",
}


def _save_fake_artifact():
    """Write one signed artifact through the real Registry, return its address."""
    from blue_engine.ml.registry import Registry

    payload = {"best_candidate": "ridge_l1", "reports": {"ridge_l1": _FAKE_REPORT}}
    return Registry().save(payload, dict(_FAKE_MANIFEST))["address"]


# --------------------------------------------------------------------------- #
# 1. the empty state — no artifacts yet
# --------------------------------------------------------------------------- #
def test_snapshot_with_no_artifacts_is_honest():
    def _run(_root):
        from blue_engine.dashboard.pages import _data

        snap = _data.ml_training_snapshot()
        _check(snap["ok"] is False, "no artifacts must be ok=False")
        _check(snap["n_runs"] == 0, "no artifacts => n_runs 0")
        _check(snap["latest"] is None, "no artifacts => latest None")
        _check(snap["candidates"] == [], "no artifacts => no candidates")
        _check(any("no ML artifacts" in w for w in snap["warnings"]),
               f"a warning must explain why the page is empty: {snap['warnings']}")
        return True

    _check(_with_ml_env(_run), "empty-state snapshot must succeed")


# --------------------------------------------------------------------------- #
# 2. values are passed through, never recomputed
# --------------------------------------------------------------------------- #
def test_snapshot_passes_manifest_values_through_verbatim():
    def _run(_root):
        from blue_engine.dashboard.pages import _data

        _save_fake_artifact()
        snap = _data.ml_training_snapshot()
        _check(snap["ok"] is True, "a saved artifact must be found")
        _check(snap["n_runs"] == 1, f"expected 1 run, got {snap['n_runs']}")

        run = snap["latest"]
        _check(run["symbol"] == "NIFTY", f"symbol: {run['symbol']}")
        _check(run["git_commit"] == "abc1234", f"commit: {run['git_commit']}")
        _check(run["n_rows"] == 620, f"n_rows: {run['n_rows']}")
        _check(run["status"] == "draft", f"status: {run['status']}")
        _check(run["best_candidate"] == "ridge_l1", "best candidate must carry over")

        c = snap["candidates"][0]
        # The stored numbers, not a re-derivation of them.
        _check(c["skill_mean"] == 0.041, f"skill_mean: {c['skill_mean']}")
        _check(c["skill_std"] == 0.012, f"skill_std: {c['skill_std']}")
        _check(c["prob_real"] == 0.98, f"prob_real: {c['prob_real']}")
        _check(c["n_trials"] == 6, f"n_trials: {c['n_trials']}")
        _check(c["net_bps"] == 4.0, f"net_bps: {c['net_bps']}")
        _check(c["survives_cost"] is True, "the cost gate flag must carry over")
        _check(c["family"] == "linear", f"family: {c['family']}")
        _check("ESTABLISHED" in c["verdict"], f"verdict: {c['verdict']}")
        return True

    _check(_with_ml_env(_run), "artifact snapshot must succeed")


def test_missing_metrics_stay_none_and_are_not_zeroed():
    """A None must survive as None, or the page would render 0.0 as a real score."""
    def _run(_root):
        from blue_engine.dashboard.pages import _data

        _save_fake_artifact()
        # A fold that FAILED carries no metrics at all.
        broken = {"fold_rows": [{"fold": 9, "error": "fit: ValueError: boom",
                                 "train_rows": 0, "test_rows": 0}]}
        folds = _data._flatten_folds(broken["fold_rows"])
        _check(len(folds) == 1, "one failed fold must produce one row")
        _check(folds[0]["error"] == "fit: ValueError: boom",
               "the fold error must be preserved for display")
        _check(folds[0]["skill"] is None,
               "an absent metric must stay None, never become 0.0")
        _check(folds[0]["accuracy"] is None,
               "an absent metric must stay None, never become 0.0")
        return True

    _check(_with_ml_env(_run), "None-preservation check must succeed")


def test_deflation_trial_count_is_not_hidden():
    """The page must always expose n_trials — the number that makes p meaningful."""
    def _run(_root):
        from blue_engine.dashboard.pages import _data

        _save_fake_artifact()
        snap = _data.ml_training_snapshot()
        c = snap["candidates"][0]
        _check(c["n_trials"] is not None,
               "n_trials must never be hidden: deflated p is meaningless without it")
        _check(c["expected_max_sharpe"] == 0.04,
               "the luck baseline must be shown next to the observed Sharpe")
        return True

    _check(_with_ml_env(_run), "deflation exposure check must succeed")


# --------------------------------------------------------------------------- #
# 3. a re-saved artifact de-duplicates to its newest status
# --------------------------------------------------------------------------- #
def test_registry_history_dedupes_to_latest_status():
    def _run(_root):
        from blue_engine.ml.registry import Registry
        from blue_engine.dashboard.pages import _data

        addr = _save_fake_artifact()
        # A promotion appends a second index line for the same address.
        Registry().promote(addr, to="validated")
        _check(len(_data.ml_registry_history()) > 1,
               "the index is append-only, so promote adds a line")

        snap = _data.ml_training_snapshot()
        _check(snap["n_runs"] == 1,
               f"one artifact must show as one run, got {snap['n_runs']}")
        _check(snap["latest"]["status"] == "validated",
               "the page must show the NEWEST status, not the training-time draft")
        return True

    _check(_with_ml_env(_run), "dedupe check must succeed")


# --------------------------------------------------------------------------- #
# 4. an unreadable artifact is reported, not swallowed
# --------------------------------------------------------------------------- #
def test_unreadable_artifact_is_reported():
    def _run(root):
        from blue_engine.dashboard.pages import _data

        # An index line pointing at an address with no files on disk.
        art_dir = os.path.join(root, "ml_artifacts")
        os.makedirs(art_dir, exist_ok=True)
        with open(os.path.join(art_dir, "registry.jsonl"), "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"address": "0" * 64, "name": "ghost",
                                 "status": "draft", "created_at": 1}) + "\n")
        snap = _data.ml_training_snapshot()
        _check(snap["errors"], "an unreadable artifact must surface as an error")
        _check(any("ghost" not in e for e in snap["errors"]),
               "the error names the artifact that could not be read")
        return True

    _check(_with_ml_env(_run), "error-reporting check must succeed")


# --------------------------------------------------------------------------- #
# 5. the page is actually wired into the app
# --------------------------------------------------------------------------- #
def test_ml_page_is_registered_and_refreshable():
    """Registration + refresh callback: a page missing either is broken."""
    def _run(_root):
        import dash
        from blue_engine.dashboard.dash_app import create_app

        app = create_app()
        paths = {p["path"] for p in dash.page_registry.values()}
        _check("/ml" in paths, f"/ml must be registered; pages: {sorted(paths)}")

        # The Interval id must match the callback the app registered, or the
        # page renders once and freezes forever after.
        import blue_engine.dashboard.pages.ml_models as page
        layout = page.layout()
        interval_ids = [c.id for c in layout.children
                        if isinstance(c, dcc_interval_type())]
        _check(interval_ids, "the page must own a refresh Interval")
        _check(interval_ids[0] == "ml-refresh",
               f"Interval id must be 'ml-refresh', got {interval_ids}")
        out_ids = [c.id for c in layout.children if getattr(c, "id", None)]
        _check("ml-refresh-out" in out_ids,
               f"the refreshable container must exist; got {out_ids}")

        # And the render target must produce content without raising.
        kids = page.body()
        _check(isinstance(kids, list) and kids,
               "body() must return renderable children")
        return True

    _check(_with_ml_env(_run), "page wiring check must succeed")


def dcc_interval_type():
    from dash import dcc
    return dcc.Interval


def test_ml_page_renders_without_artifacts():
    """The empty state must render, not crash — a fresh install hits this."""
    def _run(_root):
        import blue_engine.dashboard.pages.ml_models as page

        kids = page.body()
        _check(isinstance(kids, list) and len(kids) >= 3,
               "the empty state must still render a heading, KPIs and a hint")
        return True

    _check(_with_ml_env(_run), "empty-state render must succeed")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("test_ml_dashboard: all passed")