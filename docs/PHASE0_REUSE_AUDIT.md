# Phase 0: reuse audit of `Blue_engine/`

Status: IN PROGRESS. Entry gate for Phase 1 (see `docs/IMPLEMENTATION_PLAN.md`).
Rule: `Blue_engine/` code is wrapped, never copied (`AGENTS.md`).

## 1. Goal

For each `src/quant/` module that declares a `Blue_engine/` target, decide:

- **WRAP**: the module becomes a thin adapter over the Blue_engine function. No logic copy.
- **KEEP**: no Blue_engine equivalent exists. The new code stays, with a reason.
- **PENDING**: not audited yet.

Each WRAP is one commit. Each commit runs the unit suite and the Blue_engine suite.

## 2. Environment and test baseline

Blue_engine code imports the package name `blue_engine`. The folder on disk is `Blue_engine/`.
Production install maps one to the other (`Blue_engine/pyproject.toml`). The test conftest
does the same mapping by hand. Production code has no such mapping, so wrappers need a bridge.

Baseline run, 2026-11-03, Linux sandbox, Python 3.13, `PYTHONPATH=src:.`:

| Run | Passed | Failed | Skipped | Notes |
|---|---|---|---|---|
| `tests/unit` | 73 | 0 | 0 | Phase 1–3 suite. Clean. |
| `Blue_engine/tests`, no `openalgo` | 132 | 37 | 76 | 33 failures are `No module named 'openalgo'`. |
| `Blue_engine/tests`, `openalgo` 2.0.3 from GitHub tag | 1644 | 49 | 8 | See below. |

Findings:

1. **The 37 failures are mostly environment.** 33 need the OpenAlgo SDK (`openalgo==2.0.3`, pinned).
   The sandbox cannot reach PyPI. The SDK was loaded from the GitHub tag `v2.0.3`, which matches the pin.
   With the SDK loaded, the risk-gate live-acknowledgement tests pass (7 of 7).
   The earlier claim that they fail upstream is not confirmed. They fail only without the SDK.
2. **New failures with the SDK loaded (49):**
   - 39: `pyarrow` not installed (parquet). Environment.
   - Several: `yfinance` not installed. Environment.
   - `test_skill_writer::test_default_sandbox_is_under_the_project_data_dir` expects the folder name
     `my-trading-system`. Fails in a checkout named `Algo-Pro`. A real portability bug: the test depends on the checkout name.
   - `test_phase3_bars`: expected at least 4 strategy calls, got 2. **Real logic failure, not environment.**
   - `test_optimizer_engine`: best score 2.97 is under 90% of the known peak 3.84. **Real logic failure, convergence.**
   - `test_portfolio_engine`, `test_phase4_registry_cli`: need a closer read before classifying.
3. **Blue_engine import name.** The package name is `blue_engine`. On a case-sensitive
   filesystem, a plain `import blue_engine` fails. Windows works by accident. The bridge in
   section 4 fixes this for the new wrappers.

Decision on test environments (changed after the first wrap, step 1):

- Wrappers import Blue_engine. Blue_engine imports the OpenAlgo SDK. So the Phase 1 unit tests
  that exercise a wrapped module now need the SDK. Without it, 11 Phase 1 tests fail with
  `BlueEngineUnavailable`. Before the wrap, all 73 passed without the SDK.
- This is intentional. Production installs the SDK (`requirements.txt` pins `openalgo==2.0.3`).
  A silent fallback to a copied formula would be the exact failure this phase prevents.
- The wrapper tests in `tests/unit/test_phase0_wrappers.py` skip with a reason when the SDK is absent.
  Full runs must have the SDK. Results with it: 83 passed.

## 3. Mapping: Phase 1–2 modules

Verified = read both sides and compared logic. Declared = taken from the module header only.

| src module | Blue_engine target | Duplicate? | Decision | Status |
|---|---|---|---|---|
| `research/costs.py` | `backtest/costs.py` (`CostModel`) | Yes. Same statutory stack, different interface and placeholder rates | WRAP | Done. Step 1 |
| `research/walk_forward.py` | `ml/evaluation/splits.py` (`walk_forward`) | Yes. Expanding window with purge, same purpose | WRAP (API change: `test_size` removed) | Done. Step 2 |
| `research/validation.py`, Sharpe | `ml/evaluation/metrics.py` (`sharpe`) | Yes. Same formula | WRAP | Done. Step 3 |
| `research/validation.py`, DSR | `ml/evaluation/metrics.py` (`deflated_sharpe`) | Formula differs: Blue omits the cross-trial variance, so its threshold is mis-scaled | KEEP (see `docs/decisions/0001-dsr-pbo-bootstrap-kept-local.md`) | Done. Step 3 |
| `research/validation.py`, PBO | none found in `Blue_engine/` | No. Blue's docstring claims PBO exists; it does not | KEEP (decision 0001) | Verified |
| `research/validation.py`, bootstrap | `portfolio/optim/significance.py` (`bootstrap_ci`, `bootstrap_metric`) | Different output: Blue gives CIs, this gives a one-sided p-value | KEEP (decision 0001) | Verified |
| `research/backtest.py` | `backtest/engine.py`, `backtest/loops.py` | Partly. Blue engine is strategy-driven | PENDING | Step 4 |
| `research/trial_log.py` | `core/auto_tune.py` | Not yet read | PENDING | |
| `research/lineage.py` | `core/ledger.py` | Not yet read. Ledger is execution-scoped; lineage is run-scoped | PENDING | |
| `research/capacity.py` | `backtest/metrics.py` | Not yet read | PENDING | |
| `risk/gate.py`, `risk/pretrade.py` | `core/risk_gate.py` | Not yet read. Safety-critical | PENDING, priority | |
| `risk/kill_switch.py` | `core/kill_switch_store.py` | Not yet read | PENDING | |
| `execution/openalgo_client.py` | `core/client.py` | Not yet read. Must stay the only `.placeorder(` caller | PENDING, priority | |
| `monitor/ledger.py` | `core/ledger_store.py` | Not yet read | PENDING | |
| `monitor/tca.py` | `backtest/costs.py` | Not yet read | PENDING | |
| Phase 2 and 3 modules (about 40 more) | see section 3 header list in `src/quant/*` | Not audited | PENDING | |

