"""Conviction size tiers (A2) and hard-cap wiring (A3). Pure functions, no I/O except config load.

Flow stage: Fractional Kelly (conviction tier multiplier, applied under the hard caps)
Status: PHASE 2 IMPLEMENTED (A2 tiers, A3 cap wiring). Tiers are disabled by default in
config/conviction_tiers.yaml. Calibration report (A4) is pending.

Rules:
- Conviction can only reduce size below the hard caps. It never raises size above them.
- Final size = min(base * tier_multiplier * regime_factor, every hard cap).
- Regime factor in [0, 1] de-risks when market conditions deteriorate.
- When tiers are disabled, tier multiplier is 1.0 (flat sizing).
- Every decision returns an audit record, so the caller can log it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

import yaml

# repo root: src/quant/portfolio/tiers.py -> parents[3]
DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "conviction_tiers.yaml"


@dataclass(frozen=True)
class TierTable:
    enabled: bool
    bands: Tuple[Tuple[float, float], ...]   # (min_score, multiplier), sorted ascending
    max_multiplier: float


@dataclass(frozen=True)
class SizeDecision:
    size: float                 # final size, after all caps
    base_size: float
    conviction_score: float
    tier_multiplier: float      # 1.0 when tiers disabled
    regime_factor: float
    tiers_enabled: bool
    caps: dict                  # every hard cap that was applied
    binding: str                # what limited the size: a cap name, "conviction_tier", "regime", or "none"
    notes: tuple = field(default=())

    def as_audit_record(self) -> dict:
        """Flat dict for the audit log. Contains the inputs and the reason for the size."""
        return {
            "size": self.size,
            "base_size": self.base_size,
            "conviction_score": self.conviction_score,
            "tier_multiplier": self.tier_multiplier,
            "regime_factor": self.regime_factor,
            "tiers_enabled": self.tiers_enabled,
            "caps": dict(self.caps),
            "binding": self.binding,
        }


def _check_unit(name: str, value: float) -> float:
    v = float(value)
    if not math.isfinite(v) or not 0.0 <= v <= 1.0:
        raise ValueError(f"{name} must be a finite number in [0, 1], got {value}")
    return v


def load_tiers(path: Optional[Path] = None) -> TierTable:
    """Load and validate the tier config. Missing or bad config raises (fail loud at startup)."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("'enabled' must be true or false")
    max_mult = float(raw.get("max_multiplier", 1.0))
    if not math.isfinite(max_mult) or max_mult <= 0:
        raise ValueError("'max_multiplier' must be a positive number")
    bands_raw = raw.get("bands") or []
    bands = []
    for item in bands_raw:
        min_score = _check_unit("band min_score", item["min_score"])
        mult = float(item["multiplier"])
        if not math.isfinite(mult) or mult < 0:
            raise ValueError("band multiplier must be finite and >= 0")
        bands.append((min_score, min(mult, max_mult)))
    bands.sort(key=lambda b: b[0])
    if enabled and not bands:
        raise ValueError("tiers enabled but no bands defined")
    return TierTable(enabled=enabled, bands=tuple(bands), max_multiplier=max_mult)


def tier_multiplier(score: float, table: TierTable) -> float:
    """Multiplier for a score. 1.0 when disabled. 0.0 below the lowest band (no trade)."""
    if not table.enabled:
        return 1.0
    s = _check_unit("conviction score", score)
    mult = 0.0
    for min_score, band_mult in table.bands:
        if s >= min_score:
            mult = band_mult
    return min(mult, table.max_multiplier)


def decide_size(
    base_size: float,
    conviction_score: float,
    caps: Mapping[str, float],
    table: TierTable,
    regime_factor: float = 1.0,
) -> SizeDecision:
    """Combine base size, conviction tier, regime de-risk and hard caps into one size.

    caps: name -> maximum allowed size in the same units as base_size. At least one cap.
    """
    base = float(base_size)
    if not math.isfinite(base) or base < 0:
        raise ValueError("base_size must be a finite number >= 0")
    score = _check_unit("conviction score", conviction_score)
    regime = _check_unit("regime_factor", regime_factor)
    if not caps:
        raise ValueError("at least one hard cap is required")
    cap_values = {}
    for name, value in caps.items():
        v = float(value)
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"cap {name!r} must be finite and >= 0")
        cap_values[name] = v

    mult = tier_multiplier(score, table)
    candidate = base * mult * regime

    lowest_cap_name = min(cap_values, key=lambda n: cap_values[n])
    lowest_cap = cap_values[lowest_cap_name]
    final = min(candidate, lowest_cap)

    if candidate > lowest_cap:
        binding = lowest_cap_name          # a hard cap limited the size
    elif mult == 0.0 and base > 0:
        binding = "conviction_tier"        # score below the lowest band: no trade
    elif regime < 1.0 and base * mult > 0:
        binding = "regime"                 # regime de-risk limited the size
    else:
        binding = "none"

    notes = ()
    if not table.enabled:
        notes = ("tiers disabled: flat sizing",)
    return SizeDecision(
        size=final,
        base_size=base,
        conviction_score=score,
        tier_multiplier=mult,
        regime_factor=regime,
        tiers_enabled=table.enabled,
        caps=cap_values,
        binding=binding,
        notes=notes,
    )
