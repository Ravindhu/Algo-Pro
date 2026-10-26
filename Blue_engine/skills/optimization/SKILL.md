# `frontline.optimization` — Search-space skill

**Stage:** `optimization` · **Engine:** `optimizer/spaces.py`,
`optimizer/objective.py` · **Publishes:** `data/agent_stage/optimization.json`

## Purpose

Bound the search space for the hyper-parameter optimiser and choose the
permitted search method. A *narrower, justified* space is the defence against
curve-fitting; the skill exists to make overfitting harder, not easier.

## Output contract (`_SCHEMAS["optimization"]`)

| Key | Type | Allowed |
|---|---|---|
| `min_exploration_rsi` | integer | `0` … `100` |
| `max_exploration_rsi` | integer | `0` … `100`, must be ≥ `min_exploration_rsi` |
| `optimization_method` | enum | `bounded_scipy` · `genetic_restricted` |
| `rationale` | text | max 15 words |
| `data_requests` *(optional)* | object | `{"needs": [...], "reason": "..."}` |

The validator rejects a payload where `min_exploration_rsi > max_exploration_rsi`.

## Evidence it wants

None mandatory.

## What it may do

- Bound the RSI exploration window anywhere inside `0 … 100` with either end
  twice the price.
- Choose either permitted method.

## What it may **not** do

- Widen search into an unbounded space — the schema range is the boundary and
  `optimizer/spaces.py` is the authority for what a strategy actually accepts.
- Approve a study for live use. Promotion is an operator action.
- Write anything but `optimization.json`.
- Place, size or cancel an order.

## Dashboard

The 10-parameter visual workflow (importances → slice → parallel coordinates)
is rendered by `dashboard/pages/optimizer.py`. The skill influences the *search
bounds*, not the plots.

## Tests

`tests/test_optimizer_engine.py`, `tests/test_optimizer_data_cache.py`
