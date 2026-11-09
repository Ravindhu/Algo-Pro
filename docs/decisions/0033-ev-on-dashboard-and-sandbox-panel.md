# 0033 - EV shown on the dashboard; SANDBOX panel restored

Date: 2026-11-09
Status: accepted

## Decision

1. **EV is shown** on the main dashboard. EV = mean P&L per closed paper trade.
   The EV ban in decision 0008 is lifted. The owner removed the EV line from
   `AGENTS.md` rule 8 on 2026-11-09.
2. **MAE/MFE and rule compliance stay off the dashboard.** The owner's edit removed
   the whole ban line, but only EV was requested to change. Those two remain off until
   the owner says otherwise.
3. **The SANDBOX panel is restored** on `dashboard/main_dashboard.html`. It was removed
   by a later commit (`cfc349e`) without a decision note.

## Numbers shown (paper ledger only)

| Stat | Meaning |
|---|---|
| EV per trade | mean P&L per closed trade (money) |
| EV % per trade | mean return per closed trade |
| Avg win / avg loss | mean P&L of winning / losing trades |
| Risk : reward | avg win / |avg loss| |

EV equals `win_rate * avg_win + (1 - win_rate) * avg_loss`. Hand-checked in
`tests/unit/test_dashboard_metrics_handchecked.py`.

## Statistical guard

Below 30 closed trades the SANDBOX note says EV is not statistically reliable.
The number is still shown, so the owner can see it, but it is labelled.

## Flow and ownership

- Flow stage: Dashboard (`src/quant/reporting/`, `dashboard/`).
- Changed: `reporting/dashboard.py` (`expectancy`, `build_payload`),
  `reporting/dashboard_data.py` (performance payload), `dashboard/main_dashboard.html`.
- No new route. No order path. No change to `dashboard/app.py`.

## Consequences

- Any future request to show MAE/MFE or rule compliance still needs a new decision.
- EV from backtests is not shown here. Dashboard EV is paper-trade EV only.
