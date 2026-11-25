"""Three-level risk tree (owner 2026-11-20, confirmed top-down).

Status: PHASE 2 IMPLEMENTED (budget module and stateless planner; not yet wired into the replay or the bot).

Every level splits its budget into 5 equal-count stages: 3 AGGRESSIVE slots, then a WAIT for the
matching conservative touch, then 2 CONSERVATIVE slots. The split is set by `aggressive_share`
(default 0.60). Owner-confirmed bounds: 0.30 to 0.80 (owner 2026-11-26 lowered the floor from 0.50), so neither stage can be starved.

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
SHARE_MIN = 0.30
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
class SlotGate:
    """Slot counter for one level (MTF over its HTF OCLs; HTF over its LTF OCLs).

    3 aggressive slots fire when a new OCL forms. After the 3rd, the level waits. A conservative touch arms
    one conservative slot. The next OCL to form takes it (owner: 'wait for the conservative key level, then a
    new OCL forms'). 2 conservative slots in total. Nothing else fires."""
    aggressive_used: int = 0
    conservative_used: int = 0
    armed: bool = False

    @property
    def waiting(self) -> bool:
        return self.aggressive_used >= AGGRESSIVE_COUNT and self.conservative_used < CONSERVATIVE_COUNT

    @property
    def closed(self) -> bool:
        return self.aggressive_used >= AGGRESSIVE_COUNT and self.conservative_used >= CONSERVATIVE_COUNT

    def on_conservative_touch(self) -> bool:
        """Arms the next conservative slot. Only while waiting. Returns True when armed."""
        if self.waiting and not self.armed:
            self.armed = True
            return True
        return False

    def on_new_ocl(self) -> Optional[str]:
        """A new OCL forms. Returns the stage of the slot it takes, or None (refused)."""
        if self.aggressive_used < AGGRESSIVE_COUNT:
            self.aggressive_used += 1
            return AGGRESSIVE
        if self.armed and self.conservative_used < CONSERVATIVE_COUNT:
            self.armed = False
            self.conservative_used += 1
            return CONSERVATIVE
        return None

    def to_doc(self) -> Dict[str, object]:
        return {"aggressive_used": self.aggressive_used, "conservative_used": self.conservative_used,
                "armed": self.armed}

    @staticmethod
    def from_doc(d: Dict[str, object]) -> "SlotGate":
        return SlotGate(aggressive_used=int(d["aggressive_used"]), conservative_used=int(d["conservative_used"]),
                        armed=bool(d.get("armed", False)))


# ---- planner -------------------------------------------------------------------------------------
# Events, in time order (produced by the replay / bot):
#   ("mtf_cons_touch",)            MTF conservative key level touched
#   ("htf_ocl", ocl_id)            a new HTF OCL formed (its key touched) inside the MTF cycle
#   ("htf_cons_touch",)            the current HTF OCL's conservative key level touched
#   ("ltf_ocl", ocl_id)            a new LTF OCL formed inside the current HTF OCL
#   ("ltf_agg_touch", ocl_id)      the LTF OCL's aggressive level touched
#   ("ltf_cons_touch", ocl_id)     the same LTF OCL's conservative level touched
#   ("ltf_invalidated", ocl_id)    the LTF OCL closed (its untouched budget is forfeited)
#   ("htf_invalidated",)           the current HTF OCL closed (its LTF gate ends)
@dataclass(frozen=True)
class Decision:
    event: tuple
    level: str                 # mtf | htf | ltf
    stage: Optional[str]       # aggressive | conservative | leaf_aggressive | leaf_conservative | None
    fraction: float            # budget as a fraction of equity (0 when refused)
    refused: Optional[str]     # None, or why nothing was allocated
    slot: Optional[int] = None  # slot number at its level: aggressive 1-3, conservative 1-2 (None when refused)
    # LTF touches only: ((mtf stage, mtf slot, mtf budget) or None, (htf stage, htf slot, htf budget) or None,
    # leaf stage) of the OCL's chain. Budgets are fractions of equity, as in `fraction`.
    context: tuple = ()


def _slot_no(gate: "SlotGate", stage: str) -> int:
    """Slot number a stage just took from its gate (call right after on_new_ocl)."""
    return gate.aggressive_used if stage == AGGRESSIVE else gate.conservative_used


def plan(events, allocation_pct: float, aggressive_share: float = DEFAULT_AGGRESSIVE_SHARE) -> list:
    """Budget for every event. Stateless: the same events always give the same decisions.
    An LTF OCL's budget is fixed when it forms; its aggressive touch uses share of it and its conservative
    touch uses (1 - share) of it. Each touch is used at most once, and only while its OCL is open."""
    t = tree_budgets(allocation_pct, aggressive_share)
    share = t.aggressive_share
    mtf = SlotGate()
    htf = None                 # SlotGate of the current HTF OCL (its LTF slots)
    htf_stage = None           # stage the current HTF OCL took from the MTF gate
    mtf_ctx = None             # (stage, slot) the current MTF OCL took
    htf_ctx = None             # (stage, slot) the current HTF OCL's LTF OCL took (set per LTF OCL)
    ltf = {}                   # open LTF OCLs: id -> {"budget", "agg", "cons", "open", "ctx"}
    out = []
    for ev in events:
        kind = ev[0]
        if kind == "mtf_cons_touch":
            armed = mtf.on_conservative_touch()
            out.append(Decision(ev, "mtf", None, 0.0, None if armed else "not_waiting"))
        elif kind == "htf_ocl":
            stage = mtf.on_new_ocl()
            if stage is None:
                out.append(Decision(ev, "mtf", None, 0.0, "mtf_slots_used_or_not_armed"))
                htf, htf_stage, mtf_ctx = None, None, None
                continue
            htf_stage = stage
            htf = SlotGate()
            htf_ctx = None
            mtf_ctx = (stage, _slot_no(mtf, stage), t.htf_slot[stage])
            out.append(Decision(ev, "mtf", stage, t.htf_slot[stage], None, slot=mtf_ctx[1]))
        elif kind == "htf_cons_touch":
            if htf is None:
                out.append(Decision(ev, "htf", None, 0.0, "no_open_htf_ocl"))
            else:
                armed = htf.on_conservative_touch()
                out.append(Decision(ev, "htf", None, 0.0, None if armed else "not_waiting"))
        elif kind == "ltf_ocl":
            if htf is None:
                out.append(Decision(ev, "ltf", None, 0.0, "no_open_htf_ocl"))
                continue
            stage = htf.on_new_ocl()
            if stage is None:
                out.append(Decision(ev, "ltf", None, 0.0, "htf_slots_used_or_not_armed"))
                continue
            budget = stage_slot(t.htf_slot[htf_stage], stage, share)
            htf_ctx = (stage, _slot_no(htf, stage), budget)
            ltf[ev[1]] = {"budget": budget, "stage": stage, "agg": False, "cons": False, "open": True,
                          "ctx": (mtf_ctx, htf_ctx)}
            out.append(Decision(ev, "htf", stage, budget, None, slot=htf_ctx[1]))
        elif kind == "ltf_agg_touch":
            o = ltf.get(ev[1])
            if o is None or not o["open"]:
                out.append(Decision(ev, "ltf", None, 0.0, "no_open_ltf_ocl"))
            elif o["agg"]:
                out.append(Decision(ev, "ltf", None, 0.0, "already_used"))
            else:
                o["agg"] = True
                out.append(Decision(ev, "ltf", "leaf_aggressive", o["budget"] * share, None,
                                    context=(o["ctx"][0], o["ctx"][1], "leaf_aggressive")))
        elif kind == "ltf_cons_touch":
            o = ltf.get(ev[1])
            if o is None or not o["open"]:
                out.append(Decision(ev, "ltf", None, 0.0, "no_open_ltf_ocl"))
            elif o["cons"]:
                out.append(Decision(ev, "ltf", None, 0.0, "already_used"))
            else:
                o["cons"] = True
                out.append(Decision(ev, "ltf", "leaf_conservative", o["budget"] * (1.0 - share), None,
                                    context=(o["ctx"][0], o["ctx"][1], "leaf_conservative")))
        elif kind == "ltf_invalidated":
            o = ltf.get(ev[1])
            if o is not None:
                o["open"] = False
            out.append(Decision(ev, "ltf", None, 0.0, None))
        elif kind == "htf_invalidated":
            htf, htf_stage = None, None
            out.append(Decision(ev, "htf", None, 0.0, None))
        else:
            raise ValueError(f"unknown event {kind!r}")
    return out


