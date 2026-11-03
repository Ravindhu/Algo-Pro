# 0004: Sharpe ratio definition

Date: 2026-11-04
Stages: Research validation, Monitor, Reporting
Status: accepted

## Context

Blue_engine has two Sharpe functions that give different numbers on the same series:

| Function | Formula |
|---|---|
| `Blue_engine/ml/evaluation/metrics.py::sharpe` | mean excess return / std (ddof=1), x sqrt(periods) |
| `Blue_engine/portfolio/attribution/perf.py::sharpe` | annualised compound return - rf, / annualised vol |

Our `src/quant/research/validation.py::sharpe` already uses the first.

## Decision

1. **Standard: arithmetic mean / standard deviation, annualised** (`ml.evaluation.metrics.sharpe`).
   Reasons: it is the textbook definition used by the deflated Sharpe maths (Bailey and Lopez de
   Prado use per-period mean / std), it is the one our walk-forward and DSR inputs use, and it
   does not depend on the compounding path.
2. The compound-return version stays in Blue for attribution reports. Its docstring now says it is
   a different metric (commit with this record).
3. Any report must name the Sharpe it used. Never compare the two directly.

## Consequences

- No change to `src` behaviour.
- Blue attribution reports keep their current number; the docstring makes the difference visible.
