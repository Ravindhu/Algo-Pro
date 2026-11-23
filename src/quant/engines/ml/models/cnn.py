"""
ml/models/cnn.py — the 1-D convolutional candidate (torch, optional).

WHY A CNN ON A PRICE SERIES — AND WHEN IT IS A MISTAKE
------------------------------------------------------
A CNN's strength is local, translation-tolerant pattern detection: "a red
marubozu after a 3-day squeeze", "a volume dry-up at the 20-MA". On OHLCV
windows those patterns are exactly a 1-D convolution with small kernels over
the (close, high, low, volume) channels.

On DAILY INDEX bars the honest expectation is that this adds nothing over
GBM on the engineered features — the features already encode the patterns a
kernel would find. The CNN earns its place when the input is RAW and
high-frequency: 1-minute or 5-minute bars, where the pattern inventory is far
too large to hand-engineer. So: keep the window small (default 32 bars), keep
kernels small (3 and 5), and let the walk-forward report decide.

THE ARCHITECTURE (and why it is this shape)
-------------------------------------------
input  (32 bars x 4 channels)
  -> Conv1d(4->16, k=3) -> ReLU -> MaxPool(2)
  -> Conv1d(16->16, k=5) -> ReLU -> MaxPool(2)
  -> flatten -> Linear(~80 -> 16) -> ReLU -> Linear(16 -> 1)

Two conv layers is the minimum that can express "pattern followed by pattern"
(e.g. squeeze THEN breakout). Adding a third doubles the parameter count and,
on daily bars, buys memorisation, not skill.

The input is built from the same rows as every other candidate — the caller
maps each row into its (window x 4) tensor. A row without a full warm-up
window is UNUSABLE, and the model raises rather than padding with zeros:
zero-padding a warm-up fabricates a flat market that never existed.

Availability: torch required. ``ok=False`` with a reason when missing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .base import ModelSpec, has_torch

__all__ = ["build", "DEFAULT_PARAMS", "make_windows"]


DEFAULT_PARAMS: Dict[str, Any] = {
    "window": 32,          # bars of context per sample
    "channels": ("close", "high", "low", "volume"),
    "conv_channels": (16, 16),
    "kernels": (3, 5),
    "hidden": 16,
    "dropout": 0.2,
    "epochs": 60,
    "batch_size": 32,
    "lr": 1e-3,
    "weight_decay": 1e-4,
    "val_frac": 0.15,
    "patience": 8,
    "seed": 0,
}


def make_windows(rows: Sequence[Dict[str, Any]], *,
                 window: int = DEFAULT_PARAMS["window"],
                 channels: Sequence[str] = DEFAULT_PARAMS["channels"],
                 ) -> List[Optional[List[List[float]]]]:
    """Turn OHLCV rows into ``(window x len(channels))`` tensors per row.

    Row ``t``'s window covers bars ``t-window+1 .. t`` (inclusive), i.e. the
    model sees the SAME information a live bar would have. The final bar of
    the window is bar ``t`` itself — the bar the label is measured FROM, which
    is knowable at prediction time. The label looks FORWARD from there; that
    forward step belongs to labels.py, not here.

    Returns one entry per row; entries before the warm-up are None (not zero —
    see the module docstring).
    """
    out: List[Optional[List[List[float]]]] = [None] * len(rows)
    for t in range(window - 1, len(rows)):
        ok = True
        win: List[List[float]] = []
        for r in rows[t - window + 1:t + 1]:
            vals: List[float] = []
            for ch in channels:
                v = r.get(ch)
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    f = float("nan")
                if f != f or abs(f) == float("inf"):
                    ok = False
                    break
                vals.append(f)
            if not ok:
                break
            win.append(vals)
        if ok:
            out[t] = win
    return out


def build(**over: Any) -> ModelSpec:
    params = {**DEFAULT_PARAMS, **{k: v for k, v in over.items()
                                   if k in DEFAULT_PARAMS}}
    if not has_torch():
        return ModelSpec(
            name="cnn", family="cnn", ok=False,
            reason="torch not installed — CNN family needs it; "
                   "use GBM/RF (sklearn) or the ridge baseline on this machine",
            params=params)

    def _fit(X: Sequence[Any], y: Sequence[Optional[int]]) -> Any:
        import torch
        from torch import nn

        clean = [(xi, yi) for xi, yi in zip(X, y) if yi is not None and xi is not None]
        if len(clean) < 60:
            raise ValueError(f"cnn.fit: {len(clean)} usable windowed rows < 60")
        Xc = [xi for xi, _ in clean]
        yc = [float(yi) for _, yi in clean]

        torch.manual_seed(params["seed"])
        n_val = max(5, int(len(Xc) * params["val_frac"]))
        Xtr, ytr = Xc[:-n_val], yc[:-n_val]
        Xva, yva = Xc[-n_val:], yc[-n_val:]

        device = "cpu"
        c1, c2 = params["conv_channels"]
        k1, k2 = params["kernels"]
        w = params["window"]
        # pooled size after two MaxPool(2) layers (floor-div, ceil-free)
        pooled = max(1, w // 4)
        feat_dim = c2 * pooled

        net = nn.Sequential(
            nn.Conv1d(len(params["channels"]), c1, kernel_size=k1,
                      padding=k1 // 2), nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Conv1d(c1, c2, kernel_size=k2, padding=k2 // 2), nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Flatten(),
            nn.Linear(feat_dim, params["hidden"]), nn.ReLU(),
            nn.Dropout(params["dropout"]),
            nn.Linear(params["hidden"], 1),
        ).to(device)
        opt = torch.optim.Adam(net.parameters(), lr=params["lr"],
                               weight_decay=params["weight_decay"])
        lossf = nn.BCEWithLogitsLoss()

        Xt = torch.tensor(Xtr, dtype=torch.float32, device=device).transpose(1, 2)
        yt = torch.tensor(ytr, dtype=torch.float32, device=device).unsqueeze(1)
        Xv = torch.tensor(Xva, dtype=torch.float32, device=device).transpose(1, 2)
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
            net.load_state_dict(best_state)
        return {"net": net, "device": device}

    def _predict(fitted: Dict[str, Any], X: Sequence[Any]) -> List[float]:
        import torch

        net, device = fitted["net"], fitted["device"]
        usable = [xi for xi in X if xi is not None]
        if len(usable) != len(X):
            # The caller must window-filter before predicting; a None here is
            # a contract violation, not a degenerate case.
            raise ValueError("cnn.predict: None window in X — filter the warm-up first")
        net.eval()
        with torch.no_grad():
            Xd = torch.tensor(usable, dtype=torch.float32,
                              device=device).transpose(1, 2)
            return [float(p) for p in torch.sigmoid(net(Xd)).squeeze(1)]

    return ModelSpec(
        name="cnn", family="cnn",
        fit_fn=_fit, predict_fn=_predict, params=params,
        n_params_estimate=(
            len(params["channels"]) * params["conv_channels"][0]
            * params["kernels"][0] + params["conv_channels"][0]
            * params["conv_channels"][1] * params["kernels"][1]
            + (params["conv_channels"][1] * (params["window"] // 4) + 1)
            * params["hidden"] + params["hidden"]),
    )
