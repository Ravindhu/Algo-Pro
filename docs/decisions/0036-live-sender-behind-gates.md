# 0036. Live sender lives inside the strategy bot, behind gates

Status: proposed (2026-11-15). Owner sign-off still required before any live run.

## Context
Two live paths existed. `scripts/run/run_live.py` ran the old paper pipeline with a live
client and had none of the bot's stops, targets, daily-loss tracking or screen. The bot had
those, but refused live mode in code.

## Decision
Path A: the strategy bot is the only live order path.
- `execution/strategy_bot.py` gains `LiveSender` (inherits the analyze fill check) and
  `mode: live`. Live needs `live_enabled: true` AND an injected `live_gate` that passes.
- `scripts/run/run_strategy_bot_live.py` supplies the gate: `run_live.startup_checks`
  (approval, limits hash, complete limits, kill switch, calendar, API key) and
  the broker position reconciliation (`require_broker=True`: no broker reader blocks entries).
- Every live order still passes `OpenAlgoClient`'s live guard (approval, limits hash,
  compliance gate) and an approved `GateDecision` for the same intent.
- Committed config stays `mode: paper`, `live_enabled: false`.

## Consequences
- `run_live.py` is no longer the intended live entry point. Retiring it is a later step.
- Phase 2 (analyze-mode soak) and phase 3 (SEBI check, kill-switch drill, owner approval
  with `scripts/ops/approve_live.py`) are not done. Live stays off until they pass.
