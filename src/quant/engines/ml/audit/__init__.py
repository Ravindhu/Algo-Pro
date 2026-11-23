"""
ml/audit — the honesty layer.

Nothing in this package makes money. It exists to stop the *other* packages from
lying to you, which in a trading system is the same thing.

Modules
-------
degradation   Degradation rows + severity gate (what we could not get, and why)
leakage       Point-in-time leak detector (the look-ahead tripwire)
quality       Data-quality gate (row counts, gaps, staleness, schema drift)

**Read `degradation` first if you read only one file.** It defines the vocabulary
(`Severity`) that the other two use, and it is what makes a failure *visible* on
the dashboard instead of an inference from a log line.

The three together answer, for any training run, three questions the operator is
entitled to ask before risking capital:

  "Did you have all the data you said you had?"   -> degradation
  "Is any feature secretly using the future?"     -> leakage
  "Is the data clean, complete and fresh enough?" -> quality

If any answer is "no" or "not sure", the run is BLOCKED and the dashboard says so
in plain English. A blocked run costs an afternoon. A dishonest run costs money.
"""

from __future__ import annotations

from .degradation import Degradation, DegradationLog, Severity, first_fatal
from .leakage import (
    LeakFinding,
    audit_features,
    check_future_correlation,
    check_shift_alignment,
    check_timestamps,
)
from .quality import QualityReport, check_quality, require_quality

__all__ = [
    # degradation
    "Degradation", "DegradationLog", "Severity", "first_fatal",
    # leakage
    "LeakFinding", "audit_features", "check_timestamps",
    "check_shift_alignment", "check_future_correlation",
    # quality
    "QualityReport", "check_quality", "require_quality",
]