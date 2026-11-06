# Decision 0018: port the monetary risk checks out of Blue_engine (2026-11-06)

Status: IMPLEMENTED. Owner sign-off: PENDING (DECOUPLING_PLAN D3). The decoupling plan requires
an owner review before this merges. The owner asked for the fix on 2026-11-06, and the parity
result is below for review.

## What changed

- New file `src/quant/risk/money_checks.py`. Pure Python. Ports Blue's `RiskGate.check` for entry
  intents: action, quantity, size cap, notional cap, daily-loss cap, exposure cap.
- `gate.py` now imports `entry_breaches` from `money_checks.py`. The gate no longer loads Blue for
  these checks.
- The old Blue-backed wrapper, `risk/blue_risk_gate.py`, moved to
  `Temp/Staging Workspace/blue_risk_gate_wrapper_2026-11-06.py` (not deleted, per AGENTS.md).
- The old Blue-spy test moved to `Temp/Staging Workspace/`. Its cases are covered by
  `tests/unit/test_money_checks.py` (hand-checked, runs without Blue).

## Why it is safe to port

Blue's `core/risk_gate.py` uses only the Python standard library. The port is a line-by-line
copy of the arithmetic and the breach text. The parity test loads Blue's real class and compares.

## Parity evidence

`tests/parity/test_money_checks_parity.py`:
- 34,560 grid cases: 3 actions x 8 quantities x 6 prices x 16 limit sets x 6 P&L values x 5 exposure values.
- Compares the breach lists exactly (text included). Requires at least 1,000 real breaches in the grid.
- Edge cases: quantity 0, no price, loss exactly at the limit, exposure exactly at the cap (passes),
  exposure 1 over the cap (fails).
- Result: zero differences.

## What is not ported (on purpose)

- Blue's master `blocked` switch and live acknowledgement. The quant gate has its own kill switch and live guard.
- Exit reduction mode. Exits never reach this check.
- Config-level fallbacks for `open_exposure` and `price`. The quant gate passes these values in.

## Unchanged policies

- Fail closed on an unset entry limit: `pretrade.py` and `loss_breaker.py` still reject. `entry_breaches`
  returns no breaches when all four limits are unset, the same as the old wrapper.
- Exits and protection orders are never checked here.
- Duplicate intent ids are still blocked in `gate.py`.

## Still open

- Owner sign-off on the diff (D3).
- Stage 4 item 13 (`load_env_file` in `openalgo_client.py`) still calls Blue's loader.
- Stage 5 (remove Blue from the quant path) still waits for item 13 and stage 6.

## Test results (sandbox, 2026-11-06)

- `pytest tests/unit`: 27 failed, 424 passed, 18 skipped. All 27 failures are `httpx` missing
  (the HRP path via Blue's bridge, item 8, is still pending). The gate tests that needed `httpx`
  now run and pass, because the money checks no longer load Blue.
- `pytest tests/parity/test_money_checks_parity.py`: 6 passed.
