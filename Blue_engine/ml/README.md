# Blue_engine.ml — offline machine-learning layer

> **Rule zero:** nothing in this package sends an order. It produces *validated
> probabilities*; the live engine policy decides what to do with them. See
> `Blue_engine/ml/__init__.py` for the full design rules.

## The pipeline, end to end

```
data_sources/          features/             models/            evaluation/
raw vendor data  ->  point-in-time store  ->  model.fit  ->  purged walk-forward
NSE RBI OpenAlgo      (the leak barrier)      ridge gbm rf    skill, deflated Sharpe,
yfinance news macro    + labels              mlp cnn rl       cost-adjusted edge, PBO
                                              ENSEMBLE                |
                                                                        v
                                              audit/  <-  registry/  (manifest sign/verify)
                                              leakage, quality gate, degradation
```

## Layout

| Folder | What it does | Key entry points |
|---|---|---|
| `data_sources/` | One file per vendor; every row stamped with *knowledge* time (`as_of_ts`) | `registry.SOURCES`, `fetch()` |
| `features/` | Feature engineering + `point_in_time` join (the leak barrier) + labels | `asof_join`, triple-barrier labels |
| `models/` | Models implementing the `base.Model` contract; optional deps degrade loudly | `ridge`, `gradient_boosting`, `random_forest`, `mlp`, `cnn`, `rl`, `ensemble` |
| `evaluation/` | **Pure Python** (no sklearn/torch) so it runs anywhere an artifact can be audited | `walk_forward`, `skill`, `deflated_sharpe`, `auc` |
| `audit/` | Leakage detector, data-quality gate, degradation rows for the dashboard | `leakage`, `quality` |
| `training/`, `registry/` | Orchestrators and artifact manifest sign/verify (planned) | — |

## The evaluation order (read this twice)

```
1 split (purged, decided BEFORE any model exists)
2 audit (leakage + quality — BEFORE fitting)
3 fit   (one fold at a time, train only)
4 score (skill vs base rate, per fold)
5 aggregate (mean AND std — dispersion is the risk number)
6 deflate   (correct for how many variants you tried)
7 cost-check (does the edge survive Indian transaction costs?)
8 only then consider shipping
```

Steps 2, 6, 7 are the ones usually skipped — and they are the three that
actually prevent losses. A model that passes 1–5 and fails 6 or 7 is a model
that looked good in a notebook.

## The model contract

Every model subclasses `base.Model`:

```python
class MyModel(Model):
    name = "my_model"
    def fit(self, X, y): ...                 # store fitted state on self
    def predict_proba(self, X) -> list[float]: ...   # P(up), same rows as X
```

Pure-Python models (ridge) have no dependency and always work. sklearn models
raise `DependencyMissing` at construction if sklearn is absent — the ensemble
marks that member `unavailable` and continues with the rest (a loud degradation
row is logged, never a silent quality change). torch models (mlp/cnn/rl)
behave the same way.

## The ensemble

`models/ensemble.py` blends member probabilities with **skill-weighted averaging**
(`evaluation.skill`), falling back to equal weights when skill is not usable.
It is deliberately pure Python: the blending logic is auditable line by line.

```python
from Blue_engine.ml.models.ensemble import Ensemble
from Blue_engine.ml.models import ridge, gradient_boosting, random_forest

ens = Ensemble([ridge.RidgeModel(), gradient_boosting.GBMModel(),
                random_forest.RFModel()])
ens.fit(X_train, y_train, y_train)   # y_true is needed for skill weights
p = ens.predict_proba(X_test)
```

**Why an ensemble:** single models flip-flop on regime changes. Averaging
probabilities reduces variance without needing more data; skill weights stop a
bad model from dragging the blend down.

## Running the tests

From the repo root, using the project venv:

```powershell
. .\openalgo\.venv\Scripts\Activate.ps1
python -m pytest Blue_engine/tests/ml -q
```

All tests are hermetic (synthetic data, no network). 336 tests, ~17s.

## Adding a data source (the honest 4-step recipe)

1. Write `ml/data_sources/<vendor>.py` implementing
   `DataSource.fetch(symbol, start, end)`; index = `as_of_ts` (knowledge time).
2. Register in `ml/data_sources/registry.py:SOURCES`.
3. Add a unit test pinning a *known* vendor value (e.g. an RBI reference rate
   on a dated page). If you cannot write that test, the source stays out of
   production training.
4. Re-run the audit gate. Zero rows => source marked `degraded`; training refuses.

## Non-negotiables

1. **No look-ahead.** The loader enforces knowledge-time joins, not the model.
2. **No silent fallback.** Missing data => `Degradation` row => dashboard shows
   INSUFFICIENT DATA. Never train on a silently different sample.
3. **Reproducibility.** An artifact is valid only with its manifest (dataset
   hash, feature hash, git commit, params, seed, split dates).
4. **Leak-safe splits.** Purged walk-forward by default; random K-fold is blocked.
5. **Offline only.** Trading imports *frozen, signed* outputs from the registry.
