# Priority Implementation Plan: Conviction Sizing and P&L-Blind Dashboard

Status: DRAFT for review. No trading code changed yet.
Last updated: 2026-11-05 (re-audited against repo at `c28fcd8`).

Companion to `docs/IMPLEMENTATION_PLAN.md`. Numbered items there (PB-*, P2-*) map to this file.

---

## 1. What exists today (audited 2026-11-05)

### Priority A: Conviction-based position sizing

| Piece | Where | State |
|---|---|---|
| Fractional Kelly | `src/quant/portfolio/sizing.py`, wraps `Blue_engine/portfolio/sizing/kelly.py` | Works. One edge estimate (mu, sigma). No conviction input. |
| Vol-target scale | `src/quant/portfolio/sizing.py`, wraps `Blue_engine/portfolio/sizing/vol_target.py` | Works. |
| Drawdown governor | `Blue_engine/portfolio/sizing/drawdown_governor.py` | Exists. Not wired to the quant layer. |
| Platt calibration | `src/quant/models/calibration.py` | Fitter only. Does not score conviction. |
| Min-of-caps combiner | `Blue_engine/portfolio/sizing/__init__.py` | Documented design. Most conservative cap binds. |
| Conviction score | `src/quant/portfolio/conviction.py` | MISSING. |
| Size tiers | `src/quant/portfolio/tiers.py`, `config/conviction_tiers.yaml` | MISSING. |

Gap: no module combines the six conditions into one score, and there are no size tiers.

### Priority B: P&L-blind dashboard

| Piece | Where | State |
|---|---|---|
| Terminal dashboard | `Blue_engine/dashboard/render.py` | Shows unrealized P&L by default (position table and detail panel). |
| Web dashboard | `dashboard/algo_pro/index.html` | Shows a large "Today" P&L figure by default, live updates. |
| Quant dashboard | `src/quant/reporting/dashboard.py` | Always shows total P&L (commit "P&L always visible"). |
| Backtest metrics | `Blue_engine/backtest/metrics.py` | Exists. Check MAE, MFE, profit factor, EV before reuse. |

Gap: P&L is shown by default in all three dashboards. This conflicts with the standing rule (P&L hidden by default). Owner must decide first (P0-1).

---

## 2. Guardrails for both features

- Conviction never overrides a hard cap. Final size = min(Kelly cap, vol-target cap, drawdown cap, tier cap, portfolio exposure cap).
- No module in `risk/`, `execution/` or `portfolio/` imports `core/llm`.
- Every size decision writes an audit record: inputs, each condition's pass/fail, tier chosen, which cap bound, final size.
- Conviction inputs must be point-in-time. No look-ahead. Tests check this.
- Costs, slippage and brokerage are included in any evaluation. A tier is enabled only after out-of-sample evidence.
- Nothing goes to live trading. Live stays blocked by Phase 3 exit checks.

---

## 3. Phase A: Conviction sizing

### A1. Conviction score (pure function, no I/O) - item P2-1
- Inputs: six condition scores, each in [0, 1]:
  1. Technical momentum
  2. Market structure and trend alignment
  3. Catalyst quality (corporate announcement)
  4. Volume and liquidity confirmation
  5. Volatility and execution conditions (acceptable = 1)
  6. Strategy historical validation (out-of-sample results only)
- Combine with a weighted geometric mean, so one weak condition pulls the score down hard. Weights fixed in config, not fitted on the test set.
- Output: score in [0, 1] plus per-condition breakdown.
- Location: `src/quant/portfolio/conviction.py`.

### A2. Size tiers - item P2-2
- Fixed table in `config/conviction_tiers.yaml`: score bands map to a size multiplier.
- Example bands (proposal only): below 0.5 = no trade; 0.5-0.7 = base; 0.7-0.85 = 1.5x base; above 0.85 = 2x base, capped.
- Values are placeholders until backtest and out-of-sample evidence exist. Tests check logic, not profit.
- Location: `src/quant/portfolio/tiers.py`.

### A3. Wire into sizing with hard caps - item P2-3
- Tier multiplier applies to base size from fractional Kelly.
- Then the min-of-caps rule applies. Conviction can only reduce size below the caps, never raise it above them.
- Automatic de-risking: if regime or drawdown signals worsen, the tier cap drops even if the score is high.
- Location: extend `src/quant/portfolio/sizing.py`. Wrap Blue_engine modules; do not copy formulas.

### A4. Validation - item P2-6
- Unit tests: score math, tier boundaries, cap binding, de-risk behaviour, no look-ahead.
- Calibration report: reliability table of predicted conviction vs realised outcomes, out-of-sample only.
- Backtest with costs: flat sizing vs tiered sizing. Report both. Do not enable tiers unless out-of-sample results improve after costs.

Exit check for A: tests pass, calibration report exists, tiers disabled by default until owner approves.

---

## 4. Phase B: P&L-blind dashboard

### B1. Default view - items PB-1, PB-2
- If P0-1 = hidden: hide realized and unrealized P&L in `render.py`, `index.html` and `reporting/dashboard.py`.
- One explicit toggle reveals money figures. Toggle state not stored by default.
- Keep position size, price and status visible.
- If P0-1 = shown: keep current behaviour and record the decision in `docs/decisions/`. Section closed.

### B2. Execution panels - item PB-3
- Strategy consistency, win-rate probability, EV, profit factor, risk-to-reward, MAE, MFE.
- Execution quality (slippage vs expected price).
- Strategy deviation alerts, rule compliance, risk utilisation.
- Reuse `Blue_engine/backtest/metrics.py` after checking it computes these correctly.

### B3. Separate authorised accounting view - item PB-4
- Money figures move to a separate view that needs a deliberate, authenticated action.
- Periodic review screen: daily P&L, drawdown, EV over the review window.
- Authorisation method: owner decision (status note section 3, item 4).
- Shared data helpers go in one module, not duplicated across dashboards.

### B4. Tests - item PB-5
- Default render contains no money figures (if P0-1 = hidden).
- Toggle reveals them; accounting view requires the authorised action.
- EV, profit factor, MAE/MFE match hand-calculated values on a small fixed trade list.

Exit check for B: default screens match the owner decision, and the tests above pass.

---

## 5. Order of work

1. P0-1 owner decision on P&L visibility.
2. P0-2 test baseline with `openalgo` installed.
3. P2-1 conviction score and tests (pure, runs on numpy).
4. P2-2 tiers config and tests.
5. P2-3 wiring into sizing, with audit records.
6. PB-1 and PB-2 hide P&L by default, with tests (smallest change, highest safety value; can run in parallel with steps 3-5).
7. PB-3 execution panels.
8. PB-4 accounting view.
9. P2-6 backtest with costs and calibration report.

Each step ends with: tests run, this document updated, and a commit with the required trailers.

---

## 6. Blockers and open decisions

- **Test runs:** the 70 failures in the 11-04 run came from the missing `openalgo` package. Pure numpy modules can be tested in the sandbox.
- **Branch:** recent work went straight to `main`. Owner to confirm.
- **Tier values and weights:** placeholders until backtest evidence exists.
- **Authorised accounting view:** what counts as authorised is not decided.
- **P&L default:** current code shows P&L; the standing rule says hidden. Owner to decide (P0-1).
- **Existing Phase 2/3 claims:** some items are marked done in older docs but are only partly built. Corrected in `IMPLEMENTATION_PLAN.md` with DONE / PARTIAL / PENDING labels. Owner to review.
