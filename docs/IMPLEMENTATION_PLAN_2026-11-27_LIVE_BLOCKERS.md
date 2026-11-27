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

## Phase 3 - partly done (this commit)
Done:
- Items 2, 13 (kill switches): one shared check, quant.risk.kill_switch.operator_kill_active(),
  over the three owner kill files (paper, paper bot, live bot). It is called inside
  engine_control.start_engine (so the dashboard button, start_engine_now and full-auto are all
  covered) and in the supervisor preflight (owner_kill_gate). Before this, the button and
  start_engine_now checked only the paper file, the preflight checked only the optim-policy switch,
  and the live kill file stopped nothing at start.
- Item 9 (paper universe): the paper runner now passes the same M and A switch files as live.
- Item 1 (screen clock): the re-scrape check judges age by the screen's as_of (IST), not by mtime.
  A stamp more than 5 minutes in the future counts as stale (clock skew tolerance).
- Item 12 (symbols): SYMBOL_RE requires a letter for NSE symbols, or exactly six digits for BSE.
- Item 17 (restore retention): pre-restore-* folders are pruned to the newest 30 after each restore.
- Tests: tests/unit/test_phase3_safety.py (28 tests). Existing test test_screener_once_per_day
  now uses the real clock (a fixed past date only worked with the old mtime logic).
- Full suite: no new failures against master.

Not changed in this commit (needs a decision, or the live machine):
- Items 10, 11 (screener session): _saved_today reads os.environ, and the login marker is mtime
  based on purpose (it records the login time). Changing it touches the broker-cooldown login path,
  so it needs a dedicated change with a test for a real double-login.
- Items 19 (key), 16 (.env path): fetch_key infers has_api_key, and refresh_session writes
  _restored/.env while readers load the repo-root .env. Both need the real OpenAlgo /apikey reply
  and an owner decision on the file location, given the backup rules.
- The full-auto kill step still takes a single path argument; it is covered by the engine-level check.

## Phase 4 - done (this commit)
Findings (verified by running, 2026-11-28):
- Item 14 (session key never rotates) and tests 1-2: src/quant/core/key_rotation.py is on master,
  and its route test passes. The old remote branch feature/auto-key-rotation-2026-11-25 is no
  longer on origin, so it was merged or deleted; no separate action needed.
- Test 3 (/api/overview flow_map) and test 4 (POST /api/screener/refresh scrapes now): pass with
  FastAPI installed. The audit saw them failing on an older state.
- The audit's "5 failed in tests/unit" are fixed on master: tests/unit has no failures now.
- Test fix: test_owner_refresh_scrapes_now_and_returns_result asserted exactly one scrape. A
  background sign-in job from another test can scrape in the same window (timing-dependent), so it
  now asserts at least one scrape, which is what the rule requires. Stable over 3 repeats.

IMPORTANT correction to earlier reports: the 105 failures I carried as "pre-existing" were
ENVIRONMENT gaps in the sandbox (websocket-client, openalgo==2.0.3 pinned in requirements.txt,
fastapi, and sqlalchemy), not code bugs. With the pinned dependencies installed, the full suite on
this branch is: 2840 passed, 3 skipped, 1 xfailed, 0 failed.

Ops (no change in this repo):
- "runner not found at C:\\Users\\Admin\\...\\run_preopen_once.py": that file is not in this repo. The
  trading PC runs an older copy. Pull the current master on that PC.
- "feed auth failed": broker or OpenAlgo session has expired. The owner signs in at /broker.

Open for the owner:
- sqlalchemy is imported by src/quant/engines/market/sqlite_downloader.py but is NOT in
  requirements.txt. Add it with a version pin before a fresh install.
- Test paths named in the audit (tests/parity, blue_engine/tests) no longer exist.

## Status
- Phase 0: done. Phase 1: done. Phase 2: partly done. Phase 3: partly done (items 10, 11, 16, 19 open). Phase 4: done. Full suite on the branch: 2840 passed, 0 failed.
- Items not verifiable from this sandbox: NSE/broker endpoints, OpenAlgo routes (bugs 15, 16).
