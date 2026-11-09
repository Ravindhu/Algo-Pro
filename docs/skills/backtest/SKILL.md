# `frontline.backtest` — Honesty skill

**Stage:** `backtest` · **Engine:** `backtest/engine.py`, `backtest/costs.py` ·
**Publishes:** `data/agent_stage/backtest.json`

## Purpose

Decide the historical window and the slippage assumption that make a backtest
*honest*, and name the flaw when one is visible. The point is to resist
flattering results, not to produce them.

## Output contract (`_SCHEMAS["backtest"]`)

| Key | Type | Allowed |
|---|---|---|
| `historical_start_date` | date | `YYYY-MM-DD`, a real calendar date |
| `slippage_penalty_pct` | number | `0.0` … `0.05` |
| `flaw_detected` | text | max 15 words |
| `data_requests` *(optional)* | object | `{"needs": [...], "reason": "..."}` |

## Evidence it wants (stage gate defaults)

| Path | Check |
|---|---|
| `backtest.data.periods` | `min_count` ≥ 1 |

## What it may do

- Pick any valid start date.
- Raise the slippage penalty anywhere up to `0.05` (5 %) — this is the
  "honesty knob": the schema maximum is also the safe direction.
- Report a flaw in free text.

## What it may **not** do

- Lower the slippage penalty below the deterministic assumption —
  `_CAPS["backtest"]["slippage_penalty_pct"]` is *higher is safer*
  (`test_tighter_slippage_penalty_is_kept_because_higher_is_safer`).
- Promote a strategy to live. Nothing in this stage routes orders.
- Write anything but `backtest.json`.
- Place, size or cancel an order.

## Failure behaviour

Non-blocking: the backtester falls back to its configured window and cost
model.

## Tests

`tests/test_backtest_engine.py`, `tests/test_backtest_batch.py`,
`tests/test_llm_chain.py`
