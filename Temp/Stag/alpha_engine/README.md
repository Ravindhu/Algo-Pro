# Retired: AlphaEngine scoring stage

Parked 2026-11-24 per owner decision. The macro pipeline no longer runs this
stage. Strategy signals (src/quant/engines/alpha/signals, Statergy) are the
trading strategy and stay in place. See
docs/IMPLEMENTATION_PLAN_2026-11-24_RETIRE_ALPHA.md.

## Also parked (2026-11-24)

- `models_alpha_engine.py` — the advice-only wrapper (`quant.models.alpha_engine`)
  that loaded the scoring class. Its three tests were removed from
  `src/quant/tests/test_eight_wraps.py`.
