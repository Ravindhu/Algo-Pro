# Implementation plan: SVG end-to-end, phases 1 to 5

Last updated: 2026-11-09. Branch: `main`. Written in simple English.
Decisions: docs/decisions/0028 (part 5 superseded), 0031. Live design: docs/LIVE_RUN_DESIGN.md.

## Owner answers applied (no open questions for the owner from this plan)

| Item | Answer | Where applied |
|---|---|---|
| Branch | main | all commits |
| Live switch | Dashboard button only | `core/trading_mode.py`, `dashboard/app.py` |
| Confirm phrase | GO LIVE | `core/trading_mode.py` |
| Approver | Typed by the person on each LIVE request (not stored in config) | dashboard, audited |
| max_symbols_per_portfolio | 8 | `config/risk_limits.yaml` |
| On switch to PAPER | Leave open positions | decision 0031 |
| Muhurat session | Not traded | decision 0031 |
| Holiday calendar | From NSE circular (16 equity holidays, two files agree) | `config/calendar/holidays.csv` |

## Phase status

### Phase 1 - Paper bridge: DONE
- Conviction tiers in sizing. Off in config. When off, sizing is unchanged (tested).
- Event blackout feed: `run_paper --events`, entries carry `trade_day`, audit records whether a calendar was loaded.
- Tests: `src/quant/tests/test_phase1_bridge.py` (23).

### Phase 2 - Safety and recovery: MOSTLY DONE
- Done and tested: duplicate intent block after restart, halt freeze and resume, kill switch survives restart,
  recovery of a dead process, kill-close does not run twice.
- Open: a full kill-and-restart run of the paper runner (X-7). Next session.

### Phase 3 - Post-trade and monitoring: PRESENT, NOT AUDITED
- Modules exist: reconciliation, TCA, drift, parity, strategy health, alerts.
- Not run end to end here. Quality not audited.

### Phase 4 - Missing stages: PARTLY DONE
- Present: cash and T+1 settlement, tax lots (FIFO), replay, capacity, costs, validation.
- Open: beta enforcement in the gate (not verified); feed heartbeat and gap monitor (not verified).
- Not built: global cues (SGX/GIFT Nifty, USDINR, crude), bulk and block deals, index-futures hedge overlay.
  Global cues and bulk/block need a data source first.

### Phase 5 - Live path: CHECKLIST AND SWITCH DONE, LIVE STILL REFUSED
- Done (decision 0031):
  - eight-check live checklist (`core/live_checklist.py`), pure, unknown facts fail closed
  - facts gatherer from local files (`core/live_facts.py`)
  - one switch on the dashboard, GO LIVE phrase, approver typed per request, every request audited
  - restart returns to PAPER; a test blocks any other code from writing the mode
- Tests: `src/quant/tests/test_live_checklist.py` (33), `tests/unit/test_trading_mode.py` (10).
- Still open, in order:
  1. D3: approve a licensed live data source in `config/data_sources.yaml`. Until then LIVE is refused.
  2. Wire the readers for OpenAlgo mode, safe mode, loss breach and reconciliation (now "not checked" = refused).
  3. Analyze-mode run through OpenAlgo (needs `httpx`, not installable in this sandbox).
  4. Live runner using the same code path as paper, with only the broker adapter changed. Not built.

## Verification (2026-11-09)

- Full quant suite `src/quant/tests`: 482 passed, 13 skipped, 1 xfailed.
- `tests/unit` plus `tests/parity`: 1867 passed, 3 skipped, 1 failed.
  The failure `test_upgrade_pin::test_repo_pin_matches_vendored_copy` also fails on `master`. It predates this work.
- Not run here: `dashboard/app.py` endpoint (needs `httpx` for the test client). It compiles.
  The endpoint is covered by the checklist and mode tests, not by an HTTP test. Needs a CI run.

## Corrections made in this plan

- Live risk limits are NOT null. Decision 0016 sets live equal to paper. Only `max_symbols_per_portfolio` was unset, now 8.
- The strategy-to-signals bridge was already built. The earlier "missing" list was wrong for many stages.

## Next session, two priorities

1. D3: approve a licensed live data source (blocks live). Then wire OpenAlgo, safe mode, loss and reconciliation readers.
2. X-7: a paper-runner kill-and-restart test. Then check beta enforcement in the gate.

## Work log

- 2026-11-09: merged master into main; corrected inventory; phase 1 bridge (code and tests).
- 2026-11-09: live run design (docs/LIVE_RUN_DESIGN.md).
- 2026-11-09: owner answers applied; live checklist and one-switch endpoint; decision 0031; plan rewritten.
  Note: the plan was written after the first code change, which breaks the plan-first rule. Recorded for transparency.
