# `frontline.market` — Volatility-state skill

**Stage:** `market` · **Engine:** `market/market_engine.py` ·
**Publishes:** `data/agent_stage/market.json`

## Purpose

Classify the traded market's volatility state and pick the technical lookback
the indicator layer should use. It reads structure; it does not predict price.

## Output contract (`_SCHEMAS["market"]`)

| Key | Type | Allowed |
|---|---|---|
| `market_volatility_state` | enum | `Low` · `Elevated` · `High_Panic` |
| `technical_lookback_days` | enum | `10` · `14` · `20` · `50` |
| `short_term_bias` | enum | `Bullish` · `Bearish` · `Sideways` |
| `rationale` | text | max 15 words |
| `data_requests` *(optional)* | object | `{"needs": [...], "reason": "..."}` |

## Evidence it wants (stage gate defaults)

| Path | Check |
|---|---|
| `market` | `present` |

## What it may do

- Escalate the volatility state upward on any evidence it judges material.
- Choose any permitted lookback.
- Request more market data (breadth, India VIX history, sector dispersion).

## What it may **not** do

- De-escalate the volatility state below the deterministic engine's baseline —
  `_CAPS` has no `market` entry, so the *validator* governs shape while the
  engine governs the operative state (see
  `tests/test_frontline_agent.py::test_market_agent_can_only_escalate_volatility_state`).
- Write anything but `market.json`.
- Place, size or cancel an order.

## Failure behaviour

Non-blocking. The engine's own volatility classifier is authoritative when the
skill is unavailable.

## Tests

`tests/test_frontline_agent.py`, `tests/test_stage_skills.py`
