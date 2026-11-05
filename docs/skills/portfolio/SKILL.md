# `frontline.portfolio` — Constraint skill

**Stage:** `portfolio` · **Engine:** `portfolio/portfolio_engine.py` ·
**Publishes:** `data/agent_stage/portfolio.json`

## Purpose

Translate the macro/regime picture into portfolio *constraints*: a sector
ceiling, a minimum cash buffer, and whether rebalancing is appropriate at all.
It constrains the optimiser; it never allocates capital itself.

## Output contract (`_SCHEMAS["portfolio"]`)

| Key | Type | Allowed |
|---|---|---|
| `max_tech_sector_exposure` | number | `0.0` … `1.0` |
| `min_cash_buffer_pct` | number | `0.0` … `1.0` |
| `rebalance_allowed` | bool | `true` / `false` |
| `rationale` | text | max 15 words |
| `data_requests` *(optional)* | object | `{"needs": [...], "reason": "..."}` |

## Evidence it wants (stage gate defaults)

| Path | Check |
|---|---|
| `alpha.data.selected` | `present` |

## What it may do

- Tighten the sector ceiling to any value down to `0.0`.
- Raise the cash buffer to any value up to `1.0` (100 % cash = fully defensive).
- **Block rebalancing entirely.** `rebalance_allowed` has an absolute safe
  side, so the only value that survives `_escalate` is `false`.

## What it may **not** do

- Widen the sector ceiling past the deterministic baseline —
  `_CAPS["portfolio"]["max_tech_sector_exposure"]` is *lower is safer*
  (regression test:
  `test_agent_cannot_widen_exposure_past_the_baseline_ceiling`).
- Turn rebalancing **on**. `rebalance_allowed` is forced to `false`.
- Choose weights, quantities or instruments.
- Write anything but `portfolio.json`.
- Place, size or cancel an order.

## Failure behaviour

Non-blocking. With no valid answer the optimiser uses its configured policy
(`portfolio/optim_policy.py`) unchanged.

## Tests

`tests/test_llm_chain.py`, `tests/test_portfolio_engine.py`
