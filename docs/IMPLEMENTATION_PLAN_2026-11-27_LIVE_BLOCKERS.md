# Implementation plan: live blockers (2026-11-27)

Branch: fix/live-blockers-2026-11-27 (from master a9cb41b). Not merged to master.
Source: owner audit screenshots, "Bugs that block live trading" (items 8-20),
"Gates" (1-7), "Bugs read directly" (1-19), failing tests (1-4), ops notes.

## Phase 0 - Safety (done on this branch)
- config/bot.yaml: mode paper, live_enabled false. Live stays off until phase 5.
  Reason: master still had mode live with the blockers below open.

## Phase 1 - Process safety (one live bot, reliable stop)
- Bug 8, 20: single-instance lock; the bot writes its own PID; wrapper PID not used.
- Bug 13: stop waits for exit (psutil), PID removed only after exit is confirmed.
- Bug 1 (direct): remove dead start_engine() spawn path.
- Bug 15: start passes --mode to the supervisor; refuse start if mode missing.
- Bug 16 (direct): source_mode default "off/off" fails closed to paper.

## Phase 2 - Fail-open paths
- Bug 10, 13: sandbox band disabled in live; no quote limits = no order.
- Bug 11, 14: ASM/GSM read from the real route; unknown flag = block + log.
- Bug 12, 18: preflight uses a tradable equity or broker index-quote, not NSE:NIFTY.
- Bug 3, 4: reconciliation mismatch stops the bot + alert; missing cash = refuse, loud.
- Bug 5, 6, 7: analyzer/feed/risk-hash refusals shown on dashboard and alerted.
- Bug 19: margin_for_order validates BUY/SELL.

## Phase 3 - Paper and live parity
- Bug 9: paper runner uses the same universe loader as live.
- Bug 2, 13 (kill switch): one kill-switch file for dashboard, startup checks, engine.
- Bug 1 (clock): one screen-freshness clock.
- Bug 10 (env): one config reader for session cookie.
- Bug 11 (session): credential check on every start.
- Bug 12 (symbols): symbol regex requires a letter and sane length.
- Bug 19 (key), 16 (.env path): write .env where readers read it.
- Bug 17: restore retention guard fixed.

## Phase 4 - Security and tests
- Bug 14 + tests 1-2: session key rotation (branch feature/auto-key-rotation-2026-11-25 to be reviewed, not merged here).
- Tests 3: /api/overview keeps flow_map key.
- Test 4: screener refresh route contract decided, code or test fixed.
- Full pytest on all four paths before any merge.

## Phase 5 - Live sign-off (owner only)
- Paper/analyze soak on the trading PC, kill-switch drill, host guard confirmed.
- Owner switches mode and live_enabled. Not done by the bot branch.

## Phase 1 - done (this commit)
- One spawn path: the dashboard button and start_engine both call `_spawn_supervisor`. The
  start_engine_now runner is no longer used by the button.
- Start lock (`data/run/engine.lock`, O_EXCL) held from the running check until the PID file
  is written. A second press in that window is refused. A dead or old lock is cleared.
- PID file written at once with the supervisor's PID (was: the wrapper's PID, written late).
- Stop: signals the supervisor's process group on POSIX (reaches the bot child), waits for
  exit, and removes the PID file ONLY after exit is confirmed. If the engine survives, stop
  is refused and the PID file is kept.
- Windows: `tasklist` PID match is exact; stop uses `taskkill /T` (no /F). NOT tested on
  Windows from this sandbox. If taskkill does not stop a detached bot, stop is refused and
  the owner must check it.
- Screener login moved from the button's runner into supervise_bot.py (non-fatal).
- Tests: tests/unit/test_engine_phase1.py (9 tests). Full suite: same 110 pre-existing
  failures/errors as master (tail-risk tests, and 5 files that need the `openalgo` package
  missing in this sandbox). No new failures.
- Not done in Phase 1: bug 16 source_mode default (moved to Phase 2/3). The supervisor has no
  SIGTERM handler of its own; the bot shuts down through the process-group signal. Not verified
  against a live bot.

## Phase 2 - partly done (this commit)
Done:
- Items 10, 13 (sandbox band): screener rows carry band_source; read_screen refuses
  'sandbox_default' rows unless the run is analyze (the sandbox runner passes allow_sandbox_band).
  A fallback band written while the analyzer was ON can no longer price a live order.
- Items 12, 18 (preflight): build_gates probes RELIANCE on NSE, not the index NSE:NIFTY.
- Item 19 (margin_for_order): direction must be BUY or SELL, else ValueError, nothing sent.
- Tests: tests/unit/test_phase2_live_safety.py (10 tests). One existing test expectation
  updated for the new band_source field (src/quant/tests/test_pre_gate.py).
- Full suite: same 110 pre-existing failures/errors as master; no new failures.

Not changed in this commit (needs owner decision or the live machine):
- Items 11, 14 (ASM/GSM route): the vendored OpenAlgo has NO ASM/GSM endpoint. An unknown flag
  already drops the symbol (fails closed), so this blocks trades but cannot send a bad order.
  The real broker field must be confirmed on the trading PC before changing the source.
- Items 3, 4 (reconciliation and cash): entries are already blocked on mismatch and sized 0 on
  missing cash (fail closed). Stopping the bot and alerting is a design change: exits must keep
  running. Owner decision pending.
- Items 5, 6, 7 (visible refusals): analyzer, feed and risk-hash refusals work correctly but are
  not yet shown on the dashboard or alerted. Dashboard work, next step.

## Status
- Phase 0: done. Phase 1: done. Phase 2: partly done (items 3-7 and 11/14 open, see above). Phases 3-4: not started.
- Items not verifiable from this sandbox: NSE/broker endpoints, OpenAlgo routes (bugs 15, 16).
