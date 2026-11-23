"""
optim - pure portfolio optimisers for the Portfolio engine.

Every optimiser answers the same question: *given expected returns and a risk
model, what weights?* They all share the same contract (see ``base.py``): take
a problem dict in, return weight dicts out, always honour box/turnover/beta/gross
constraints, and never raise.

Modules
-------
base            BaseOptimiser ABC, the ConstraintSpec, and the simplex clamp
constraints     feasible-set projection (box, turnover, beta, gross/net)
mean_variance   min-variance & max-Sharpe (closed form on the frontier)
risk_parity     true ERC (equal risk contribution) via cyclical coordination
hrp             Hierarchical Risk Parity + Nested Clustered Optimisation
black_litterman Black-Litterman posterior from alpha/ML/NLP views
cvar            mean-CVaR via subgradient descent (the tail-aware optimiser)
cost_aware      India cost model, no-trade region, cost-justified rebalance
benchmark       walk-forward optimiser comparison, net of India costs
significance    block-bootstrap CIs and paired tests (is the winner real?)
registry        name -> optimiser resolution + ``optimise()`` dispatcher

Dependency policy
-----------------
No numpy required: every optimiser is written against plain list-of-lists so it
runs in the bare ``openalgo==2.0.3`` environment. Large universes are not the
target here (sleeves, not 500 stocks); when the universe grows past ~50 the
engine can swap in a numpy/scipy implementation behind the same ABC.
"""
from . import (base, benchmark, black_litterman, constraints, cost_aware, cvar,
               hrp, mean_variance, registry, risk_parity, significance)

__all__ = [
    "base", "constraints", "mean_variance", "risk_parity", "hrp",
    "black_litterman", "cvar", "cost_aware", "benchmark", "significance",
    "registry",
]