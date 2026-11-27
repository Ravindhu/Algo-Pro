# Full Auto soak-test checklist (sandbox, at least two weeks)

Plain English: Full Auto runs the bot in OpenAlgo's **sandbox** (analyze) mode every weekday.
It never trades real money. Before any live use, every box below must be ticked for at least
two weeks of weekdays. Live use is a separate decision by the owner and is not part of Full Auto.

Status: CHECKLIST (2026-11-27). Tick and date each line in the daily report review.

## Daily (each weekday)
- [ ] Start step ran between 08:45 and 15:10 IST (`full_auto_step` rows in the audit, all ok).
- [ ] Analyzer confirmed ON (`analyzer` step ok). If not, the day is a failure, not a retry.
- [ ] Broker and OpenAlgo login ok (`login` step). A cooldown is recorded as a failure.
- [ ] Screener.in session ok (`screener` step).
- [ ] Kill switch OFF at start (`kill` step ok). If ON, read the trip reason; do NOT reset
      without a written reason (kill switch is never reset automatically).
- [ ] Engine running in analyze mode (`engine` step ok, bot log says `--sandbox`).
- [ ] Day-end report written to `data/reports/<day>.md` at or after 15:45 IST.
- [ ] Report shows mode `sandbox` and the event counts look normal (no surprise kinds).

## Weekly
- [ ] No `full_auto_step` failures left unexplained.
- [ ] No kill-switch events without a human reason.
- [ ] Credential updates (if any) show `credential_update ok` in the audit, with no values in the log.
- [ ] `git status` clean for `data/` and no secrets in any report file (grep for `api_key`, `password`).

## Stop conditions (turn Full Auto OFF and investigate)
- Two failed start days in a row.
- Any step that reports the wrong mode (anything other than analyze).
- Any report or audit line that contains a secret value.
- The engine starts while Full Auto is OFF.

## Known limits (2026-11-27)
- A failed start is attempted once per day. To retry the same day, the owner restarts the day's
  check manually (not built yet).
- The start needs the dashboard process running. If the dashboard is closed, nothing runs.
- Screener ASM/GSM handling in sandbox is still strict; names may be dropped if the broker does not publish the flag.
- Live OpenAlgo has never been tested from the sandbox path.
