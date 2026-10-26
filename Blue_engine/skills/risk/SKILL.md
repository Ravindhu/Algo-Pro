# `frontline.risk` — Veto skill

**Stage:** `risk` · **Engine:** `core/risk_engine.py`, `core/risk_gate.py` ·
**Publishes:** `data/agent_stage/risk.json`

## Purpose

The last advisory step before execution. It may veto on **any** grounds and may
only ever make the picture more cautious. It is the only skill with a
capital-stopping output, which is why its clamps are the strictest in the
system.

## Output contract (`_SCHEMAS["risk"]`)

| Key | Type | Allowed |
|---|---|---|
| `risk_veto_active` | bool | `true` / `false` |
| `max_allowed_slippage_pct` | number | `0.0` … `0.05` |
| `force_trailing_stop_pct` | number | `0.1` … `20.0` |
| `veto_reason` | text | max 15 words, used verbatim if vetoing |
| `data_requests` *(optional)* | object | `{"needs": [...], "reason": "..."}` |

## Evidence it wants (stage gate defaults)

| Path | Check |
|---|---|
| `portfolio.data` | `present` |

## What it may do

- **Veto for any reason.** Total discretion on the *yes/no*. "I do not trust
  this data" is a valid, sufficient veto reason.
- Tighten the slippage ceiling down to `0.0`.
- Force a tighter trailing stop down to `0.1`.
- Request more data instead of guessing.

## What it may **not** do — the three absolute rails

1. **`risk_veto_active` is forced to `true`.** There is no "slightly less
   vetoed". An agent answer can never clear a veto
   (`test_agent_cannot_clear_a_veto_when_a_baseline_says_veto`).
2. **Never widen a limit.** `_CAPS["risk"]` makes both numeric keys *lower is
   safer*; the result is `min(baseline, proposed)`.
3. **Never place, size or cancel an order.** `pov/` and `risk_gate.py` do not
   import the LLM layer, the skills, or the writer
   (`test_execution_modules_never_import_the_writer`).

A veto is still only *advisory*: `RiskEngine` combines it with its own
deterministic breaches, and the pipeline blocks POV when `risk_veto_active` is
true regardless of what the skill said.

## Why the caps here are not "arbitrary"

The operator's rule "no cap" applies to *judgement*: this skill may veto anything.
The clamp applies to *magnitudes that protect capital*. Removing it would let a
prompt-injected headline clear a live veto, which inverts the project's own
priority order (Capital Protection → Data Integrity → Risk Control → …).
If you want to experiment without the clamp, do it in a paper pipeline with a
temporary `_CAPS` override — never in live mode.

## Tests

`tests/test_llm_chain.py`, `tests/test_frontline_engine_outputs.py`,
`tests/test_live_monitor_guard.py`
