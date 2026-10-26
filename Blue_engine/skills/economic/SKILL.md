# `frontline.economic` — Macro regime skill

**Stage:** `economic` · **Engine:** `economics/economic_engine.py` ·
**Publishes:** `data/agent_stage/economic.json`

## Purpose

Read policy, inflation and growth context and turn it into a macro regime that
*scales* the equity allocation. This skill sets the label and the multiplier;
the allocation maths stays in the engine.

## Output contract (`_SCHEMAS["economic"]`)

| Key | Type | Allowed |
|---|---|---|
| `macro_regime` | enum | `Stagflationary` · `Expansionary` · `Recessionary` · `Normal` |
| `equity_regime_multiplier` | number | `0.25` … `1.5` |
| `interest_rate_bias` | enum | `Hawkish` · `Dovish` · `Neutral` |
| `rationale` | text | max 15 words |
| `data_requests` *(optional)* | object | `{"needs": [...], "reason": "..."}` |

Answer must contain **exactly** these keys — the validator rejects a payload
with a missing or extra key. Use `data_requests` to ask for more input; never
guess a missed number.

## Evidence it wants (stage gate defaults)

| Path | Check |
|---|---|
| `macro` | `present` |
| `macro.policy_rate` | `range` −5 … 30, fresh |

## What it may do

- Choose any regime label, with rationale.
- Propose a multiplier anywhere in `0.25 … 1.5`.
- Request more macro data.

## What it may **not** do

- Push the multiplier above the deterministic engine's own value
  (`_CAPS["economic"]["equity_regime_multiplier"]` — direction is *lower is safer*).
- Write anything but `economic.json` in the sandbox.
- Place, size or cancel an order.

## Failure behaviour

A failed, disabled or unparsable answer is **non-blocking by default**: the
engine runs on its own deterministic inputs and `report["agent_advice"]`
records the failure. The *data gate* — not the skill — decides whether the
stage may advance.

## Tests

`tests/test_frontline_agent.py`, `tests/test_llm_chain.py`,
`tests/test_stage_skills.py`
