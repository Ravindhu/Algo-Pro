# Decision 0017: P&L visibility, test-only data, and live guard (2026-11-06)

Status: DECIDED by owner. Follows the audit of 2026-11-06.

## Owner decisions

1. **P&L stays visible on the dashboard by default.** The default is `pnl_visible_default: True`
   in `dashboard/app.py`. This overrides the P&L-blind default in the original brief. No code change.
2. **INFY is for testing only.** INFY bars (Blue_engine sample data) are never used for live
   trading. Live trading needs a licensed live data source, and the owner must choose it.
   Recorded in `config/data_sources.yaml`. A test checks the flag.

## Live order guard (hardening)

The old guard only checked that `live_approval` was a non-empty string.
The new guard in `src/quant/execution/openalgo_client.py` needs all of these:

| Step | Check | Refused when |
|---|---|---|
| 1 | Mode is live | mode is paper |
| 2 | A `LiveApproval` object exists with an approver | string, None, or blank approver |
| 3 | A limits file path is given | path is missing |
| 4 | The approval's SHA-256 matches `config/risk_limits.yaml` now | limits changed after approval |
| 5 | A `GateDecision` is approved and matches the intent id | missing, rejected, or other intent |
| 6 | API key is not empty | empty key |

Plain English: a human must approve a specific version of the risk limits. If anyone changes
the limits file, the approval stops working. Each order must also pass the risk gate.

The layout test now also blocks the raw endpoint string `placeorder` outside the client file.

## What is not changed

- The live path is still not enabled. There is no live runner.
- `config/risk_limits.yaml` values are unchanged.
- The gate monetary checks still run in Blue_engine (see audit finding 5).

## Tests

- `tests/unit/test_live_guard.py` (12 cases: every refusal and the allowed path).
- `tests/unit/test_data_sources.py` (INFY test-only, live sources need a licence).
- `tests/unit/test_layout.py` (placeorder string check).
- `tests/unit/test_phase1_risk_exec.py` (updated to the new signature).
- Full unit suite here: 36 failed, 398 passed. The 36 failures are the same set as the
  untouched commit. They come from missing `httpx` and from HRP tests. No new failures.
