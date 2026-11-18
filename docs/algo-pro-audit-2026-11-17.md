# Algo-Pro Quant Audit (read-only)

Repository: Ravindhu/Algo-Pro
Master head audited: a4b1587
Date: 2026-11-17
Scope: money-path code (backtest engine, costs, metrics, loops, risk engine, POV sizing, ledger, conviction, tiers, owner limits, config/risk_limits.yaml).
Not read: strategies, RiskGate, runner, ledger_store. Test suite not run.
No changes were made to the repository.

## What holds up

- Backtest fills happen at the next bar's open. The strategy only sees bars up to its decision bar. No look-ahead found in the engine.
- Conviction sizing uses a geometric mean, treats missing data as zero, and can only shrink size under the hard caps.
- Owner-limit merge keeps the stricter value and blocks entries if the file cannot be read.
- Live trading is still blocked: the four book-level caps are null. Monetary caps are set (10 shares, Rs 10,000 per order, Rs 20,000 open exposure, Rs 1,000 daily loss).

## Issues, most important first

1. Two cost models disagree. costs.py (simulator) ignores the `slippage` config key and uses its own 5 bps. cost_aware.py (planner) counts 5 bps per side. Changing `slippage` moves the planner but not the backtest.
2. Brokerage modelled as 3 bps. Many Indian brokers charge a flat Rs per order, far above 3 bps on small tickets. STT differs by product: 0.1% delivery both sides; 0.025% intraday sell.
3. Profit factor uses gross P&L; win rate, wins, losses and average trade use net P&L. A trade that wins before costs and loses after is counted differently in each metric (metrics.py).
4. Owner-null semantics contradict. risk_limits.yaml header: null rejects entries. Book-cap comment: null switches the check off. owner_limits.py: null adds nothing and the gate's own policy applies. Needs confirmation in RiskGate.
5. Owner limits reach only the blue runner (runner.py). Any other order path would use looser master.json caps.
6. Risk-engine slippage units ambiguous. Default max_allowed_slippage_pct = 0.005, compared directly with observed_slippage_pct. If callers pass percent, the gate trips on every trade.
7. Veto rationale truncated to 15 words. Full breach list is kept, so no data lost; summary can hide the reason.
8. Missing day P&L may pass as None into RiskGate. Confirm RiskGate rejects None (fail closed).
9. Slippage reconciler only moves slippage up and calibrates on BUY fills only. Conservative but biased.
10. POV slippage penalty has no upper bound. Penalty above 1 would raise the rate under slippage.
11. Ledger race: record_child writes avg price to the last child in the list. With concurrent children this can hit the wrong child.

Smaller items:
- Open positions marked without exit cost (slightly optimistic equity and drawdown).
- Sortino docstring describes a different downside-mean formula from the code (code matches the standard definition).
- SELL fill price uses 1/(1+x) instead of 1-x (negligible).

## Open decisions (needed before fixing items 2 and 4)

- Brokerage: flat Rs per order or percent? Delivery or intraday?
- Null caps: reject all entries (fail closed), or switch only that check off?

## Status

Fixes were not applied. The branch audit-fixes-2026-11-17 could not be created because the GitHub connector returned 403 (no write access).
