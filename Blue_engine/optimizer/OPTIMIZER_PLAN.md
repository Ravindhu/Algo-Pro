# Optimizer Engine — Phase Plan & Work Log

**Goal:** pick *one* strategy, search its parameters, and hand back the **parameter
set that maximises out-of-sample Sharpe** — plus the three visuals an operator
needs to trust that answer:

1. **Which parameters are driving the ship** → `plot_param_importances`
2. **Each important parameter's individual sweet spot** → `plot_slice`
3. **How the important parameters weave together** → `plot_parallel_coordinate`

Status: `[x]` done · `[~]` in progress · `[ ]` pending

---

## 0. Discovery (what already exists — do not duplicate)

Before writing a line, the existing surface was reviewed. Four things already
exist and are **reused**, not rebuilt:

| Existing component | What it gives us | Reused as |
|---|---|---|
| `Blue_engine/ml/evaluation/splits.py` | leak-safe `walk_forward` / `purged_kfold` / `last_holdout` | the fold geometry |
| `Blue_engine/ml/evaluation/metrics.py` | `sharpe`, `max_drawdown`, `deflated_sharpe`, `fold_summary`, `cost_adjusted_edge` | the objective + deflation |
| `Blue_engine/ml/evaluation/walk_forward.py` | `run_walk_forward` orchestrator (fit→score per fold) | the honest scoring loop |
| `Blue_engine/portfolio/optim/benchmark.py` | `sweep_rebalance_params` **grid** sweep | the thing this engine replaces (kept for its cost-band use) |

**The gap this engine fills:** the project has only an *exhaustive Cartesian grid*
(`sweep_rebalance_params`). AGENTS.md explicitly asks to replace sequential
scanning with **TPE**. There is no parameter search over a *strategy's own*
parameters, and no importance/slice/parallel-coordinate visualisation anywhere.

---

## 1. Architecture

```
                      Blue_engine/optimizer/
                      ─────────────────────────
  strategy params ──► search_space.py   (typed, bounded, named spaces)
                          │
                          ▼
  folds + data    ──► objective.py      (param set -> out-of-sample Sharpe)
                          │  reuses ml/evaluation/splits + metrics + run_walk_forward
                          ▼
                      engine.py         (sampler + study loop + pruning + budget)
                          │
              ┌───────────┴────────────┐
              ▼                        ▼
        study.py (trials)        analysis.py (3 figures)
              │                        │
              ▼                        ▼
        report.py (JSON + MD)     plots.py (Plotly figures)
              │
              ▼
     dashboard/pages/optimizer.py  (read-only page)
                          │
                          ▼
     scripts/optimize_strategy.py  (CLI entry point)
```

### Design rules (inherited from the project, non-negotiable)

1. **No look-ahead.** Every trial is scored with walk-forward folds from
   `ml/evaluation/splits.py`. A trial never sees the test block during fit.
2. **Deflation is mandatory.** Searching N trials *is* a selection effect.
   The reported Sharpe is passed through `deflated_sharpe(observed, n_trials=N)`.
   A raw Sharpe from a search is not a Sharpe (AGENTS.md; Bailey & López de Prado).
3. **Costs are always charged.** A parameter set that wins only gross of Indian
   costs has not won. `cost_adjusted_edge` is applied by the objective.
4. **Degrade, never halt.** If Optuna is absent we fall back to our own
   TPE-style sampler. If neither a strategy nor data is available the engine
   returns `ok=False` with a reason — it never guesses a best parameter set.
5. **Read-only w.r.t. capital.** Nothing here places an order. The output is a
   *recommendation*; promoting it to the live config is a separate, deliberate act.

---

## 2. Phases

### Phase 1 — Package skeleton & search space — ✅ DONE

| # | Deliverable | Where |
|---|---|---|
| 1 | Package init, logger, data-root convention | `optimizer/__init__.py` |
| 2 | `ParamSpec` (int/float/categorical, low/high/log/step/choices) | `optimizer/search_space.py` |
| 3 | `SearchSpace` with `sample(trial_like)`, `validate(params)`, `to_dict()` | `optimizer/search_space.py` |
| 4 | Built-in spaces: `ocl_keylevel` (10 params), `pov_slicer`, `portfolio_rebalance` | `optimizer/spaces.py` |
| 5 | `param_space.json` override path so desks add spaces without code | `optimizer/spaces.py` |

### Phase 2 — Objective & search backend — ✅ DONE

| # | Deliverable | Where |
|---|---|---|
| 6 | `Objective` — turns a param dict into out-of-sample Sharpe via walk-forward | `optimizer/objective.py` |
| 7 | Strategy adapters: `callable`, `module:function`, and the built-in synthetic benchmark | `optimizer/strategies.py` |
| 8 | `TPESampler` fallback (pure Python, no numpy) used when Optuna is absent | `optimizer/sampler.py` |
| 9 | `Study` — trial history, param importance, best trial, pruning (MedianPruner-style) | `optimizer/study.py` |
| 10 | `OptimizerEngine.search()` — the loop, reproducibility, budget guard | `optimizer/engine.py` |
| 11 | Trial journal → `data/optimizer/studies/<study>.jsonl` (crash-safe, resumable) | `optimizer/store.py` |

