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
and the kill-switch drill on the live host.

## Known gaps left open

- Fixed 2026-11-15 (second pass, see below): open positions, broker stop ids and open exposure
  are saved and restored at start. Parents still being sliced are not restored.
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

Not changed: the live start still needs your values, an approval, the
analyze-mode soak and the kill-switch drill on the live machine.

## Second pass, 2026-11-15 (audit items 1-10)

12. **Broker-side stop-loss.** Each open position gets an SL-M stop at the broker when it opens,
    sized to the full quantity and triggered at the bot's stop price. It is replaced when the
    position grows, cancelled before the bot sells, and checked every cycle. A stop that fills
    closes the position in the bot's books. A failed stop blocks new entries until it is placed.
13. **Restart-safe book.** Open positions, stop ids, exposure, marks, and today's parents are
    saved to `data/bot_live/positions.json` on every change and restored at start, before the
    broker reconciliation. The live runner now also passes the P&L store (`pnl_state.json`).
14. **One live entry point.** `run_live.py` main() refuses to run; the bot runner is the only path.
15. **LIMIT entries.** Entries are limit orders `entry_max_slippage_bps` (default 20) above the
    last closed close. Unfilled entries are cancelled. A partial fill is flagged and blocks entries.
    Exits and stops stay MARKET/SL-M so they always try to fill.
16. **Egress IP every cycle.** A changed or unknown public IP blocks new entries; exits still run.
17. **Exchange on every order.** The client now sends `exchange` (NSE default). Before this, OpenAlgo
    would reject every order because the field was missing.
18. **SEBI items removed** from the live gate messages, config comments, live plan docs, and the
    checklist file. The SEBI turnover fee in the cost model is kept; it is a real charge.

Still owner items: book caps, egress IP, capital cap, approval, analyzer soak, kill-switch drill,
and confirming the current retail algo rules with the broker. Live stays off.
