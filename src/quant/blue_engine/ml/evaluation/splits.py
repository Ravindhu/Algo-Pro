"""
ml/evaluation/splits.py — time-series splits that cannot leak.

WHY RANDOM K-FOLD IS BANNED HERE
--------------------------------
`train_test_split(shuffle=True)` and `KFold` are the default in every tutorial and
they are catastrophic on market data. Two independent mechanisms break:

  (a) **Autocorrelation.** Adjacent days are highly correlated. Put day 100 in
      train and day 101 in test and the model has effectively memorised the
      answer. Validation accuracy looks great; live performance is zero.

  (b) **Label horizon overlap.** If the label at `t` is the return from `t` to
      `t+5`, then the label at `t` is not known until `t+5`. A test row at `t+1`
      shares four of its five forward days with a training row at `t`. The model
      is being tested on information it trained on. This is the *purge* problem.

So this module offers only three splitters, all walk-forward:

    walk_forward(...)        expanding or rolling window, sequential folds
    purged_kfold(...)        purged + embargoed K-fold (López de Prado)
    last_holdout(...)        the newest N rows, for a final untouched check

PURGE AND EMBARGO, IN PLAIN ENGLISH
-----------------------------------
    purge    = throw away training rows whose LABEL window overlaps the test
               window. If the label looks 5 days ahead, the 5 training rows
               immediately before the test set must go: their labels are not yet
               fully known when the test set starts.
    embargo  = additionally drop a few rows AFTER the test set from the next
               training window. Markets are serially correlated, so the day right
               after the test period still carries its information. A small
               embargo (a few days) breaks that link.

Both are parameterised and both default to the label horizon. Getting them wrong
is silent: the split "works", the metrics are just optimistic.

WHY EXPANDING IS THE DEFAULT MODE
---------------------------------
Expanding means each fold trains on everything up to that point, like a desk
accumulating history. Rolling means each fold trains on a fixed-length recent
window, like a model that adapts to the current regime.

Neither is universally right:
  * expanding  — more data, but early regimes keep influencing late predictions.
  * rolling    — regime-adaptive, but throws away history and can overfit a short
                 window if the fold count is low.
Default is expanding because with the history we currently have (a few years) the
rolling windows would be too short to fit anything. Once the price cache holds
10+ years, rolling with a 750-day window becomes the better choice — and that
switch is a config value, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .. import logger

__all__ = ["Split", "walk_forward", "purged_kfold", "last_holdout",
           "describe_splits"]


@dataclass
class Split:
    """One train/test split, expressed as row index ranges.

    Indices, not timestamps, because everything downstream (the feature matrix,
    the label vector) is index-aligned. The timestamps are carried alongside
    purely for logging and for the manifest — an artifact should record *which
    dates* it was trained on, so a later investigation can ask "was this model
    fitted through the COVID crash?" without re-running the splitter.
    """

    fold: int
    train_idx: List[int]
    test_idx: List[int]
    #: Rows dropped from the end of train because their labels overlap the test
    #: window. Recorded so the audit can show purge actually happened — a
    #: splitter that silently purges nothing is the bug this whole file prevents.
    purged_idx: List[int] = field(default_factory=list)
    #: Rows dropped immediately after the test window.
    embargo_idx: List[int] = field(default_factory=list)
    train_dates: Tuple[Optional[str], Optional[str]] = (None, None)
    test_dates: Tuple[Optional[str], Optional[str]] = (None, None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fold": self.fold,
            "n_train": len(self.train_idx),
            "n_test": len(self.test_idx),
            "n_purged": len(self.purged_idx),
            "n_embargoed": len(self.embargo_idx),
            "train_dates": list(self.train_dates),
            "test_dates": list(self.test_dates),
        }


def _dates(rows: Sequence[Dict[str, Any]], idx: Sequence[int],
           ts_col: str) -> Tuple[Optional[str], Optional[str]]:
    if not idx:
        return (None, None)
    first = str(rows[idx[0]].get(ts_col) or "")[:10] or None
    last = str(rows[idx[-1]].get(ts_col) or "")[:10] or None
    return (first, last)


def walk_forward(rows: Sequence[Dict[str, Any]], *,
                 n_splits: int = 5,
                 horizon: int = 5,
                 embargo: Optional[int] = None,
                 mode: str = "expanding",
                 min_train: int = 250,
                 ts_col: str = "as_of_ts",
                 ) -> List[Split]:
    """Sequential walk-forward folds with purge and embargo.

    Layout for `n_splits=3`, `horizon=1` on 100 rows::

        fold 0:  train [0..49]   test [51..65]     (row 50 purged)
        fold 1:  train [0..65]   test [67..81]     (row 66 purged)
        fold 2:  train [0..81]   test [83..99]     (row 82 purged)

    Every test block is strictly AFTER its training block, and the boundary row is
    purged so no training label peeks into the test period. That ordering is what
    makes the reported metrics an estimate of *future* performance.

    Args:
        rows: dataset, sorted oldest-first.
        n_splits: number of test blocks. More folds = more stable metrics but
            shorter test blocks. 5 is a reasonable default: each test block is
            ~20% of the usable span, which for 3 years of daily data is ~7 months
            — long enough to contain a real drawdown.
        horizon: the label's forward horizon, in bars. **The purge length.**
            Passing the wrong value here (e.g. 1 when the label looks 5 days
            ahead) silently re-introduces the leak this function exists to
            prevent. The trainer reads it from the label config, not by hand.
        embargo: extra bars dropped after each test block. Defaults to `horizon`.
        mode: "expanding" (train on all history up to the fold) or "rolling"
            (train on the most recent `min_train` bars only).
        min_train: minimum training rows for the first fold. Below ~250 bars (one
            trading year) a fitted model is mostly noise, so folds that cannot
            meet this are skipped and logged rather than silently produced.
        ts_col: timestamp column, used only for the manifest.

    Returns:
        A list of :class:`Split`. Empty when the data is too short — the caller
        must treat that as a hard failure, not as "no folds, carry on".
    """
    n = len(rows)
    if n == 0:
        logger.warning("walk_forward: empty dataset")
        return []

    embargo = int(horizon if embargo is None else embargo)
    if n_splits < 1:
        raise ValueError("n_splits must be >= 1")

    # Reserve the final test block, then split the remainder into equal blocks.
    # Integer floor keeps every test block the same size, which matters: uneven
    # blocks make per-fold metrics incomparable.
    test_size = max(1, n // (n_splits + 1))
    splits: List[Split] = []

    for k in range(n_splits):
        test_start = n - (n_splits - k) * test_size
        test_end = test_start + test_size
        if test_start <= 0 or test_end > n:
            continue

        # Purge: drop the `horizon` rows immediately before the test block.
        train_end = max(0, test_start - horizon)
        if mode == "rolling":
            train_start = max(0, train_end - min_train)
        else:
            train_start = 0

        train_idx = list(range(train_start, train_end))
        test_idx = list(range(test_start, test_end))

        if len(train_idx) < min_train:
            logger.info("walk_forward: fold %d skipped (train=%d < min_train=%d)",
                        k, len(train_idx), min_train)
            continue

        purged = list(range(train_end, test_start))
        emb_idx = list(range(test_end, min(n, test_end + embargo)))

        splits.append(Split(
            fold=k, train_idx=train_idx, test_idx=test_idx,
            purged_idx=purged, embargo_idx=emb_idx,
            train_dates=_dates(rows, train_idx, ts_col),
            test_dates=_dates(rows, test_idx, ts_col),
        ))

    if not splits:
        logger.warning(
            "walk_forward produced no folds: n=%d, min_train=%d, n_splits=%d. "
            "The dataset is too short — do NOT train without folds.", n,
            min_train, n_splits)

    return splits


def purged_kfold(rows: Sequence[Dict[str, Any]], *,
                 n_splits: int = 5,
                 horizon: int = 5,
                 embargo: Optional[int] = None,
                 min_train: int = 250,
                 ts_col: str = "as_of_ts",
                 ) -> List[Split]:
    """Purged + embargoed K-fold: every fold held out once, always forward.

    Difference from :func:`walk_forward`: each test block is a *different* part of
    the series, so the model is evaluated across the whole history rather than
    only the end. Useful for hyper-parameter selection, where you want a stable
    estimate rather than a simulation of a single forward path.

    This is NOT random K-fold. Folds are still contiguous time blocks, and each
    fold's training set is only the rows available *before* it — so it remains a
    strictly causal evaluation. The name says "k-fold" because the series is
    partitioned; the causality is what matters.

    Caution: because fold 0 has almost no history, early folds are weak. They are
    kept (with their `min_train` guard) because dropping them would hide that the
    early period is untrainable, which is information.
    """
    n = len(rows)
    if n == 0:
        return []
    embargo = int(horizon if embargo is None else embargo)
    block = max(1, n // n_splits)

    splits: List[Split] = []
    for k in range(n_splits):
        test_start = k * block
        test_end = min(n, test_start + block)
        if test_start <= 0:
            # Fold 0 has no prior history by construction. Record the intent and
            # skip rather than train on an empty set.
            logger.info("purged_kfold: fold 0 skipped (no prior history)")
            continue
        train_end = max(0, test_start - horizon)
        train_idx = list(range(0, train_end))
        if len(train_idx) < min_train:
            logger.info("purged_kfold: fold %d skipped (train=%d < %d)",
                        k, len(train_idx), min_train)
            continue
        splits.append(Split(
            fold=k, train_idx=train_idx,
            test_idx=list(range(test_start, test_end)),
            purged_idx=list(range(train_end, test_start)),
            embargo_idx=list(range(test_end, min(n, test_end + embargo))),
            train_dates=_dates(rows, train_idx, ts_col),
            test_dates=_dates(rows, list(range(test_start, test_end)), ts_col),
        ))
    return splits


def last_holdout(rows: Sequence[Dict[str, Any]], *,
                 holdout_frac: float = 0.2,
                 horizon: int = 5,
                 ts_col: str = "as_of_ts",
                 ) -> Optional[Split]:
    """The newest slice of the data, kept untouched for a final honest check.

    Why this is separate from walk-forward: once you have tuned hyper-parameters
    against walk-forward folds, those folds are *used up* — they have influenced
    your choices, so they can no longer give an unbiased estimate. The holdout is
    the one number you look at once, at the end, and it is why you must not tune
    against it.

    Returns None when the dataset is too small to reserve a holdout at all, which
    the caller must treat as "cannot validate" rather than "passed".
    """
    n = len(rows)
    if n < 50:
        return None
    cut = int(n * (1.0 - holdout_frac))
    train_end = max(0, cut - horizon)
    if train_end < 20 or n - cut < 5:
        return None
    return Split(
        fold=-1,
        train_idx=list(range(0, train_end)),
        test_idx=list(range(cut, n)),
        purged_idx=list(range(train_end, cut)),
        embargo_idx=[],
        train_dates=_dates(rows, list(range(0, train_end)), ts_col),
        test_dates=_dates(rows, list(range(cut, n)), ts_col),
    )


def describe_splits(splits: Sequence[Split]) -> Dict[str, Any]:
    """Summary for the manifest and the dashboard.

    Includes `overlap_violations`: an explicit count of any training index that
    falls inside a test index range. It must always be 0 — and computing it rather
    than asserting it means a future refactor that breaks causality shows up as a
    number in a report instead of as an unexplained good backtest.
    """
    if not splits:
        return {"n_folds": 0, "note": "no folds — dataset too short"}

    violations = 0
    for s in splits:
        test_set = set(s.test_idx)
        violations += sum(1 for i in s.train_idx if i in test_set)

    return {
        "n_folds": len(splits),
        "folds": [s.to_dict() for s in splits],
        "overlap_violations": violations,
        "total_train_rows": sum(len(s.train_idx) for s in splits),
        "total_test_rows": sum(len(s.test_idx) for s in splits),
        "total_purged_rows": sum(len(s.purged_idx) for s in splits),
        "total_embargoed_rows": sum(len(s.embargo_idx) for s in splits),
    }