## 4. Bridge: how wrappers import `Blue_engine`

One module, `src/quant/core/blue_bridge.py`, is the only place that imports `blue_engine`.
It mirrors the mapping in `Blue_engine/tests/conftest.py`. If the package is installed, it
uses the installed package. If not, it loads `Blue_engine/` under the name `blue_engine`.
All other `src/quant` modules import from the bridge.

Why one bridge: one place to fix the import name, one place to report a missing SDK,
and no wrapper quietly reimplements the import.

## 5. Step log

Each step is one commit. The step list is updated before each commit.

1. DONE: `research/costs.py` → thin wrapper over `blue_engine.backtest.costs.CostModel`.
   Bridge `src/quant/core/blue_bridge.py` added in the same commit.
   Equivalence: 36 grid cases match the pre-wrap code (max diff 2e-10 INR).
   Regression tests: `tests/unit/test_phase0_wrappers.py` (golden values, delegation spy).
2. DONE: `research/walk_forward.py` → wrapper over `blue_engine.ml.evaluation.splits.walk_forward`.
   API CHANGE (flagged): `purged_walk_forward` no longer takes `test_size`. Blue derives the test-block size
   from `n_splits`. Its only caller was `tests/unit/test_phase1_research.py`, updated to the new signature.
   Production scripts already call Blue directly. Test added for delegation and purge gap.
3. DONE (partial): `research/validation.py`. `sharpe` wraps Blue. Behaviour change: constant input gives 0.0 instead of ~6.9e16 (float noise). DSR, PBO and bootstrap kept local. Decision: `docs/decisions/0001-dsr-pbo-bootstrap-kept-local.md`.
4. `research/backtest.py` → decision after reading `engine.py`.

### Open questions found in step 1 (NOT changed; need a decision)

- **STT on the BUY leg of delivery.** The pre-wrap code charged STT on the sell leg only.
  Blue charges STT on both legs. Delivery STT, as I understand it, applies to both buy and sell.
  Verify against the current NSE/SEBI schedule. If confirmed, the wrapper's `_leg_config` changes
  one line. That is a behaviour change and needs its own commit and test update.
- **Brokerage cap.** Pre-wrap code: `min(0.03% of notional, ₹20)`. Blue uses a pure percentage.
  The wrapper keeps the ₹20 cap by converting it to bps per trade. Confirm the broker's real schedule.
- **Impact.** Pre-wrap code used a flat `impact_bps` (5 bps). Blue's better model is square-root
  impact (`impact_coefficient`). Phase 1 exit check 3 wants impact included. The flat model is kept for now.
  Switching to the square-root model changes results and needs its own commit.
- **Placeholder rates.** Unchanged from before the wrap. Verify before any live use.

## 6. Exit check for Phase 0

1. Every Phase 1–2 WRAP target is a delegation, not a copy. A test checks that the wrapper calls Blue_engine.
2. The unit suite stays green (73 tests, no regression).
3. The Blue_engine baseline is recorded in this file. Real failures are listed by name.
4. Each KEEP has a reason in this file.
5. The PENDING rows above are either done or explicitly deferred to Phase 1 work by the user.

## 7. Blue_engine findings (reported, NOT changed here)

- `ml/evaluation/metrics.py::deflated_sharpe` mis-scales the threshold (no cross-trial variance). Rejects almost every strategy at N >= 10. See decision 0001.
- `ml/__init__.py` docstring lists PBO in `ml/evaluation/`. No implementation exists.
- Blue_engine import name: code uses `blue_engine`, folder is `Blue_engine/`. Works on Windows only without a bridge (fixed for src by `quant/core/blue_bridge.py`).
- Blue_engine test failures with the SDK loaded (49): environment (pyarrow, yfinance) and two real logic failures (`test_phase3_bars`, `test_optimizer_engine`), plus a checkout-name dependency (`test_skill_writer`).
