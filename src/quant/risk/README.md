# src/quant/risk

Safety gate. Every order intent passes here first.

- `gate.py`: single entry point for intents. Writes an audit row for pass and block. PARTIAL.
- `pretrade.py`, `price_band.py`, `liquidity.py`, `loss_breaker.py`: hard limits. PARTIAL (gate tests need `openalgo`).
- `kill_switch.py`: PARTIAL.
- `blue_risk_gate.py`: wrapper around Blue's risk gate. PARTIAL.
- `controls.py`: event blackout blocks entries and allows exits (X-8). DONE. Halt handler and overnight gap PENDING.
- `exits.py`, `squareoff.py`: stop, trailing, time exit, rollover. PARTIAL (expiry, X-1).

Rule: entries blocked on halt and blackout; exits and protection orders are never blocked.
See `docs/IMPLEMENTATION_PLAN.md` (Phase 1 exit check 1).
