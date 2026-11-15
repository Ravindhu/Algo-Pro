# Live hardening, 2026-11-15

Plain-English note on the live-readiness audit fixes. Live trading is still OFF.
`config/bot.yaml` keeps `mode: paper` and `live_enabled: false`.

## What was wrong and what changed

1. **Book-level risk caps were off when null.** A null cap switches its check off in the
   pre-gate, so live could start with no drawdown, CVaR, beta or sector check.
   Fix: the live start gate refuses while any of the four is null
   (`RiskLimits.unset_book_for_live`). No value was chosen for you.
2. **Daily P&L counted closed trades twice.** The open gain stayed in daily P&L and the
   realized gain was added on top. The strategy loss counter also received each change once
   per open position.
   Fix: each open position keeps its last mark (`_marks`). Only the change is added, for that
   position. A mark is reversed when the average entry changes or the position closes.
   Regression tests fail on the old code and pass on the fix.
3. **The start gate read the wrong kill-switch file.** It read `data/live/kill_switch.json`;
   the live bot writes `data/bot_live/kill.json`.
   Fix: the runner passes the bot's own kill file to the gate.
4. **Risk per trade differed.** `bot.yaml` had 0.25%; `settings.yaml` had 0.5% (owner, 2026-11-08).
   Fix: `bot.yaml` is 0.5%. A test keeps them equal.
5. **The static-IP check was optional.** Fix: live refuses unless `live.allowed_egress_ip` is set
   and matches the current egress IP. An IP that cannot be checked also refuses.
6. **No fixed live capital cap.** Fix: live refuses unless `live.max_capital_inr` is a positive
   number. Sizing equity is the lower of this cap and the broker's available cash.
7. **`availablecash` key.** Checked against the vendored OpenAlgo SDK
   (`src/quant/vendor/openalgo/account.py`). A missing key gives zero, so no entries.
   Still confirm on the live account.

## What the owner must set before any live run

In `config/risk_limits.yaml`: `max_sector_pct`, `max_beta`, `max_cvar`, `max_drawdown`.
In `config/settings.yaml` under `live:`: `allowed_egress_ip` and `max_capital_inr`.
Then the existing steps still apply: approval (`scripts/ops/approve_live.py`), the
SEBI checklist, and the kill-switch drill on the live host.

## Known gaps left open

- After a restart, open positions are not restored in the bot. Restored daily P&L may include
  their unrealized loss. This is conservative (it trips the loss limit sooner), not unsafe.
- Live analyze-mode soak (phase 2) has not run.

## Dashboard wiring (same day)

8. **One STOP reaches every engine.** The kill switch is now read from its file on every
   check, so a trip written by the dashboard stops a running bot at its next entry check.
   STOP writes three kill files (paper runner, paper bot, live bot); RESET clears all three.
   A file that cannot be read counts as ON.
9. **Risk per trade has one source:** `config/settings.yaml` (sizing). `bot.yaml` refuses a
   `risk_per_trade` key. The bot reads the setting at start, so a change applies on the next start.
10. **Live cap and whitelisted IP can be set from the dashboard setup panel** (owner sign-in).
    Values are validated (positive rupees; IPv4 address) and saved under `live:` in settings.yaml.
11. **Live readiness is shown in the setup panel** (`GET /api/live/status`, owner only). It lists
    exactly what the live start gate still refuses.

Not changed: the live start still needs your values, an approval, the SEBI checklist, the
analyze-mode soak and the kill-switch drill on the live machine.
