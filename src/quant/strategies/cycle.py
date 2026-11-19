"""Cycle replay for the five sequence strategies (owner rules 2026-11-19, rules doc section 11).

Flow stage: Strategy, Engulf sequence, Strategy bot cycle
Status: PHASE 1 IMPLEMENTED (2026-11-19). Pure logic, no orders. Bot wiring is phase 2.

Replays the whole cycle from closed bars, so no state is stored between cycles:
  MTF setup -> MTF touch -> HTF sweep -> HTF OCL forms and its key level is touched
  -> LTF OCL loop: aggressive touch = entry; conservative touch of the same OCL = entry (trail mode);
     a close below the LTF OCL low arms the wait for a new LTF OCL below that level (added entry).
Hard stop: the HTF OCL low. A close below it ends the HTF cycle (status htf_invalidated).
MTF invalidation: a close below the MTF setup low ends the whole cycle (status mtf_invalidated).
Exit mode: 'trail' if any conservative touch happened (ATR trail, no fixed target);
           'target' if only aggressive touches happened (target = avg + target_r x R, R = avg - HTF OCL low);
           'target_pending' if target_r is not yet set (owner: backtested best N, not known yet).
Assumptions (see rules doc section 10): HTF OCL key touch is required; entries are the newest LTF bars only in phase 2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from quant.strategies.owner_patterns import (NO_SWEEP_KINDS, _arrays, _newest_setup, _ocl_start,
                                            find_ocl_rrg, find_swing_low_sweep, setup_low)


@dataclass
class Entry:
    time: object
    price: float
    kind: str  # "aggressive" or "conservative"


@dataclass
class CycleResult:
    kind: str
    status: str  # waiting | open | htf_invalidated | mtf_invalidated
    htf_low: Optional[float] = None       # hard stop for every entry in this HTF cycle
    entries: List[Entry] = field(default_factory=list)
    avg_entry: Optional[float] = None
    mode: Optional[str] = None            # trail | target | target_pending
    target: Optional[float] = None


def run_cycle(kind: str, mtf, htf, ltf, target_r: Optional[float] = None) -> Optional[CycleResult]:
    """Replay the cycle on closed bars. Returns None when no MTF setup has been touched yet."""
    if len(mtf) < 3 or len(htf) < 4 or len(ltf) < 4:
        return None
    setup = _newest_setup(mtf, kind)
    if setup is None:
        return None
    end, level = setup
    pattern_low = setup_low(mtf, kind, end)
    mo, mh, ml, _ = _arrays(mtf)
    mc = mtf["close"].to_numpy(dtype=float)
    n_m = len(mtf) - 1
    touch = next((k for k in range(end + 1, n_m + 1) if ml[k] <= level <= mh[k]), None)
    if touch is None:
        return None
    touch_time = mtf.index[touch]
    inval_k = next((k for k in range(touch + 1, n_m + 1) if mc[k] < pattern_low), None)
    mtf_inval_time = mtf.index[inval_k] if inval_k is not None else None

    # HTF sweep (not needed for the bottom strategy)
    if kind in NO_SWEEP_KINDS:
        start_time = touch_time
    else:
        sweep = find_swing_low_sweep(htf, len(htf) - 1)
        if sweep is None or htf.index[sweep["sweep_index"]] < touch_time:
            return None
        start_time = htf.index[sweep["sweep_index"]]

    # HTF OCL formed after the sweep (newest first), and its key level touched after it forms
    _, hh, hl, _ = _arrays(htf)
    h_found = None
    for m in range(len(htf) - 1, 2, -1):
        if htf.index[m] < start_time:
            break
        o = find_ocl_rrg(htf, m)
        if o is None:
            continue
        touches = [p for p in range(m + 1, len(htf)) if hl[p] <= o["key_level"] <= hh[p]]
        if not touches:
            return None  # HTF OCL formed, key level not touched yet
        h_found = (m, o, touches[0])
        break
    if h_found is None:
        return None
    hm, hoc, hp = h_found
    htf_low = float(min(hl[_ocl_start(hm, hoc): hm + 1]))
    htf_key_time = htf.index[hp]

    # LTF loop, bar by bar after the HTF key touch
    _, lh, ll, lc = _arrays(ltf)
    n_l = len(ltf) - 1
    entries: List[Entry] = []
    cur = None
    prev_low = None
    any_cons = False
    status = "waiting"
    for k in range(n_l + 1):
        t = ltf.index[k]
        if t <= htf_key_time:
            continue
        if mtf_inval_time is not None and t >= mtf_inval_time:
            status = "mtf_invalidated"
            break
        if entries and lc[k] < htf_low:
            status = "htf_invalidated"
            break
        if cur is not None and k > cur["end"]:
            if lc[k] < cur["low"]:
                prev_low = cur["low"]  # stop-out level closed: arm the wait for a new LTF OCL below it
                cur = None
            else:
                if not cur["agg_touched"] and ll[k] <= cur["agg"] <= lh[k]:
                    entries.append(Entry(t, cur["agg"], "aggressive"))
                    cur["agg_touched"] = True
                elif cur["agg_touched"] and not cur["cons_touched"] and ll[k] <= cur["cons"] <= lh[k]:
                    entries.append(Entry(t, cur["cons"], "conservative"))
                    cur["cons_touched"] = True
                    any_cons = True
                continue
        if cur is None:
            o = find_ocl_rrg(ltf, k)
            if o is None:
                continue
            if prev_low is not None and not (o["key_level"] < prev_low):
                continue  # must form below the previous stop-out level
            s = _ocl_start(k, o)
            cur = {"end": k, "agg": o["key_level"], "cons": o["conservative"],
                   "low": float(min(ll[s: s + 3])), "agg_touched": False, "cons_touched": False}
            prev_low = None

    if not entries:
        return CycleResult(kind=kind, status=status, htf_low=htf_low)
    if status == "waiting":
        status = "open"
    avg = sum(e.price for e in entries) / len(entries)
    if any_cons:
        mode, target = "trail", None
    elif target_r is None:
        mode, target = "target_pending", None
    else:
        mode, target = "target", avg + target_r * (avg - htf_low)
    return CycleResult(kind=kind, status=status, htf_low=htf_low, entries=entries,
                       avg_entry=avg, mode=mode, target=target)
