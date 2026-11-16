"""Apply the owner's hard limits (config/risk_limits.yaml) to the blue engine's raw config.

Why this exists: the blue runner (supervised and POV runs) reads its risk block from
``blue_engine/input/master.json``, which carries looser caps than the owner-approved
``config/risk_limits.yaml``. The owner's file is the source of truth, so for every
monetary cap both sources set, the STRICTER value wins. Owner values are never
loosened here.

Fail-closed rules:
- Owner YAML missing or unreadable -> ``blocked`` is set, so no entry is placed.
- Owner cap is null -> nothing is added for that cap (the blue gate's own policy
  applies: live runs without monetary caps are refused via ``require_live_ack``).
- Owner ``blocked: true`` -> ``blocked`` is set.
"""
from __future__ import annotations

import copy
import logging
import pathlib
from typing import Any, Dict, Optional

import yaml

_log = logging.getLogger("blue_engine.owner_limits")

#: Keys the blue RiskGate reads that also exist in config/risk_limits.yaml.
MERGED_KEYS = ("max_order_quantity", "max_order_notional", "max_daily_loss", "max_open_exposure")

#: src/quant/blue_engine/core/owner_limits.py -> repo root is four levels up.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
OWNER_LIMITS_PATH = REPO_ROOT / "config" / "risk_limits.yaml"


def load_owner_limits(path: pathlib.Path = OWNER_LIMITS_PATH) -> Optional[Dict[str, Any]]:
    """Return the owner's limits as a dict, or None when the file is missing or invalid."""
    try:
        data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _as_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def apply_owner_limits(raw: Dict[str, Any], path: pathlib.Path = OWNER_LIMITS_PATH) -> Dict[str, Any]:
    """Return a copy of ``raw`` whose ``risk`` block is no looser than the owner's limits."""
    out = copy.deepcopy(raw)
    risk = out.get("risk") if isinstance(out.get("risk"), dict) else {}

    owner = load_owner_limits(path)
    if owner is None:
        _log.error("owner limits unreadable at %s; blocking entries (fail closed)", path)
        risk["blocked"] = True
        out["risk"] = risk
        return out

    for key in MERGED_KEYS:
        owner_value = _as_number(owner.get(key))
        if owner_value is None:
            continue  # null in the owner file: add nothing for this cap
        current = _as_number(risk.get(key))
        if current is None:
            # Config sets no cap (or a non-numeric one): the owner's cap applies.
            risk[key] = owner_value
        else:
            risk[key] = min(current, owner_value)

    if bool(owner.get("blocked", False)):
        risk["blocked"] = True
    risk.setdefault("blocked", False)

    out["risk"] = risk
    return out
