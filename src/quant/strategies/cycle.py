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

import copy
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from quant.strategies.owner_patterns import (NO_SWEEP_KINDS, _arrays, _newest_setup, _ocl_start,
                                            find_ocl_rrg, first_sweep_after, setup_low)
from quant.strategies.risk_ladder import RiskLadder


@dataclass
class Entry:
    time: object
    price: float
    kind: str  # "aggressive" or "conservative"
    ocl: Optional[str] = None  # id of the LTF OCL this entry belongs to (its C3 time, as text)


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
    # Ordered LTF events for the risk ladder (rules doc 11 and plan P3):
    # (event, ocl_id, price, time) with event in aggressive | conservative | invalidated.
    events: List[Tuple[str, str, float, object]] = field(default_factory=list)


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
    events: List[Tuple[str, str, float, object]] = []
    events: List[Tuple[str, str, float, object]] = []
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
                events.append(("invalidated", cur["id"], cur["low"], t))
                cur = None
            else:
                if not cur["agg_touched"] and ll[k] <= cur["agg"] <= lh[k]:
                    entries.append(Entry(t, cur["agg"], "aggressive", cur["id"]))
                    events.append(("aggressive", cur["id"], cur["agg"], t))
                    cur["agg_touched"] = True
                elif cur["agg_touched"] and not cur["cons_touched"] and ll[k] <= cur["cons"] <= lh[k]:
                    entries.append(Entry(t, cur["cons"], "conservative", cur["id"]))
                    events.append(("conservative", cur["id"], cur["cons"], t))
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
            cur = {"id": str(ltf.index[k]), "end": k, "agg": o["key_level"], "cons": o["conservative"],
                   "low": float(min(ll[s: s + 3])), "agg_touched": False, "cons_touched": False}
            prev_low = None
    paused = prev_low is not None and status == "waiting"
    return entries, any_cons, status, end_time, paused, events


def ladder_plan(events, htf_low, equity, loss_ceiling, atr_by_time=None):
    """The bot's risk ladder run on the replay events (plan P4). Returns the planned orders.

    atr_by_time maps an LTF bar time to ATR(14) on that bar (owner 2026-11-20 sizing stop). A touch on a
    bar with no ATR gets no order (fail closed), the same as the bot.
    Full fill is assumed for every order (the replay has no fills), so nothing is carried here.
    Live fills (POV) can differ, so the live average can differ from this one."""
    if equity is None or loss_ceiling is None:
        return []  # fail closed: no sizing without the capital and the owner ceiling
    ladder = RiskLadder(equity=float(equity), htf_low=float(htf_low), loss_ceiling=loss_ceiling)
    orders = []
    for kind, ocl_id, price, t in events:
        if kind == "invalidated":
            ladder.invalidate(ocl_id)
            continue
        if kind not in ("aggressive", "conservative"):
            continue
        atr = atr_by_time.get(t) if atr_by_time is not None else None  # None = no ATR = no order
        touch_name = "aggressive_touch" if kind == "aggressive" else "conservative_touch"
        trial = copy.deepcopy(ladder)  # same check as the bot: only commit when the trial plans an order
        if getattr(trial, touch_name)(ocl_id, price, atr) is None:
            continue
        order = getattr(ladder, touch_name)(ocl_id, price, atr)
        if order is None:
            continue
        ladder.record_fill(order, order.qty)  # full fill assumed
        orders.append(order)
    return orders


def _finish(kind, status, htf_low, entries, any_cons, events, target_r, equity, loss_ceiling,
            htf_key_time=None, paused=False, atr_by_time=None):
    """Cycle result. Average = quantity-weighted mean of the ladder orders (plan P4), not a price mean."""
    events = list(events or [])
    orders = ladder_plan(events, htf_low, equity, loss_ceiling, atr_by_time)
    if not orders:
        return CycleResult(kind=kind, status=status, htf_low=htf_low, htf_key_time=htf_key_time,
                           ltf_paused=paused, events=events)
    if status == "waiting":
        status = "open"
    total = sum(o.qty for o in orders)
    avg = sum(o.qty * o.price for o in orders) / total
    if any_cons:
        mode, target = "trail", None
    elif target_r is None:
        mode, target = "target_pending", None
    else:
        mode, target = "target", avg + target_r * (avg - htf_low)
    return CycleResult(kind=kind, status=status, htf_low=htf_low, entries=entries,
                       avg_entry=avg, mode=mode, target=target,
                       htf_key_time=htf_key_time, ltf_paused=paused, events=events)


def run_cycles(kind: str, mtf, htf, ltf, target_r: Optional[float] = None, *,
               equity: Optional[float], loss_ceiling: Optional[float],
               atr_by_time: Optional[dict] = None) -> List[CycleResult]:
    """All HTF cycles in the history, in order. Empty list when no MTF setup has been touched.
    equity and loss_ceiling are the same values the bot uses (owner settings). loss_ceiling None = no
    orders (fail closed)."""
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
        entries, any_cons, status, _, paused, events = _ltf_loop(ltf, key_time, htf_low, inval_time,
                                                                 mtf_inval_time)
        results.append(_finish(kind, status, htf_low, entries, any_cons, events, target_r,
                               equity, loss_ceiling, htf_key_time=key_time, paused=paused,
                               atr_by_time=atr_by_time))
        if status != "htf_invalidated":
            break  # open, waiting or MTF invalidated: this is the end of the replay
        floor = htf_low
        start = inval_time  # the next HTF OCL must form after the invalidation
    return results


def run_cycle(kind: str, mtf, htf, ltf, target_r: Optional[float] = None, *,
              equity: Optional[float], loss_ceiling: Optional[float],
              atr_by_time: Optional[dict] = None) -> Optional[CycleResult]:
    """The last cycle in the history (the current one), or None when no cycle exists."""
    results = run_cycles(kind, mtf, htf, ltf, target_r, equity=equity, loss_ceiling=loss_ceiling,
                         atr_by_time=atr_by_time)
    return results[-1] if results else None


def fresh_entries(result: Optional[CycleResult], newest_ltf_time) -> List[Entry]:
    """Only entries on the newest closed LTF bar may become orders (owner rule, 2026-11-19)."""
    if result is None:
        return []
    return [e for e in result.entries if e.time == newest_ltf_time]
