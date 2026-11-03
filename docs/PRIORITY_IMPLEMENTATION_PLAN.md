# Priority Implementation Plan: Conviction Sizing and P&L-Blind Dashboard

Status: IN PROGRESS. Step A1 done (branch `feat/conviction-score-v2`). Written before implementation, as the project rules require.
Last updated: 2026-11-04.

Companion to `docs/IMPLEMENTATION_PLAN.md`. Read that first for the phase structure.

---

## 1. What exists today (audited 2026-11-04)

### Priority A: Conviction-based position sizing

| Piece | Where | State |
|---|---|---|
| Fractional Kelly | `src/quant/portfolio/sizing.py`, wraps `Blue_engine/portfolio/sizing/kelly.py` | Works. Takes one edge estimate (mu, sigma). No conviction input. |
| Vol-target scale | `src/quant/portfolio/sizing.py`, wraps `Blue_engine/portfolio/sizing/vol_target.py` | Works. |
| Drawdown governor | `Blue_engine/portfolio/sizing/drawdown_governor.py` | Exists. Not yet wired to the quant layer. |
| Platt calibration | `src/quant/models/calibration.py` | Only a fitter (maps a raw score to a probability). It does not score conviction. |
| Min-of-caps combiner | `Blue_engine/portfolio/sizing/__init__.py` | Documented design. The most conservative governor binds. |

Gap: nothing combines the six required conditions into one conviction score, and there are no predefined size tiers. The plan's "Calibrated conviction" line marks the Platt fitter as complete. That is accurate for the fitter only. The conviction engine itself is not built.

### Priority B: P&L-blind dashboard

| Piece | Where | State |
|---|---|---|
| Terminal dashboard | `Blue_engine/dashboard/render.py` | Shows unrealized P&L in the position table (line ~112-133) and in the detail panel (line ~180-204). Shown by default. |
| Web dashboard | `dashboard/algo_pro/index.html` | Shows a large "Today" P&L figure (line ~123) that updates live (line ~294). Shown by default. |
| Backtest metrics | `Blue_engine/backtest/metrics.py` | Exists. To be checked for MAE, MFE, profit factor and EV before reuse. |

Gap: P&L is visible by default in both dashboards. This fails Phase 3 exit check 5 ("P&L hidden by default"). The EV, MAE/MFE, profit factor, rule-compliance and deviation panels do not exist on the execution screen yet.

---

## 2. Guardrails for both features

- Conviction never overrides a hard cap. Final size = min(Kelly cap, vol-target cap, drawdown cap, tier cap, portfolio exposure cap).
- No module in `risk/`, `execution/` or `portfolio/` imports `core/llm` (existing Phase 2 exit check 3).
- Every size decision writes an audit record: inputs, each condition's pass/fail, tier chosen, which cap bound, and the final size.
- Conviction inputs must be point-in-time. No look-ahead. Tests will check this.
- Costs, slippage and brokerage are included in any evaluation. A tier is only enabled after out-of-sample evidence.
- Nothing goes to live trading. Live stays blocked by the Phase 3 exit checks.

---

## 3. Phase A: Conviction sizing

### A1. Conviction score (pure function, no I/O)
- Inputs: six condition scores, each in [0, 1]:
  1. Technical momentum
  2. Market structure and trend alignment
  3. Catalyst quality (corporate announcement)
  4. Volume and liquidity confirmation
  5. Volatility and execution conditions (acceptable = 1)
  6. Strategy historical validation (from out-of-sample results only)
- Combine with a weighted geometric mean, so one weak condition pulls the score down hard. Weights are fixed in config, not fitted on the test set.
- Output: score in [0, 1], plus the per-condition breakdown.
- Location: `src/quant/models/conviction.py` (flow lane: Signal and conviction, stage: Calibrated conviction). Status: step A1 done, 18 unit tests pass.

