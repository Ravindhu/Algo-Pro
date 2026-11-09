# Live run design: one switch, on the dashboard

Status: DESIGN, answers applied 2026-11-09. Checklist and one-switch endpoint implemented (decision 0031). Live still refused: D3 (licensed data) open. See docs/IMPLEMENTATION_PLAN_SVG_E2E.md.
Written in simple English.

## 1. What the owner asked for

1. The holiday calendar comes from the NSE circular. This is already done.
2. Paper to live is switched in one place only: the LIVE / PAPER button on the NSE dashboard.
   No env var, config file, command-line flag or API call switches the mode on its own.

## 2. Facts checked in the repo (2026-11-09)

- Holiday calendar: `config/calendar/holidays.csv` (the file the code reads) and
  `config/calendar/nse_holidays_2026.csv` (circular source, with an `equity_holiday` flag)
  agree exactly: 16 equity holidays, no differences.
- Next trading day check: 2026-11-10 (Diwali Balipratipada) is an equity holiday. Live must not start that day.
- Muhurat session 2026-11-08 was a Sunday special session. Paper does not trade it. Live default: same.
- Current mode code (`src/quant/core/trading_mode.py`, decision 0028): live is ALWAYS refused. The dashboard
  cannot switch to live. This design changes that rule, so decision 0028 part 5 must be superseded.
- Live guard (decision 0017, `execution/openalgo_client.py`): needs a LiveApproval with an approver, a
  limits-file SHA-256 that still matches, an approved GateDecision for the intent, and an API key.
- Live limits: decision 0016 sets live = paper values, in the same file `config/risk_limits.yaml`.
  CORRECTION to the earlier summary: live limits are NOT null. Only `max_symbols_per_portfolio` is unset,
  so paper AND live refuse every entry until the owner sets it.

## 3. The one switch

- The dashboard header has the only mode control: PAPER and LIVE.
- The backend keeps the mode in memory only. Restart returns to PAPER (decision 0028 part 4 stays).
- The mode is changed only through `POST /api/trading-mode` (token required). No other code path may
  set it. A test will fail if any other code writes the mode.
- Switching to PAPER is always allowed, with no approval and no checklist. It stops new entries at once.
- Every request, accepted or refused, is written to the audit log with the time, the token user and the reason.

## 4. Going to LIVE: the checklist (all must pass, or the switch is refused)

| # | Check | Refused when |
|---|---|---|
| 1 | Calendar verified and today is an NSE equity trading day | empty calendar, weekend or holiday |
| 2 | Inside the NSE cash session 09:15 to 15:30 IST | outside the session |
| 3 | Live data source chosen and licensed (`config/data_sources.yaml`) | still INFY or test-only data |
| 4 | LiveApproval exists with an approver for the current limits hash | approval missing, blank approver, or hash changed |
| 5 | `max_symbols_per_portfolio` is set (non-null) | unset (today: unset) |
| 6 | OpenAlgo reachable and in the expected mode (analyze first, then live) | unreachable or wrong mode |
| 7 | Kill switch off, safe mode not HALT_ALL, daily loss breach not active | any of these active |
| 8 | Reconciliation of positions and funds is clean since last close | any mismatch |

The dashboard shows each check with a pass or fail reason. The button stays disabled until all pass.

## 5. Confirming the switch

- Two steps: click LIVE, then type the approver name and the phrase `GO LIVE`. Both must match the approval record.
- The backend re-runs the checklist at the moment of the click. The page result is not trusted.
- On success the mode is LIVE and the audit log gets one record with the checklist results and the limits hash.

## 6. Inside LIVE

- One code path for paper and live (LIVE plan rule 1). Only the broker adapter changes.
- Every order passes the risk gate and the live guard (decision 0017). A refused order is never resent.
- Orders only inside the session. Kill switch and daily loss stop stay on, as in paper.
- A visible STOP button always works. It sets PAPER and stops new entries.
- Open live positions when switching back to PAPER: default is NO automatic exit. Owner decides (D5).

## 7. Tests to write before build (no live order is placed by any test)

1. Mode cannot change except through the token endpoint (static check on the mode writer).
2. LIVE refused for each checklist item in section 4 (one test each).
3. LIVE allowed only when all eight pass. Use a fake broker and an approval file.
4. A changed limits file after approval stops live at once.
5. Holiday and out-of-session refusals, using `2026-11-10` as the holiday case.
6. Restart returns to PAPER.
7. PAPER switch always accepted, even with live open.
8. Every refusal and accept is written to the audit log with the reason.

## 8. Owner decisions needed (D1 to D6)

- D1. Supersede decision 0028 part 5 so the dashboard button can ARM live (with the checklist), and write it as decision 0031.
- D2. Who is the approver, and the confirmation phrase (proposed: `GO LIVE`).
- D3. Licensed live data source (required for check 3).
- D4. Value of `max_symbols_per_portfolio` (required for check 5).
- D5. On switch to PAPER: leave open live positions, or close them. Default: leave.
- D6. Muhurat special session: trade or not. Default: not.

## 9. Build order after D1 to D6 are answered

1. Decision 0031 and this design, approved.
2. Checklist module and the token endpoint change, with tests from section 7 (no broker call).
3. Analyze-mode run through OpenAlgo (checks 6 and 8 for real).
4. Live runner with the same code path. Live stays refused until checks 3 to 5 pass.

Live is NOT enabled by this document.
