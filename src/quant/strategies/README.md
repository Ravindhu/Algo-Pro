# strategies

Flow stage: Execution · strategy (parameter bounds only; no sizing or risk changes).
Status: SCAFFOLD. No strategy class is implemented yet.

Plan:
- One class per strategy, registered by name.
- Parameters load from `config/strategies/<name>.yaml`.
- Strategy code never changes risk limits or position size (AGENTS.md agent roster).
- First example config: `config/strategies/ocl_keylevel.yaml` (not yet wired).
