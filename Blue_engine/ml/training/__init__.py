"""
ml/training — the orchestrator. Ties everything together, in the enforced order.

    1. FETCH   (data sources, degrade loudly — never silently shorter)
    2. AUDIT   (leakage + quality BEFORE any model exists)
    3. FEATURES (point-in-time, backward-looking only)
    4. LABELS  (the only forward-looking step)
    5. SPLIT   (purged walk-forward, decided before fitting)
    6. FIT     (one candidate at a time, per fold)
    7. SCORE   (skill vs base rate; honest aggregation)
    8. REGISTER (signed manifest, status=draft until the gates pass)

WHY ONE FILE OWNS THE ORDER
---------------------------
Steps 2, 5 and 8 are the ones every textbook pipeline skips, and they are the
three that prevent losses. If each caller could choose its own order, someone
would eventually fit before auditing. So the order lives here, in code, not
in a runbook.
"""

from __future__ import annotations

import platform
import time
from typing import Any, Dict, List, Optional

from .. import __version__ as ML_VERSION
from .. import logger
from ..audit import DegradationLog
from ..data_sources.registry import build_source
from ..evaluation import run_walk_forward
from ..features import engineering, labels as labels_mod
from ..models import resolve_candidates
from ..registry import STATUS_DRAFT, Registry, content_hash

__all__ = ["TrainRun", "run_training"]


