# Quant Research & Engineering Rules (`quant-rule.md`)

## 1. Data Integrity & Bias Prevention
- **Zero Look-Ahead Bias:** Strictly lag all features (`shift(1)`) before signal generation. Never use future highs, lows, closes, or point-in-time restated fundamentals to compute historical signals.
- **Survivorship Bias:** Always test strategies on point-in-time constituent universes (including delisted assets) rather than current index constituents.
- **Timestamp Alignment:** Normalize all timestamps to UTC. Enforce strict `as-of` joins (`merge_asof(..., direction="backward")`) when combining multi-frequency or alternative datasets.

## 2. Backtesting & Validation Standards
- **Train / Validation / Test Split:** Lock away an out-of-sample (OOS) holdout set before writing strategy code. Touch the OOS set once; never tune hyperparameters on OOS results.
- **Walk-Forward & Purged Cross-Validation:** Use expanding/rolling walk-forward windows or purged K-fold CV with embargo periods to prevent label leakage in overlapping horizons.
- **Realistic Friction:** Every backtest must explicitly deduct:
  - Exchange/broker commissions and regulatory fees (e.g., STT, stamp duty, exchange turnover charges, tax).
  - Bid-ask spread and volume-dependent market impact / slippage.
- **Execution Realism:** Assume signal execution at `Next Open` or `VWAP`—never at the same bar's `Close` used to generate the signal.

## 3. Statistical & Overfitting Guardrails
- **Economic Thesis First:** Require a written hypothesis explaining *why* the edge exists (e.g., structural flow, risk premium, behavioral bias) before running parameter sweeps.
- **Parameter Parsimony:** Limit free parameters to $\le 3$ per signal. Reject narrow parameter peaks; valid edges must show plateau stability across neighboring parameter values.
- **Multiple Testing Adjustment:** Report Deflated Sharpe Ratio (DSR) or Bonferroni/Holm-adjusted p-values whenever more than 5 strategy variations are tested.

## 4. Portfolio Construction & Risk Controls
- **Volatility-Adjusted Sizing:** Scale position sizes inversely to realized asset volatility (e.g., ATR or EWMA volatility) rather than fixed notional amounts.
- **Hard Risk Limits (Non-Overridable in Code):**
  - Max single-position exposure cap (e.g., $\le 10\%$ of NAV).
  - Max gross/net leverage ceiling.
  - Automated daily loss limit and max drawdown circuit breaker (kill switch).
- **Correlation Checks:** Measure marginal portfolio correlation before adding a new signal; reject signals that merely replicate existing beta or factor exposure.

## 5. Production & Code Hygiene
- **Parity Between Research and Live:** Share a single feature-engineering and signal-generation library between backtesting and live execution engines to eliminate drift.
- **Deterministic Runs:** Pin random seeds, library versions, and dataset snapshots for every experiment.
- **Fail-Safe Execution:** Default to flat/cancel-all on missing market data, stale heartbeats, or broker API timeouts.
