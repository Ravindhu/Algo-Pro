# old_blue_engine

Blue engine files moved out of `src/quant/blue_engine/` because nothing in the
quant layer uses them. Nothing here is loaded at runtime, and the bridge does
not import this folder.

## How "unused" was decided

A file counts as used if any of these reach it:

1. A `load("...")` call in `src/quant/` (non-test code), through `core/blue_bridge.py`.
2. An import from a file that is already used, followed transitively.
3. A dynamic walk: the `alpha/signals`, `alpha/Statergy`, `pov/slicers` folders,
   and the direct children of `alpha`, `economics`, `market`, `portfolio`, `pov`,
   `infrastructure` that `core/registry.py` imports at discovery time.
4. A module name mentioned in code outside `blue_engine/` (`src/quant/` non-test
   code, `scripts/`, `Servers/`).

Mentions only in docs (SOP, README) were NOT counted as use.

Non-Python files (`input/`, `configs/`, `README.md`, `pyproject.toml`) were left
in place. `input/` is runtime data.

## Moved files

- `backtest/blueprints.py`
- `database/publish_data_repo.py`
- `database/sample_data.py`
- `database/sample_data_1min.py`
- `database/vendor_pin.py`
- `economics/nlp/llm_scorer.py`
- `economics/nlp/rbi_minutes.py`
- `jobs/backfill_nav.py`
- `jobs/sector_dataset_report.py`
- `jobs/vendor_maintenance.py`
- `ml/data_sources/macro_source.py`
- `ml/data_sources/news_source.py`
- `ml/data_sources/openalgo_source.py`
- `ml/data_sources/yfinance_source.py`
- `ml/evaluation/sector_ranker_eval.py`
- `screener/_vendor_pin.py`
- `screener/valuation_engine.py`
- `sectors/sector_dataset.py`
- `tests/test_backfill_nav.py`
- `tests/test_backtest_blueprints.py`
- `tests/test_sector_ranker_eval.py`
- `tests/test_vendor_maintenance.py`

## Still to check before deleting

- Documentation mentions some of these (`docs/My_trading_system_SOP/*`, `README.md`, `src/quant/blue_engine/screener/README.md`). Update those if they are still the intended reference.
- Engine tests for these modules moved here as well, so they no longer run under `testpaths`.
- Imports inside these files still use the `blue_engine.*` names. They will not resolve from this location, which is expected while the code is parked.

## To restore a file

`git mv src/quant/old_blue_engine/<path> src/quant/blue_engine/<path>`
