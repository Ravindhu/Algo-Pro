# 0034 - Portfolio shown as a popup (read-only holdings)

Date: 2026-11-09
Status: accepted (owner request 2026-11-09)

## Decision

1. Broker holdings (decision 0032) are shown in a **PORTFOLIO popup**, opened from a header button.
   The paper trading header and panel are not changed.
2. The popup shows: state badge (live broker read, not connected, error), summary tiles
   (holding value, invested, total P&L, return on invested, holdings count), an allocation donut
   (top 8 by value plus Other), a P&L bar chart, and a holdings table with return bars.
3. Read-only. No order controls. Refreshes every 30 s while open. The ₹ toggle masks money here too.

## Restore note

Decision 0032 and its module (`reporting/portfolio_view.py`), test, OpenAlgo `get_holdings`,
and `/api/portfolio` route were removed in commit `14d3bb9` (consolidated cleanup). They are restored
from `cfc349e` unchanged, except that the test route list now includes `/api/portfolio`.

## Consequences

- Real holdings appear only when OpenAlgo runs on the owner's machine and is logged in to the broker.
- Numbers are shown as the broker reports them. Missing values show as —. Nothing is estimated.
