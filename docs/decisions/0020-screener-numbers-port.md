# Decision 0020: port the screener number parser (stage 6, sub-step 6.2a) (2026-11-06)

Status: IMPLEMENTED. Owner sign-off: pending (same review as decisions 0018 and 0019).

## What changed

- New `src/quant/data/screener_numbers.py`: full port of `Blue_engine/screener/client/numbers.py`
  (`to_number`, `to_percent`, `clean`, `is_null_token`, `ratio_lookup`, `metric_number`,
  `table_row_lookup`, `latest_period_value`, and the alias tables). Pure Python.
- `src/quant/data/screen_rules.py`: its earlier partial copy of `to_number` (from decision in 6.1) is
  removed. It now imports `to_number` from `screener_numbers.py`. One parser only.
- Parity test: `tests/parity/test_screener_numbers_parity.py` (140 cases). Loads Blue's
  `numbers.py` alone, which is stdlib only, so it runs in the sandbox.

## Why it matters

A missing screener value must never become 0. A company with no reported debt would otherwise pass
a "debt/equity < 1" filter. The parser returns `None` for every missing or unreadable cell, and the
parity test checks this against Blue on every grid case.

## Known differences (none found in parity)

- The Blue docstring names `unit_scale="absolute_cr"`, but the code checks `"absolute"`. The port keeps
  the code's behaviour.

## Still open in stage 6

- 6.2b (parsers, extractors; needs bs4 approval), 6.2c (network client, yfinance; owner data decision),
  6.3 economics, 6.4 optimizer, 6.5 POV, 6.6 agent pipeline.
- See `docs/STAGE6_PLAN_2026-11-06.md`.

## Test results (sandbox, 2026-11-06)

- `pytest tests/unit tests/parity`: 27 failed, 1472 passed. All 27 are `httpx` (HRP path, item 8).
