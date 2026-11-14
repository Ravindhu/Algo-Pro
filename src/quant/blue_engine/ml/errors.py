"""ML layer exceptions.

WHY custom exceptions instead of `Exception`:
The dashboard must distinguish "we have no data yet" (a *state*, show a badge)
from "the data is corrupt" (an *incident*, page someone). Catching a broad
exception in the engine and continuing would violate the AGENTS.md rule:
"nothing in your report is a guess". Each class below is caught at a different
level and produces a different colour in the dashboard.
"""

from __future__ import annotations


class MLError(Exception):
    """Base class for every error raised by Blue_engine.ml.

    Catch this only in top-level CLI scripts, and always re-raise after logging:
    a swallowed MLError means a model trains on partial data, trains silently,
    or worse — a live strategy uses a stale artifact while believing it is fresh.
    """


class DataUnavailableError(MLError):
    """A required data source returned nothing for the requested window.

    Typical causes (all real, all observed):
      * NSE block list — scraping from a cloud IP without proper headers.
      * Vendor rate limit — the same source queried 30 times in one minute.
      * A non-trading day range — e.g. holiday-week-only requests.
      * Credentials missing/rotated for a paid feed.

    Handling policy: the quality gate marks the dataset `degraded` and the
    training orchestrator REFUSES to proceed (raise, do not skip) unless the
    caller explicitly passes `allow_degraded=True` and records a reason string
    in the run manifest. That reason is what gets shown to the user later, so
    they can never be told "the model is trained" when it is only partially so.
    """


class LeakageError(MLError):
    """Point-in-time discipline was violated — a feature used future data.

    This is the single most expensive bug class in quant ML. It does NOT raise a
    Python error at trade time; it just makes the backtest beautiful and the
    live P&L ugly. So we make it fatal, early, in the offline layer:

    Example (a real one we guard against):
        # WRONG — uses the close of the same bar we are predicting
        feat['ret_1d'] = df['close'].pct_change()
        label = (df['close'].shift(-1) > df['close'])

        # RIGHT — feature is shifted so only *yesterday's close* is visible
        feat['ret_1d'] = df['close'].pct_change().shift(1)

    The detector in ml/audit/leakage.py automates the check; this exception is
    raised when a manual audit also finds a violation, so there is one place to
    look when the pipeline stops.
    """


class ModelNotValidatedError(MLError):
    """An artifact was requested for live use but is not in `validated` status.

    Why status exists: a model can be *trained* but not *validated* — the
    walk-forward may have failed, or the sample was too small. Live execution
    paths must raise this, and the dashboard shows a red "BLOCKED" badge instead
    of quietly trading a bad model. Better flat for a day than wrong for a month.
    """


class SchemaDriftError(MLError):
    """Incoming data's columns/dtypes/units differ from what the model was fit on.

    Real case study: a vendor "fixed" its volume column from lakhs to shares —
    a 100,000x unit change. The model kept "working" and produced garbage signals
    for weeks. No exception, no log line. We prevent that by hashing the feature
    schema into the manifest; this error fires when the hash changes, and the
    operator must consciously retrain.
    """
