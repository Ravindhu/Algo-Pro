# `frontline.valuation` — Assumption-bounding skill

**Stage:** `valuation` · **Engine:** `valuation_engine.py`, `screener/valuations.py` ·
**Publishes:** `data/agent_stage/valuation.json`

## Purpose

Bound two assumptions for one company — the growth ceiling and the margin
safety haircut — so the DCF/EPV/CANAV ladder cannot be fed an optimistic
number. If the financials are incomplete, the correct answer is *refuse*, not
substitute.

## Output contract (`_SCHEMAS["valuation"]`)

| Key | Type | Allowed |
|---|---|---|
| `target_ticker` | ticker | uppercase, `A-Z0-9&._-` |
| `growth_rate_ceiling` | number | `0.0` … `0.05` |
| `margin_safety_adjustment` | number | `0.0` … `1.0` |
| `rationale` | text | max 15 words |
| `data_requests` *(optional)* | object | `{"needs": [...], "reason": "..."}` |

## Evidence it wants

None mandatory at the stage level: an empty screen is a valid answer. The
valuation engine itself returns `status="insufficient_data"` with the *named*
missing input rather than a fabricated per-share figure — that invariant is
tested in `tests/test_valuations.py`
(`status="insufficient_data"` ⇒ `per_share is None`).

## What it may do

- Propose any ceiling/haircut inside the schema range.
- Request the exact missing financial line items by name.

## What it may **not** do

- Loosen past the deterministic engine's own value — `_CAPS["valuation"]` makes
  *lower is safer* for both keys; the agent can only tighten.
- Publish a valuation figure at all. The engine computes it.
- Write anything but `valuation.json`.
- Place, size or cancel an order.

## Tests

`tests/test_valuations.py`, `tests/test_frontline_agent.py`