### Phase 3 — The three visuals — ✅ DONE

| # | Deliverable | Where |
|---|---|---|
| 12 | `param_importances()` — fANOVA-lite / variance-based importance | `optimizer/analysis.py` |
| 13 | `plot_param_importances()` — ranked bar of what drives Sharpe | `optimizer/plots.py` |
| 14 | `plot_slice()` — per-param sweet spot with binned median trend line | `optimizer/plots.py` |
| 15 | `plot_parallel_coordinate()` — top-k params, one line per trial | `optimizer/plots.py` |
| 16 | `top_k_params()` — the "filter to only the important params" step | `optimizer/analysis.py` |
| 17 | `visual_workflow()` — runs 1→2→3 in the exact order AGENTS.md prescribes | `optimizer/analysis.py` |

### Phase 4 — Reporting & CLI — ✅ DONE

| # | Deliverable | Where |
|---|---|---|
| 18 | `report.py` — JSON + Markdown with the recommended parameter set | `optimizer/report.py` |
| 19 | CLI: select strategy, budget, folds, top-k, output dir | `Scripts/optimize_strategy.py` |
| 20 | `--visual-workflow` mode (the 10-parameter workflow end to end) | `Scripts/optimize_strategy.py` |

### Phase 5 — Dashboard page — ✅ DONE

| # | Deliverable | Where |
|---|---|---|
| 21 | Read-only page: recommended params, importance → slice → parallel trio | `dashboard/pages/optimizer.py` |
| 22 | `_data.optimizer_snapshot()` adapter (degrades to empty, never raises) | `dashboard/pages/_data.py` |
| 23 | Sidebar link | `dashboard/dash_app.py` |

### Phase 6 — Tests & audit — ✅ DONE

| # | Deliverable | Where |
|---|---|---|
| 24 | Search space: sampling, validation, bounds, log scale | `tests/test_optimizer_engine.py` |
| 25 | TPE fallback: converges on a known-peak objective, deterministic under seed | `tests/test_optimizer_engine.py` |
| 26 | Objective: no-look-ahead assertion, cost charging, `ok=False` on no data | `tests/test_optimizer_engine.py` |
| 27 | Analysis: importance ranks the true driver first; slice finds the peak | `tests/test_optimizer_engine.py` |
| 28 | Plots: all three build without plotly installed (graceful) | `tests/test_optimizer_engine.py` |
| 29 | Report: never claims "best" when the study found no edge | `tests/test_optimizer_engine.py` |

---

## 3. The workflow, in the order it must be read

The engine produces the three charts in **this fixed order**, because reading
them out of order is how a 10-parameter study stops being readable:

```
  STEP 1  plot_param_importances      "which 3-4 parameters are driving the ship?"
             │
             │  -> engine.top_k_params() picks them automatically
             ▼
  STEP 2  plot_slice(important_only)  "what is each one's individual sweet spot?"
             │
             │  -> the flat middle of the curve is the plateau (robust), not the peak
             ▼
  STEP 3  plot_parallel_coordinate    "how do they weave together at peak Sharpe?"
             │     (filtered to the important params only)
             ▼
       recommended parameter set
```

### How to read the output honestly

* **A peak is not a plateau.** Step 2 exists to separate them. If a parameter's
  slice is a spike, the setting is noise-fit and must not ship. Ship the middle
  of a *flat* region.
* **Importance is measured on the trials run, not on the market.** A parameter
  can rank low simply because it was never varied enough in the budget.
* **The deflated Sharpe is the number to quote.** The raw best-trial Sharpe is
  the maximum of N draws; it is biased upward by construction.

---

## 4. Assumptions (stated, not hidden)

* `optuna` is **not installed** in `.venv` (verified: `find_spec("optuna") is False`).
  The engine therefore ships a built-in TPE-style sampler and uses real Optuna
  automatically if it is installed later. No hard dependency is introduced.
* `plotly` 7.1.0 is installed → figures are real Plotly figures. If plotly is
  later missing, `plots.py` returns the *data* with `ok=False` rather than raising.
* The default strategy shipped in this package is a **synthetic benchmark**
  (`strategies.synthetic_band_strategy`) whose true optimum is known. That makes
  the engine testable without a broker, and lets a test assert the search
  actually finds the peak instead of merely running.
* Indian cost assumption defaults to 10 bps round-trip (matches
  `input/macro_master.json`), overridable per run.

---

## 5. Work log

| Date | Change |
|---|---|
| initial | Package created: search space, objective, TPE fallback, study, engine, analysis, plots, report, CLI, dashboard page, tests. |
