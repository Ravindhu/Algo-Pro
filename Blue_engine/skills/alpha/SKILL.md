# `frontline.alpha` — Regime-consistency skill

**Stage:** `alpha` · **Engine:** `alpha/alpha_engine.py`,
`alpha/Statergy/ocl_keylevel.py` · **Publishes:** `data/agent_stage/alpha.json`

## Purpose

Flag the selected signals whose setup looks inconsistent with the current
regime, so the deterministic scoring can down-weight them. A warning layer —
it cannot create or promote a signal.

## Output shape

| Key | Meaning |
|---|---|
| `regime_conflicts` | tickers whose setup conflicts with the regime label |
| `rationale` | max 15 words |

## Evidence it wants

None mandatory — an empty conflict list is a valid answer.

## What it may do

- Flag any selected symbol on any stated grounds.
- Request more data (per-symbol regime history, event calendar).

## What it may **not** do

- Add a symbol to `alpha.data.selected`.
- Change a strategy's parameters — those live in the strategy module and the
  optimizer study store, not here.
- Write anything but `alpha.json`.
- Place, size or cancel an order.

## Failure behaviour

Non-blocking. With no valid answer every selected signal keeps its
deterministic score.

## Tests

`tests/test_optimizer_engine.py` (strategy selection surface),
`tests/test_stage_skills.py`
