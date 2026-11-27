# Implementation plan: Full Auto button (2026-11-27)

Flow stage: Dashboard -> start-up orchestration (owner-controlled; sandbox first)
Status: PLANNED (no code yet beyond this plan)

## Owner decisions (2026-11-27)
1. First run destination: SANDBOX (OpenAlgo analyzer ON, fake money, real prices).
2. Kill switch is NEVER reset automatically. A trip stays until the owner resets it on the dashboard.
3. Credentials can be updated from the dashboard, behind 2FA, audited, never echoed.

## What the button must do (owner list)
1. Start the engine each day.
2. Sign in to OpenAlgo and the broker (TOTP), and sign in to Screener.in.
3. Update credentials from the dashboard (2FA gated).
4. Write a daily report at day end.
5. Bot in live-mode code path, but OpenAlgo analyzer ON (sandbox). For the first run this is the sandbox.
6. Kill switch: manual reset only (decision 2).
7. Daily re-login after the broker session expires.

## Gaps found in the code (verified 2026-11-27)
- G1. No runner sends orders through OpenAlgo's sandbox. `AnalyzeSender` exists in
  `execution/strategy_bot.py`, but `run_strategy_bot_live.py` only builds `LiveSender`, and
  `run_strategy_bot.py` is paper-only. `supervise_bot.py` offers only paper and live.
- G2. `LiveSender` (and the live order guard in `openalgo_client.py`) refuses while the analyzer
  is ON. So "live code path + sandbox" needs an analyze runner, not the live runner.
- G3. `bot_mode()` in `engine_control.py` returns only paper or live. It needs an analyze value.
- G4. Daily report: no report writer exists yet.
- G5. Credentials from the dashboard: no 2FA-gated write route exists yet.
- G6. The Full Auto sequence needs a state machine with stop points and an audit trail.

## Phases
Phase 1 (sandbox runner, the blocker for decision 1):
  a. Analyze runner that builds `AnalyzeSender`, refuses unless analyzer is confirmed ON.
  b. Engine mode `analyze` in `bot_mode()` and `supervise_bot.py --mode analyze`.
  c. Tests: refuses with analyzer OFF or unknown; sends only through the sandbox client.
Phase 2 (Full Auto sequence, one button):
  a. Ordered steps: analyzer check -> OpenAlgo + broker login -> API key sync -> Screener.in session
     -> engine start (analyze). Each step audited; the first failure stops the sequence and says why.
  b. Stop button stops the engine, and leaves the sign-ins as they are (owner: "same as before").
  c. Scheduled trigger: the owner's daily time, only when Full Auto is on.
Phase 3 (kill switch and reports):
  a. Trip stays until a manual reset (no auto reset). Full Auto shows the trip reason.
  b. Daily report at day end (ledger, fills, rejects, kill events), saved to data/reports/.
Phase 4 (credentials from the dashboard):
  a. Write route behind owner sign-in plus a fresh TOTP, audited, values never echoed.
  b. Writes only to _restored/.env, never creates the file.
Phase 5 (soak test): two weeks in sandbox before any live decision.

## Safety rules kept
- Live stays OFF until the owner decides after the soak test (config/bot.yaml is not changed here).
- No automatic kill switch reset.
- No secret in logs, reports or audit.


## Status update (2026-11-27, implementation)
- Phase 1 (sandbox runner, analyze mode): DONE. Commit 96e78e0.
- Phase 2 (daily start, switch, scheduler tick, wiring): DONE. `src/quant/core/full_auto.py`, `src/quant/core/full_auto_steps.py`, dashboard `POST/GET /api/full-auto`, Infrastructure button. Tests: tests/unit/test_full_auto.py.
- Phase 3 (day-end report): DONE. `src/quant/reporting/daily_report.py`, written from the tick. Tests: tests/unit/test_daily_report.py.
- Phase 4 (credential update with 2FA): DONE. `src/quant/core/credential_update.py`, dashboard `POST /api/credentials`, UI in Infrastructure. Tests: tests/unit/test_credential_update.py.
- Phase 5 (soak checklist, live refused): checklist DONE in docs/FULL_AUTO_SOAK_CHECKLIST.md. Live refusal holds by construction (START_MODE is analyze only). Soak itself is NOT done: needs two weeks of sandbox days.
- Owner decisions kept: sandbox first; kill switch never reset automatically; credential updates from dashboard with 2FA.
- Not merged to master. Merge waits for owner approval.
