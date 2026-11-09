# 0011 — Decouple src/quant from Blue_engine (staged)

Date: 2026-11-05
Status: ACCEPTED (owner choice: "staged decoupling")

## Context
AGENTS.md says Blue_engine is wrapped, never copied. The owner now wants one clean algo.
60 of 78 quant files load Blue functions at run time. Removing Blue in one step would break the optimizer, screener, and economics.

## Decision
Replace each Blue function that src/quant uses with local code, one at a time, in stages (plan: docs/DECOUPLING_PLAN.md).
Each replacement must pass a hand-checked test and a parity test against Blue. Blue_engine is removed last, after no quant code loads it.
Risk-critical items (order path, risk gate money checks) need separate owner review.

## Consequences
- This is a deliberate exception to AGENTS.md "wrap, never copy" for the functions in the plan.
- Some parity tests can only run on the owner machine (openalgo needed).
- Removing Blue_engine is a later decision, after stage 3.
