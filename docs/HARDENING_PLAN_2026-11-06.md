# Hardening plan - 2026-11-06

Plain-English plan for the owner's two decisions from the audit.
Scope: no new trading logic. Only safety and records. No live orders are enabled.

## Owner decisions (recorded in decision 0017)

1. **P&L stays visible.** The dashboard keeps P&L on by default. No code change.
2. **Data.** INFY (Blue_engine sample data) is for testing only. It is never used for live
   trading. A licensed live data source is needed before any live path, and the owner must choose it.

## Work items

| ID | Item | Stage (flow v5) | Done when |
|---|---|---|---|
| H-1 | Record decision 0017 (P&L visible, INFY test-only) | docs | decision file exists |
| H-2 | Live order client must receive a `GateDecision` that is approved and matches the intent | Execution / OpenAlgo | unit tests for block and pass paths |
| H-3 | Live approval must be a typed `LiveApproval` object (approver, date, limits hash), not a string | Execution / OpenAlgo | unit tests |
| H-4 | Layout test also catches the order path string `/placeorder` outside the client file | Tests | test fails if the string is added to another stage file |
| H-5 | `config/data_sources.yaml` marks INFY as `live_allowed: false` | Data | test checks the file |

## Order of work

H-1 -> H-2 and H-3 together -> H-4 -> H-5 -> run tests -> update status note -> commit.

## Test limits in this sandbox

`httpx` cannot be installed here (PyPI blocked). Gate tests that need Blue_engine
(`test_phase1_risk_exec.py`, gate cases) fail here for that reason only. They must run on the owner machine.
Client, layout and config tests run here.

## Exit check

- All new tests pass here.
- Gate tests pass on the owner machine (`pip install -r requirements.txt`).
- Status note and README updated in the same commit.
