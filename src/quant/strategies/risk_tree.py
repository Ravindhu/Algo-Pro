"""Three-level risk tree (owner 2026-11-20, confirmed top-down).

Status: PHASE 2 IMPLEMENTED (budget module; not yet wired into the replay or the bot).

Every level splits its budget into 5 equal-count stages: 3 AGGRESSIVE slots, then a WAIT for the
matching conservative touch, then 2 CONSERVATIVE slots. The split is set by `aggressive_share`
(default 0.60). Owner-confirmed bounds: 0.50 to 0.80, so neither stage can be starved.

Levels, all as fractions of the symbol allocation (reference 4 % = 1.0):
    symbol (MTF cycle)  -> 5 HTF OCL slots
    each HTF OCL        -> 5 LTF OCL slots
    each LTF OCL        -> 5 entries (3 aggressive, 2 conservative)

Stop for every level is HTF L (owner). Sizing itself is in risk_ladder.sizing_stop / shares_for.
Unused slot budget is forfeited; carried shares add no budget (the caller handles carry).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

AGGRESSIVE_COUNT = 3
CONSERVATIVE_COUNT = 2
SHARE_MIN = 0.50
SHARE_MAX = 0.80
DEFAULT_AGGRESSIVE_SHARE = 0.60
REFERENCE_SYMBOL_PCT = 0.04  # 4 % = the reference the fractions were derived from


def validate_share(aggressive_share: float) -> float:
    """Owner-confirmed range. Anything outside it is refused, never clamped."""
    s = float(aggressive_share)
    if not (SHARE_MIN <= s <= SHARE_MAX):
        raise ValueError(f"aggressive_share must be between {SHARE_MIN} and {SHARE_MAX}, got {s}")
    return s


AGGRESSIVE = "aggressive"
CONSERVATIVE = "conservative"


def stage_slot(parent_budget: float, stage: str, aggressive_share: float) -> float:
    """One slot of a level, given its parent's budget (fraction of equity).

    aggressive slot   = parent x share / 3
    conservative slot = parent x (1 - share) / 2
    """
    share = validate_share(aggressive_share)
    if stage == AGGRESSIVE:
        return float(parent_budget) * share / AGGRESSIVE_COUNT
    if stage == CONSERVATIVE:
        return float(parent_budget) * (1.0 - share) / CONSERVATIVE_COUNT
    raise ValueError(f"unknown stage {stage!r}")


@dataclass(frozen=True)
class TreeBudgets:
    """Every budget in the tree, as fractions of equity. Keys are the parent stage, then the child stage."""
    allocation: float                       # symbol allocation (fraction of equity, 0.04 = 4 %)
    aggressive_share: float
    htf_slot: Dict[str, float]              # HTF OCL budget by its MTF stage
    ltf_slot: Dict[tuple, float]            # LTF OCL budget by (HTF stage, LTF stage)
    entry: Dict[tuple, float]               # one LTF entry by (HTF stage, LTF stage, entry stage)

    def as_percent(self) -> Dict[str, object]:
        return {
            "allocation_pct": self.allocation * 100,
            "htf_slot_pct": {k: v * 100 for k, v in self.htf_slot.items()},
            "ltf_slot_pct": {"/".join(k): v * 100 for k, v in self.ltf_slot.items()},
            "entry_pct": {"/".join(k): v * 100 for k, v in self.entry.items()},
        }


def tree_budgets(allocation_pct: float, aggressive_share: float = DEFAULT_AGGRESSIVE_SHARE) -> TreeBudgets:
    """The whole tree for one symbol allocation (percent, e.g. 4.0 or 1.0)."""
    if allocation_pct is None or float(allocation_pct) <= 0:
        raise ValueError("symbol allocation must be a positive percent")
    share = validate_share(aggressive_share)
    alloc = float(allocation_pct) / 100.0
    htf = {st: stage_slot(alloc, st, share) for st in (AGGRESSIVE, CONSERVATIVE)}
    ltf = {(h, l): stage_slot(htf[h], l, share)
           for h in (AGGRESSIVE, CONSERVATIVE) for l in (AGGRESSIVE, CONSERVATIVE)}
    entry = {(h, l, e): stage_slot(ltf[(h, l)], e, share)
             for h in (AGGRESSIVE, CONSERVATIVE) for l in (AGGRESSIVE, CONSERVATIVE)
             for e in (AGGRESSIVE, CONSERVATIVE)}
    return TreeBudgets(allocation=alloc, aggressive_share=share, htf_slot=htf, ltf_slot=ltf, entry=entry)


@dataclass
class LevelGate:
    """Stage machine for one level: 3 aggressive slots, a wait, then 2 conservative slots.

    Aggressive slots fire on aggressive touches while not waiting. After the 3rd aggressive slot the level
    waits. A conservative touch then fires one conservative slot (only while waiting). Nothing else opens."""
    aggressive_used: int = 0
    conservative_used: int = 0

    @property
    def waiting(self) -> bool:
        return self.aggressive_used >= AGGRESSIVE_COUNT and self.conservative_used < CONSERVATIVE_COUNT

    @property
    def closed(self) -> bool:
        return self.aggressive_used >= AGGRESSIVE_COUNT and self.conservative_used >= CONSERVATIVE_COUNT

    def on_aggressive_touch(self) -> Optional[str]:
        """Returns 'aggressive' when a slot fires, else None."""
        if self.aggressive_used >= AGGRESSIVE_COUNT:
            return None  # stage full: waiting for the conservative touch
        self.aggressive_used += 1
        return "aggressive"

    def on_conservative_touch(self) -> Optional[str]:
        """Returns 'conservative' when a slot fires, else None (not waiting, or no slots left)."""
        if not self.waiting:
            return None
        self.conservative_used += 1
        return "conservative"

    def to_doc(self) -> Dict[str, int]:
        return {"aggressive_used": self.aggressive_used, "conservative_used": self.conservative_used}

    @staticmethod
    def from_doc(d: Dict[str, int]) -> "LevelGate":
        return LevelGate(aggressive_used=int(d["aggressive_used"]), conservative_used=int(d["conservative_used"]))
