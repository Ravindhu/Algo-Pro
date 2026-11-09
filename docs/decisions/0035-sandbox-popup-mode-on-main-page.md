# 0035 - Sandbox is a view-only popup; mode switch stays on the main page

Date: 2026-11-09
Status: accepted (owner request 2026-11-09)

## Decision

1. The PAPER/LIVE switch stays on the main page header. It is the only place that changes trade mode.
2. The paper trade history (SANDBOX) moves into a **SANDBOX popup**, opened from a header button.
   It is view only: stats, open positions, closed trades, order history, equity curve.
3. The PORTFOLIO popup (decision 0034) is also view only.
4. The main page keeps Risk, Safety events and Limits.

## Consequences

- No mode control is inside a popup, so changing the trade mode always goes through the main
  header and the existing token and live-approval checks (decisions 0017, 0031).
- The popup reads the same overview as before. No new routes.
