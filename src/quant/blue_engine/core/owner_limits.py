"""Apply the owner's hard limits (config/risk_limits.yaml) to the blue engine's raw config.

Why this exists: the blue runner (supervised and POV runs) reads its risk block from
``blue_engine/input/master.json``, which carries looser caps than the owner-approved
``config/risk_limits.yaml``. The owner's file is the source of truth, so for every
monetary cap both sources set, the STRICTER value wins. Owner values are never
loosened here.

Fail-closed rules:
- Owner YAML missing or unreadable -> ``blocked`` is set, so no entry is placed.
- Owner cap is null -> the gate's own policy applies for that cap (live runs
  without monetary caps are refused via ``require_live_ack``). Null does NOT
  switch the check off, and it does NOT add anything.
- Owner ``blocked: true`` -> ``blocked`` is set.

NULL SEMANTICS: ONE RULE FOR BOTH CAP FAMILIES (audit item 4, 2026-11-17)
-------------------------------------------------------------------------
This file's header used to say a null rejects entries, while the book-cap block
in ``config/risk_limits.yaml`` said a null switches that check off. Both now say
the same thing, and the code matches it:

  * a null cap is NOT SET. Nothing is added by this merge.
  * an UNSET cap makes its check unable to prove safety, so the check REFUSES
    (fail closed) when it runs: ``RiskGate`` rejects a scan ``live`` intent with
    no monetary cap (``require_live_ack``), ``portfolio_risk_checks`` refuses a
    null sector/beta cap, and ``RiskLimits.unset`` blocks a live start while any
    book cap is null.

No threshold is invented for a null cap, and no cap is silently disabled.
"""
from __future__ import annotations

import copy
import logging
import pathlib
from typing import Any, Dict, Optional

import yaml

_log = logging.getLogger("blue_engine.owner_limits")

#: Keys the blue RiskGate reads that also exist in config/risk_limits.yaml.
#: Null there is NOT SET (see the module docstring), never "check disabled".
MERGED_KEYS = ("max_order_quantity", "max_order_notional", "max_daily_loss", "max_open_exposure")

#: Every cap in the owner file. Used to report which caps are still unset.
OWNER_CAP_KEYS = MERGED_KEYS + ("max_strategy_loss", "max_adv_pct",
                                "max_symbols_per_portfolio", "max_sector_pct",
                                "max_beta", "max_cvar", "max_drawdown")

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


def unset_owner_caps(owner: Optional[Dict[str, Any]]) -> list:
    """Owner caps that are null or missing. An unset cap refuses when its check runs."""
    data = owner if isinstance(owner, dict) else {}
    return [key for key in OWNER_CAP_KEYS if _as_number(data.get(key)) is None]


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
            # Null = not set. Add nothing; the gate's check refuses on its own.
            continue
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