### A2. Size tiers
- Fixed table in `config/`: score bands map to a size multiplier (for example: below 0.5 = no trade; 0.5-0.7 = base; 0.7-0.85 = 1.5x base; above 0.85 = 2x base, capped).
- The tier table is a proposal. Values are set only after backtest and out-of-sample evidence. Until then they are config placeholders, and tests check the logic, not the profit.
- Location: `src/quant/models/tiers.py` and `config/conviction_tiers.yaml`. Decision (2026-11-04): placeholder values, tiers DISABLED by default.

### A3. Wire into sizing with hard caps
- Tier multiplier applies to the base size from fractional Kelly.
- Then the existing min-of-caps rule applies. Conviction can only reduce the size set by the caps, never raise it above them.
- Automatic de-risking: if market-regime or drawdown signals worsen, the tier cap drops even if the score is high.
- Location: extend `src/quant/portfolio/sizing.py`. Do not copy the formulas. Wrap the Blue_engine modules.

### A4. Validation
- Unit tests: score math, tier boundaries, cap binding, de-risk behaviour, no look-ahead.
- Calibration report: reliability table of predicted conviction against realised outcomes, out-of-sample only.
- Backtest with costs: compare flat sizing against tiered sizing. Report both. Do not enable tiers unless out-of-sample results improve after costs.

Exit check for A: tests pass, calibration report exists, and tiers are disabled by default in config until you approve them.

---

## 4. Phase B: P&L-blind dashboard

### B1. Default view changes
- Hide all realized and unrealized P&L figures by default in both dashboards (`render.py` and `index.html`).
- Show a single explicit toggle to reveal money figures. The toggle state is not stored by default.
- Keep position size, price and status visible.

### B2. Execution panels (shown by default)
- Strategy consistency, win-rate probability, EV, profit factor, risk-to-reward, MAE, MFE.
- Execution quality (slippage versus expected price).
- Strategy deviation alerts, rule compliance, risk utilisation.
- Reuse `Blue_engine/backtest/metrics.py` after checking it computes these correctly.

### B3. Separate authorised accounting view
- Money figures move to a separate view that requires a deliberate, authenticated action.
- Periodic review screen: daily P&L, drawdown, and EV over the review window.
- Location: `Blue_engine/dashboard/` (terminal) and `dashboard/algo_pro/` (web). Shared data helpers go in one module, not duplicated.

### B4. Tests
- Test that the default render contains no money figures.
- Test that the toggle reveals them and that the accounting view requires the authorised action.
- Test that EV, profit factor and MAE/MFE match hand-calculated values on a small fixed trade list.

Exit check for B: default screens show no P&L, and the tests above pass.

---

## 5. Order of work

1. A1 conviction score and tests (pure, can be tested locally now).
2. A2 tiers config and tests.
3. A3 wiring into sizing, with audit records.
4. B1 hide P&L by default, with tests (smallest change, highest safety value; can run in parallel with A1-A3).
5. B2 execution panels.
6. B3 accounting view.
7. A4 backtest with costs and calibration report.

Each step ends with: tests run, this document updated, and a commit with the required trailers.

---

## 6. Decisions and open items

Decided 2026-11-04:
- Branch: `main` was the requested target, but `main` and `master` have diverged (7 and 39 unique commits) and `main` lacks `src/quant/portfolio/`. Work is on `feat/conviction-score-v2` (from `master`) until the branch question is resolved. Nothing pushed to `main`.
- Tier values: placeholders, disabled by default, enabled only after out-of-sample evidence.
- Accounting view: protected by a local password.

Open:

- **Test runs:** 103 existing tests fail only because `openalgo` is not installed. PyPI is blocked in this sandbox. New pure modules that depend only on numpy can still be tested here.
- **Branch reconciliation:** choose how `main` and `master` are reconciled before anything lands on `main`. Not yet decided.
- **Accounting view password:** how the local password is stored and set (not yet designed). Needs a security review before build.
- **Existing Phase 2/3 status claims:** the existing plan marks some items as implemented that are only partly built. I will note this in the status section rather than edit those claims without your review.
