"""
ml/models/mlp.py — the shallow neural-net candidate (torch, optional).

WHERE A NEURAL NET ACTUALLY EARNS ITS PLACE ON DAILY BARS
---------------------------------------------------------
The honest answer: usually nowhere on 3 years of daily index bars. An MLP on
15 tabular features is roughly a flexible ridge; on ~700 training rows it will
either underfit (too small) or memorise noise (too big). It is shipped as a
CANDIDATE, not a default, so the desk can *measure* whether it adds anything
over ridge/GBM — and the walk-forward report is the measurement.

WHERE IT IS A REAL CHOICE
-------------------------
1. When the feature set becomes genuinely high-dimensional and heterogeneous
   (per-symbol panels, macro surprises, news embeddings) — the regime where
   linear models saturate.
2. As the encoder inside the CNN (see ``cnn.py``) and the policy/value nets
   inside the RL agent (see ``rl.py``): those NEED a differentiable function,
   and this file provides the training loop skeleton they reuse.

ARCHITECTURE — deliberately tiny
--------------------------------
[ n_features -> 32 -> 16 -> 1 ] with ReLU and a dropout of 0.2, trained with
Adam on binary cross-entropy, early-stopped on a validation slice of the TRAIN
block (never the test block — that would be a leak through the back door).
Parameters: ~15*32 + 32*16 + 16 ≈ 1,000 — small enough that the deflation
audit's "huge grid on a small sample" flag does not fire just from width.

Availability: torch required. ``ok=False`` with a reason when missing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .base import ModelSpec, has_torch

__all__ = ["build", "DEFAULT_PARAMS"]


DEFAULT_PARAMS: Dict[str, Any] = {
    "hidden": (32, 16),
    "dropout": 0.2,
    "epochs": 60,
    "batch_size": 32,
    "lr": 1e-3,
    "weight_decay": 1e-4,
    "val_frac": 0.15,          # carved from the TRAIN block, never from test
    "patience": 8,
    "seed": 0,
}


def build(**over: Any) -> ModelSpec:
    params = {**DEFAULT_PARAMS, **{k: v for k, v in over.items()
                                   if k in DEFAULT_PARAMS}}
    if not has_torch():
        return ModelSpec(
            name="mlp", family="neural_net", ok=False,
            reason="torch not installed — MLP/CNN/RL families need it; "
                   "use ridge/GBM/RF (sklearn) on this machine",
            params=params)

    def _fit(X: Sequence[Sequence[float]], y: Sequence[Optional[int]]) -> Any:
        import torch
        from torch import nn

        clean = [(xi, yi) for xi, yi in zip(X, y) if yi is not None]
        if len(clean) < 40:
            raise ValueError(f"mlp.fit: {len(clean)} usable rows < 40")
        Xc = [list(map(float, xi)) for xi, _ in clean]
        yc = [float(yi) for _, yi in clean]

        torch.manual_seed(params["seed"])
        n_val = max(5, int(len(Xc) * params["val_frac"]))
        Xtr, ytr = Xc[:-n_val], yc[:-n_val]
        Xva, yva = Xc[-n_val:], yc[-n_val:]

        device = "cpu"  # CPU is faster than a GPU for this size and keeps runs reproducible
        dims = [len(Xtr[0]), *params["hidden"], 1]
        layers: List[Any] = []
        for a, b in zip(dims, dims[1:]):
            layers.append(nn.Linear(a, b))
            if b != 1:
                layers += [nn.ReLU(), nn.Dropout(params["dropout"])]
        net = nn.Sequential(*layers).to(device)
        opt = torch.optim.Adam(net.parameters(), lr=params["lr"],
                               weight_decay=params["weight_decay"])
        lossf = nn.BCEWithLogitsLoss()

        Xt = torch.tensor(Xtr, dtype=torch.float32, device=device)
        yt = torch.tensor(ytr, dtype=torch.float32, device=device).unsqueeze(1)
        Xv = torch.tensor(Xva, dtype=torch.float32, device=device)
        yv = torch.tensor(yva, dtype=torch.float32, device=device).unsqueeze(1)

        best_state, best_loss, bad = None, float("inf"), 0
        bs = params["batch_size"]
        for _epoch in range(params["epochs"]):
            net.train()
            perm = torch.randperm(len(Xt))
            for i in range(0, len(Xt), bs):
                idx = perm[i:i + bs]
                opt.zero_grad()
                loss = lossf(net(Xt[idx]), yt[idx])
                loss.backward()
                opt.step()
            net.eval()
            with torch.no_grad():
                vloss = float(lossf(net(Xv), yv))
            if vloss < best_loss - 1e-5:
                best_loss, bad = vloss, 0
                best_state = {k: v.clone() for k, v in net.state_dict().items()}
            else:
                bad += 1
                if bad >= params["patience"]:
                    break
        if best_state is not None:
            net.load_state_dict(best_state)   # early-stop restores the best, not the last
        return {"net": net, "device": device}

    def _predict(fitted: Dict[str, Any], X: Sequence[Sequence[float]]) -> List[float]:
        import torch

        net, device = fitted["net"], fitted["device"]
        net.eval()
        with torch.no_grad():
            Xd = torch.tensor([list(map(float, r)) for r in X],
                              dtype=torch.float32, device=device)
            return [float(p) for p in torch.sigmoid(net(Xd)).squeeze(1)]

    return ModelSpec(
        name="mlp", family="neural_net",
        fit_fn=_fit, predict_fn=_predict, params=params,
        n_params_estimate=sum(a * b + b for a, b in
                              zip([15, *params["hidden"]], [*params["hidden"], 1])),
    )
