# 0031 - Live switch on the dashboard (one switch, with a checklist)

Date: 2026-11-09
Status: ACCEPTED (owner answers 2026-11-09). Supersedes decision 0028 part 5.

## Context

Decision 0028 said the dashboard can never enable live. The owner now wants the NSE dashboard
button to be the only paper-to-live switch. Live orders still stay behind the guard in decision 0017.

## Decision

1. The PAPER / LIVE button on the dashboard is the only way to change the mode. A test scans the
   code and fails if anything else writes the mode (tests/unit/test_trading_mode.py).
2. LIVE is accepted only when all three hold:
   - the confirm phrase `GO LIVE` is typed exactly,
   - a named approver is typed (the approval is made by that person at that moment; code never creates one),
   - all eight checks in `core/live_checklist.py` pass (see docs/LIVE_RUN_DESIGN.md section 4).
3. Any fact that is not verified (None) fails its check. Unreadable facts refuse live.
4. PAPER is always accepted, with no checklist. Restart returns to PAPER.
5. On switch to PAPER, open live positions are LEFT OPEN (owner answer, D5). New entries stop.
6. Muhurat special sessions are NOT traded (owner answer, D6).
7. `max_symbols_per_portfolio` = 8 (owner answer, D4). Applies to paper and live.
8. Every request, accepted or refused, is audited with its checks and the approver.

## Not yet decided

- D3: a licensed live market-data source. Until it is approved in `config/data_sources.yaml`, the
  licensed-data check fails and LIVE is refused. This is deliberate.

## Facts not yet read by the dashboard (fail closed until wired)

OpenAlgo mode, safe mode, daily loss breach and reconciliation status. They show as "not checked".