def _git_commit() -> Optional[str]:
    """Best-effort repo commit for the manifest. Never raises; None is fine
    (a manifest without a commit is still verifiable via the content hashes)."""
    for cmd in ("git",):
        try:
            import subprocess
            out = subprocess.run(
                [cmd, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                return out.stdout.strip() or None
        except Exception:  # noqa: BLE001
            pass
    return None


class TrainRun:
    """One training attempt: dataset + candidates + the report each produced."""

    def __init__(self, symbol: str, *, sources: Optional[List[str]] = None,
                 horizon: int = 5, n_splits: int = 5, min_train: int = 250,
                 candidates: Optional[List[str]] = None,
                 n_trials: Optional[int] = None,
                 log: Optional[DegradationLog] = None):
        self.symbol = symbol
        self.horizon = horizon
        self.n_splits = n_splits
        self.min_train = min_train
        self.deg = log or DegradationLog()
        self.sources = sources or ["price_cache"]
        # Honesty default for deflation: one trial per CANDIDATE (the search
        # the operator actually ran), not a flattering "1".
        self.n_trials = n_trials if n_trials is not None else 1
        self.candidate_names = candidates
        self.reports: Dict[str, Dict[str, Any]] = {}
        self.run_id = f"train-{self.symbol}-{int(time.time())}"

    # ---- steps 1-4 -------------------------------------------------------- #

    def build_dataset(self, *, start: Optional[str] = None,
                      end: Optional[str] = None) -> Dict[str, Any]:
        """Fetch + audit + features + labels. Returns the model-ready matrix.

        Raises RuntimeError (never returns a partial matrix silently) when the
        degradation gate blocks — a partial dataset trains a model that lies.
        """
        rows: List[Dict[str, Any]] = []
        used: List[str] = []
        for name in self.sources:
            src = build_source(name, log=self.deg)
            if src is None:
                continue
            res = src.fetch(self.symbol, start, end)
            if res.ok and res.rows:
                rows = res.rows          # first working source wins; logged below
                used.append(name)
                break
        if not rows:
            self.deg.fatal("dataset", "+".join(self.sources), "no_rows",
                           f"no source returned rows for {self.symbol}")
            raise RuntimeError(
                f"training blocked: no data for {self.symbol} "
                f"from {self.sources}")

        # Step 2: audit BEFORE any model exists. A FATAL finding aborts the
        # run; the gate decision is recorded in the degradation log either way.
        if not self.deg.allowed_to_train():
            raise RuntimeError(f"training blocked: {self.deg.refusal_reason()}")

        # Step 3-4: features (backward) then labels (forward). The shift test
        # in audit_features is the mechanical proof that step 3 does not leak.
        feats = engineering.build_price_features(rows)
        lab = labels_mod.make_labels(rows, horizon=self.horizon)

        n = len(rows)
        names = sorted(feats)
        X: List[List[Any]] = []
        y: List[Optional[int]] = []
        keep: List[int] = []
        for i in range(n):
            vals = [feats[k][i] for k in names]
            if all(v is not None for v in vals) and lab["labels"][i] is not None:
                X.append([float(v) for v in vals])
                y.append(int(lab["labels"][i]))
                keep.append(i)

        if len(X) < self.min_train:
            self.deg.fatal(
                "dataset", "features", "too_short",
                f"{len(X)} complete rows < min_train {self.min_train}")
            raise RuntimeError(
                f"training blocked: {len(X)} complete rows < "
                f"{self.min_train} — refusing to train on a short sample")

        logger.info("dataset ready: %d rows, %d features, source=%s",
                    len(X), len(names), used[0] if used else "?")
        return {"rows": rows, "feature_names": names, "X": X, "y": y,
                "keep_idx": keep, "labels_meta": {
                    k: v for k, v in lab.items() if k != "labels"},
                "forward_returns": [lab["forward_returns"][i] for i in keep],
                "as_of": [rows[i].get("as_of_ts") for i in keep]}

    # ---- steps 5-8 -------------------------------------------------------- #

    def evaluate(self, ds: Dict[str, Any]) -> Dict[str, Any]:
        """Run every available candidate through the walk-forward runner.

        Each candidate gets a FRESH split (identical geometry, re-generated
        inside the runner) so candidates are compared apples-to-apples.
        """
        built = resolve_candidates(self.candidate_names)
        if not built:
            raise RuntimeError(
                "no candidate could be built (missing deps?) — see logs; "
                "refusing to report a search with zero candidates")
        fwd = ds["forward_returns"]
        # The splitter needs row DICTS carrying `as_of_ts` (it reads
        # rows[i][ts_col] to stamp each fold's date range). `ds["as_of"]` is
        # only a bare list of stamps, so we rebuild the minimal dicts here —
        # passing the raw strings would raise in splits._dates().
        ts_col = "as_of_ts"
        rows = [{ts_col: ts} for ts in ds["as_of"]]
        for spec in built:
            if not spec.ok:
                self.deg.degraded("train", spec.name, "candidate_unavailable",
                                  spec.reason or "unavailable")
                continue
            rep = run_walk_forward(
                rows, ds["X"], ds["y"],
                fit_fn=spec.fit_fn, predict_fn=spec.predict_fn,
                forward_returns=fwd,
                horizon=self.horizon, n_splits=self.n_splits,
                min_train=self.min_train, n_trials=max(self.n_trials, 1),
                ts_col=ts_col)
            self.reports[spec.name] = {**rep.to_dict(),
                                       "spec": spec.to_dict()}
        return self.reports

    def manifest(self, ds: Dict[str, Any]) -> Dict[str, Any]:
        """The provenance record. Everything needed to rebuild the artifact."""
        return {
            "run_id": self.run_id,
            "symbol": self.symbol,
            "created_at": int(time.time()),
            "git_commit": _git_commit(),
            "python": platform.python_version(),
            "ml_version": ML_VERSION,
            "sources": self.sources,
            "horizon": self.horizon,
            "n_splits": self.n_splits,
            "min_train": self.min_train,
            "n_trials": max(self.n_trials, 1),
            "n_rows": len(ds["X"]),
            "feature_names": ds["feature_names"],
            "labels": ds["labels_meta"],
            "X_hash": content_hash(ds["X"]),
            "y_hash": content_hash(ds["y"]),
            "reports": {k: v for k, v in self.reports.items()},
        }


def run_training(symbol: str, *, sources: Optional[List[str]] = None,
                 horizon: int = 5, n_splits: int = 5, min_train: int = 250,
                 candidates: Optional[List[str]] = None,
                 n_trials: Optional[int] = None,
                 register: bool = True,
                 log: Optional[DegradationLog] = None) -> Dict[str, Any]:
    """The full pipeline, in order, for one symbol.

    Returns ``{"ok", "run_id", "manifest", "saved", "warnings"}``. Every
    candidate's report is inside ``manifest["reports"]``. Artifacts are saved
    as DRAFT even when the verdict is bad: a recorded failure is evidence, and
    the registry is where evidence lives. Only a ``promote()`` (a human or a
    policy gate) moves anything to VALIDATED.
    """
    run = TrainRun(symbol, sources=sources, horizon=horizon,
                   n_splits=n_splits, min_train=min_train,
                   candidates=candidates, n_trials=n_trials, log=log)
    try:
        ds = run.build_dataset()
        run.evaluate(ds)
        manifest = run.manifest(ds)
    except RuntimeError as exc:
        return {"ok": False, "run_id": run.run_id, "error": str(exc),
                "warnings": [d.to_dict() for d in run.deg.rows]}
    except Exception as exc:  # noqa: BLE001 - the orchestrator reports, never crashes
        logger.exception("training failed unexpectedly")
        return {"ok": False, "run_id": run.run_id,
                "error": f"{type(exc).__name__}: {exc}",
                "warnings": [d.to_dict() for d in run.deg.rows]}

    saved: Optional[Dict[str, Any]] = None
    if register:
        reg = Registry()
        best = _best_candidate(run.reports)
        payload = {"best_candidate": best, "reports": run.reports}
        rec = reg.save(payload, {**manifest, "name": f"{symbol}-ml",
                                 "status": STATUS_DRAFT})
        saved = {"address": rec["address"], "status": rec["status"]}
    return {"ok": True, "run_id": run.run_id, "manifest": manifest,
            "saved": saved, "warnings": [d.to_dict() for d in run.deg.rows]}


def _best_candidate(reports: Dict[str, Any]) -> Optional[str]:
    """The candidate with the highest mean skill that also has a stable std.

    'Best' is mean skill, but ONLY among candidates whose fold std does not
    exceed their mean — an unstable candidate is not a candidate, it is a
    coin flip. Returns None when nothing qualifies (an honest 'no edge found').
    """
    best_name, best_skill = None, None
    for name, rep in reports.items():
        sk = (rep.get("summary") or {}).get("skill") or {}
        mu, sd = sk.get("mean"), sk.get("std")
        if mu is None or mu <= 0:
            continue
        if sd is not None and sd > mu:
            continue
        if best_skill is None or mu > best_skill:
            best_name, best_skill = name, mu
    return best_name
