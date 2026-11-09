# strategies

Flow stage: Execution · strategy (weights only; no sizing or risk changes).
Status: PHASE 5 IMPLEMENTED (interface + one reference strategy).

What is here:
- `base.py`: `Strategy` interface. Sees history up to the decision date only. Returns long-only weights.
  `check_weights` rejects NaN, negative weights and totals above 1.
- `momentum.py`: `MomentumStrategy`, top-N positive momentum, equal weight. Reference only.

Not here yet: `config/strategies/<name>.yaml` loading and the `ocl_keylevel` strategy.
Strategy code never changes risk limits or position size (AGENTS.md agent roster).
Run it with `research/strategy_backtest.py`, search it with `research/param_search.py`,
and review the result with `feedback/strategy_loop.py` (advice only).
