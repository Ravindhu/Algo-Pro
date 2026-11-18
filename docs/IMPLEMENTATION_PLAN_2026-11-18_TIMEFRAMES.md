# Implementation plan: trade setup timeframes (2026-11-18)

Owner request: add three user-defined timeframes in the Trade setup popup:
1. MTF (setup timeframe)
2. HTF (trend / bias timeframe)
3. LTF (entry timeframe)

All three are common for every strategy (one setting, not per strategy).

## Decisions

- Stored in `config/settings.yaml` under `trade_setup.timeframes` with keys `htf`, `mtf`, `ltf`.
  Same home as `strategy` and `brokerage` (set through `/api/setup`, owner sign-in).
- Allowed values: `1m`, `3m`, `5m`, `10m`, `15m`, `30m`, `1h`, `1d`. Same label style as the
  OCL strategy params (`htf_interval`, `ltf_interval` in `config/bot.yaml`).
- Order rule: when all three are set, LTF < MTF < HTF in minutes. A bad order is refused.
- Saved only. No engine stage reads these values yet, same as `strategy`. Wiring them into
  strategies changes trade behaviour, so it needs a separate owner decision.
- No new API route, no new POST call in the HTML. This keeps `tests/unit/test_pnl_visible.py`
  (exact route set and POST count) unchanged.

## Phases

| # | Work | Files | Test |
|---|---|---|---|
| T1 | Validation, save, read of the three keys | `src/quant/core/setup_store.py` | `tests/unit/test_setup_timeframes.py` |
| T2 | Three selects in the Trade setup panel, fill and save | `dashboard/main_dashboard.html` | `tests/unit/test_pnl_visible.py`, `tests/unit/test_main_dashboard.py` |
| T3 | Full unit run, commit to master, push | — | `pytest` (full run) |

## Status

- T1: done. setup_store validates, saves and reads htf/mtf/ltf; order rule enforced.
- T2: done. Three selects in the Trade setup panel; fill and save wired; JS syntax checked.
- T3: done. Unit suite 645 passed, 1 skipped; quant suite 1410 passed, 1 xfailed.
  Pre-existing, not from this change: tests/unit/test_upgrade_pin.py fails on master (vendored pin).

## Not done (needs owner decision)

- Engine does not read these timeframes yet. OCL strategies still take htf/ltf per symbol in config/bot.yaml.
