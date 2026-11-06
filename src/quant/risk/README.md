# src/quant/risk

Safety gate. Every order intent passes here first.

- `gate.py`: single entry point for intents. Writes an audit row for pass and block. PARTIAL.
- `pretrade.py`, `price_band.py`, `liquidity.py`, `loss_breaker.py`: hard limits. PARTIAL (gate tests need `openalgo`).
- `kill_switch.py`: PARTIAL.
- `money_checks.py`: monetary entry limits (qty, notional, daily loss, exposure). PORTED from Blue's RiskGate, no Blue import. Parity test: `tests/parity/test_money_checks_parity.py`. Owner sign-off pending (decision 0018).
- `controls.py`: event blackout blocks entries and allows exits (X-8). DONE. Halt handler and overnight gap PENDING.
- `exits.py`, `squareoff.py`: stop, trailing, time exit, rollover. PARTIAL (expiry, X-1).

Rule: entries blocked on halt and blackout; exits and protection orders are never blocked.
See `docs/IMPLEMENTATION_PLAN.md` (Phase 1 exit check 1).
