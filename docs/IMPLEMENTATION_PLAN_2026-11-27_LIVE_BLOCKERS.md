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

## Status
- Phase 0: done. Phases 1-4: not started in this commit.
- Items not verifiable from this sandbox: NSE/broker endpoints, OpenAlgo routes (bugs 15, 16).
