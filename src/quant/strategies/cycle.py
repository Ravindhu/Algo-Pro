"""Cycle replay for the five sequence strategies (owner rules 2026-11-19, rules doc sections 11 and 12).

Flow stage: Strategy, Engulf sequence, Strategy bot cycle
Status: PHASE 2 IMPLEMENTED (2026-11-19): next HTF cycle, order freshness, bot wiring (paused flag and cycle id). Pure logic, no orders.

Replays the whole history from closed bars, so no state is stored between cycles:
  MTF setup -> MTF touch -> HTF sweep (once) -> HTF OCL forms after the sweep and its key is touched
  -> LTF OCL loop after the HTF key touch:
       aggressive touch = entry; conservative touch of the same OCL = entry (trail mode);
       a close below the LTF OCL low arms the wait for a new LTF OCL below that level (added entry).
  -> HTF invalidated (an HTF candle closes below the HTF OCL low): the cycle ends. A new HTF OCL must form
     below that invalidated price, its key must be touched, and the LTF loop runs again (no new sweep).
  -> MTF invalidated (an MTF candle closes below the MTF setup low): the whole replay ends.
Exit mode per cycle: 'trail' after any conservative touch; otherwise 'target' (avg + target_r x R) or 'target_pending'.
Freshness: fresh_entries() returns only the entries on the newest closed LTF bar (the only ones that can become orders).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from quant.strategies.owner_patterns import (NO_SWEEP_KINDS, _arrays, _newest_setup, _ocl_start,
                                            find_ocl_rrg, first_sweep_after, setup_low)


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
    htf_key_time: object = None           # identifies this HTF cycle (the HTF bar that touched the key)
    ltf_paused: bool = False              # an LTF stop-out closed and no new LTF OCL has formed since


def _mtf_touch(kind: str, mtf) -> Optional[Tuple[object, Optional[object], float]]:
    """(touch_time, mtf_invalidation_time or None, pattern_low) for the newest touched MTF setup, else None."""
    if len(mtf) < 3:
        return None
    setup = _newest_setup(mtf, kind)
    if setup is None:
        return None
    end, level = setup
    pattern_low = setup_low(mtf, kind, end)
    _, mh, ml, _ = _arrays(mtf)
    mc = mtf["close"].to_numpy(dtype=float)
    n_m = len(mtf) - 1
    touch = next((k for k in range(end + 1, n_m + 1) if ml[k] <= level <= mh[k]), None)
    if touch is None:
        return None
    inval = next((k for k in range(touch + 1, n_m + 1) if mc[k] < pattern_low), None)
    return mtf.index[touch], (mtf.index[inval] if inval is not None else None), pattern_low


def _htf_stage(htf, start_time, floor: Optional[float]):
    """First HTF OCL (oldest first) that forms at or after start_time, with its key touched after it forms.
    If floor is given, the OCL key must be below it (new HTF OCL below the invalidated price).
    Returns (end_index, ocl, touch_index) or None."""
    _, hh, hl, _ = _arrays(htf)
    for m in range(3, len(htf)):
        if htf.index[m] < start_time:
            continue
        o = find_ocl_rrg(htf, m)
        if o is None:
            continue
        if floor is not None and not (o["key_level"] < floor):
            continue
        touches = [p for p in range(m + 1, len(htf)) if hl[p] <= o["key_level"] <= hh[p]]
        if not touches:
            return None  # OCL formed, key not touched yet
        return m, o, touches[0]
    return None


def _htf_low(htf, m: int, ocl: dict) -> float:
    _, _, hl, _ = _arrays(htf)
    return float(min(hl[_ocl_start(m, ocl): m + 1]))


def _htf_invalid_time(htf, after_index: int, htf_low: float):
    """First HTF candle after after_index that closes below htf_low (owner: HTF invalidated = HTF close)."""
    hc = htf["close"].to_numpy(dtype=float)
    for p in range(after_index + 1, len(htf)):
        if hc[p] < htf_low:
            return htf.index[p]
    return None


def _ltf_loop(ltf, after_time, htf_low: float, htf_inval_time, mtf_inval_time):
    """Run the LTF OCL loop after after_time.
    Returns (entries, any_conservative, status, end_time, paused). paused: waiting for a new LTF OCL."""
    _, lh, ll, lc = _arrays(ltf)
    n_l = len(ltf) - 1
    entries: List[Entry] = []
    cur = None
    prev_low = None
    any_cons = False
    status = "waiting"
    end_time = None
    for k in range(n_l + 1):
        t = ltf.index[k]
        if t <= after_time:
            continue
        if mtf_inval_time is not None and t >= mtf_inval_time:
            status = "mtf_invalidated"
            break
        if htf_inval_time is not None and t >= htf_inval_time:
            status = "htf_invalidated"
            end_time = htf_inval_time
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
    paused = prev_low is not None and status == "waiting"
    return entries, any_cons, status, end_time, paused


def _finish(kind, status, htf_low, entries, any_cons, target_r, htf_key_time=None, paused=False):
    if not entries:
        return CycleResult(kind=kind, status=status, htf_low=htf_low, htf_key_time=htf_key_time,
                           ltf_paused=paused)
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
                       avg_entry=avg, mode=mode, target=target,
                       htf_key_time=htf_key_time, ltf_paused=paused)


def run_cycles(kind: str, mtf, htf, ltf, target_r: Optional[float] = None) -> List[CycleResult]:
    """All HTF cycles in the history, in order. Empty list when no MTF setup has been touched."""
    if len(mtf) < 3 or len(htf) < 4 or len(ltf) < 4:
        return []
    got = _mtf_touch(kind, mtf)
    if got is None:
        return []
    touch_time, mtf_inval_time, _ = got
    if kind in NO_SWEEP_KINDS:
        start = touch_time
    else:
        sweep = first_sweep_after(htf, touch_time)
        if sweep is None:
            return []
        start = htf.index[sweep[1]]
    results: List[CycleResult] = []
    floor = None
    while True:
        h = _htf_stage(htf, start, floor)
        if h is None:
            break
        m, ocl, hp = h
        htf_low = _htf_low(htf, m, ocl)
        key_time = htf.index[hp]
        inval_time = _htf_invalid_time(htf, hp, htf_low)
        entries, any_cons, status, _, paused = _ltf_loop(ltf, key_time, htf_low, inval_time, mtf_inval_time)
        results.append(_finish(kind, status, htf_low, entries, any_cons, target_r,
                               htf_key_time=key_time, paused=paused))
        if status != "htf_invalidated":
            break  # open, waiting or MTF invalidated: this is the end of the replay
        floor = htf_low
        start = inval_time  # the next HTF OCL must form after the invalidation
    return results


def run_cycle(kind: str, mtf, htf, ltf, target_r: Optional[float] = None) -> Optional[CycleResult]:
    """The last cycle in the history (the current one), or None when no cycle exists."""
    results = run_cycles(kind, mtf, htf, ltf, target_r)
    return results[-1] if results else None


def fresh_entries(result: Optional[CycleResult], newest_ltf_time) -> List[Entry]:
    """Only entries on the newest closed LTF bar may become orders (owner rule, 2026-11-19)."""
    if result is None:
        return []
    return [e for e in result.entries if e.time == newest_ltf_time]
