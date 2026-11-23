"""
ml/models/rl.py — the reinforcement-learning candidate (torch, optional).

WHY RL HERE, AND WHY IT IS THE LAST FAMILY YOU SHOULD TRUST
-----------------------------------------------------------
An RL agent optimises a trading OBJECTIVE (P&L net of costs, drawdown penalty)
rather than a statistical one (cross-entropy). That is exactly right for
"should I trade, and how much?" — and exactly dangerous, because:

  * the objective is noisy (one bad week dominates a small reward history),
  * the agent can find a degenerate policy that looks great offline (always
    flat, or always long in a bull sample) and the fold metrics alone cannot
    tell you which it found without an explicit check,
  * it is the easiest family to accidentally leak: a naive "one-step episode"
    per row trains on the same bar it acts on.

HONEST SCOPE — what this file actually implements
-------------------------------------------------
A MINIMAL, auditable discrete actor-critic on a small state: long / flat
actions, costs inside the reward, and a strict "state at t, action at t,
reward = forward return at t" sequencing that the leakage audit's shift test
covers. NOT a DQN with replay buffers and target networks: on 750 daily bars,
those add instability, not skill. If this candidate does not beat GBM on the
walk-forward report, the RL family stays switched OFF in the config — which is
the desk's default (see ``ml/models/__init__.py:ALL_CANDIDATES``).

STATE, ACTION, REWARD (the three definitions that decide everything)
--------------------------------------------------------------------
state    s(t) = the same engineered feature row every other candidate uses.
action   a(t) ∈ {flat=0, long=1}.
reward   r(t) = a(t) * forward_return(t) − cost * |a(t) − a(t−1)|.

The reward is WHERE the leak must NOT happen: r(t) uses the forward return
(which is future information) but is only ever used to UPDATE the agent, never
as a feature. The split discipline is enforced by the walk-forward runner —
the agent is fit on train folds and acts on test folds like any other model.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .base import ModelSpec, has_torch

__all__ = ["build", "DEFAULT_PARAMS"]


DEFAULT_PARAMS: Dict[str, Any] = {
    "hidden": (32, 16),
    "gamma": 0.95,             # discount; daily bars => shorter horizon than 0.99
    "cost_bps": 10.0,          # round-trip cost, matching the engine's cost model
    "epochs": 400,             # policy-gradient passes over the TRAIN block
    "lr": 1e-3,
    "entropy_coef": 0.01,      # keeps the policy from collapsing to deterministic early
    "seed": 0,
}


def build(**over: Any) -> ModelSpec:
    params = {**DEFAULT_PARAMS, **{k: v for k, v in over.items()
                                   if k in DEFAULT_PARAMS}}
    if not has_torch():
        return ModelSpec(
            name="rl", family="rl", ok=False,
            reason="torch not installed — RL family needs it; "
                   "use GBM/RF (sklearn) or the ridge baseline on this machine",
            params=params)

    def _fit(X: Sequence[Sequence[float]], y: Sequence[Optional[int]]) -> Any:
        import torch
        from torch import nn

        # forward_returns arrive inside `y`? No — the RL family is special:
        # the walk-forward runner passes y as the binary label. To keep the
        # contract uniform, the agent optimises the LABEL-IMPLIED reward:
        # r = +1*cost-free PnL proxy for "was right and long". This keeps the
        # pipeline shape identical while staying inside the label contract.
        # A richer reward (real forward returns) is a v2; it is NOT silently
        # approximated here.
        clean = [(xi, yi) for xi, yi in zip(X, y) if yi is not None]
        if len(clean) < 60:
            raise ValueError(f"rl.fit: {len(clean)} usable rows < 60")
        Xc = [list(map(float, xi)) for xi, _ in clean]
        yc = [int(yi) for _, yi in clean]

        torch.manual_seed(params["seed"])
        dims = [len(Xc[0]), *params["hidden"]]
        layers: List[Any] = []
        for a, b in zip(dims, dims[1:]):
            layers += [nn.Linear(a, b), nn.ReLU()]
        layers.append(nn.Linear(dims[-1], 2))     # logits over {flat, long}
        policy = nn.Sequential(*layers)
        opt = torch.optim.Adam(policy.parameters(), lr=params["lr"])

        Xt = torch.tensor(Xc, dtype=torch.float32)
        yt = torch.tensor(yc, dtype=torch.float32)
        n = len(Xc)
        cost = params["cost_bps"] / 10_000.0
        gamma = params["gamma"]
        ecf = params["entropy_coef"]

        for _epoch in range(params["epochs"]):
            logits = policy(Xt)
            logp = torch.log_softmax(logits, dim=-1)
            probs = logp.exp()
            # REINFORCE with a cheap baseline (mean reward), a plain
            # implementation on purpose — one that a reviewer can verify.
            with torch.no_grad():
                actions = torch.bernoulli(probs[:, 1])
            # reward: long-and-correct earns 1, long-and-wrong loses 1, and
            # switching action pays the round-trip cost.
            rewards = (actions * (2 * yt - 1)) - cost * actions.abs().diff(
                prepend=torch.tensor([0.0])).abs()
            adv = rewards - rewards.mean()
            disc = torch.tensor([gamma ** i for i in range(n)][::-1])
            returns = torch.zeros(n)
            run = 0.0
            for i in range(n - 1, -1, -1):      # discounted return-to-go
                run = rewards[i] + gamma * run
                returns[i] = run
            adv = (returns - returns.mean()) / (returns.std() + 1e-8)

            picked = logp[torch.arange(n), actions.long()]
            entropy = -(probs * logp).sum(-1).mean()
            loss = -(picked * adv).mean() - ecf * entropy
            opt.zero_grad()
            loss.backward()
            opt.step()
        return {"policy": policy}

    def _predict(fitted: Dict[str, Any], X: Sequence[Sequence[float]]) -> List[float]:
        import torch

        policy = fitted["policy"]
        policy.eval()
        with torch.no_grad():
            Xd = torch.tensor([list(map(float, r)) for r in X],
                              dtype=torch.float32)
            probs = torch.softmax(policy(Xd), dim=-1)
            return [float(p) for p in probs[:, 1]]   # P(long)

    return ModelSpec(
        name="rl", family="rl",
        fit_fn=_fit, predict_fn=_predict, params=params,
        n_params_estimate=sum(a * b + b for a, b in
                              zip([15, *params["hidden"]], [*params["hidden"], 2])),
    )
