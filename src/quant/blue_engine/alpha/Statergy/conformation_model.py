"""
conformation_model.py - the DAILY conformation state machine, split out of
ocl_keylevel.py.

WHY THE SPLIT
=============
``ocl_keylevel.py`` owns the three AGGRESSIVE OCL blueprints: the HTF pattern,
the key levels, the LTF touch/crossover gates, the stops, the targets, the
trailing exits. That file is the strategy.

The conformation entry model is a different thing: a state machine that decides
WHEN the aggressive strategy is even allowed to look at the LTF. It has its own
frame (the MHTF ladder), its own sequence (mitigation -> stop hunt -> IFVG ->
CISD -> CISD mitigation -> daily OCL), and its own rejection rules. Keeping it
inside the strategy file made both harder to read and made it impossible to
test the state machine without constructing a live signal object.

So:

    ocl_keylevel.py       the aggressive strategy  (unchanged behaviour)
    conformation_model.py the state machine that gates it   <- this file

The two meet at exactly one place: ``_conformation_entry`` hands the armed level
and the structural stop to the OCL class's OWN ``_check_touch`` /
``_check_ltf_rrg`` / ``_hilega_milega_bullish`` / ``_fire_intent``. No pattern
logic is duplicated.

THREE FRAMES
============
    MHTF (monthly)  OCL pattern -> KEY-LEVEL LADDER + bias     <- ocl_keylevel
    HTF  (daily)    mitigation -> stop hunt -> IFVG -> CISD ->
                    CISD mitigation -> daily OCL pattern        <- THIS FILE
    LTF  (15 min)   daily key-level mitigation -> OCL trigger   <- ocl_keylevel

RUNG SELECTION (user-selectable)
================================
``rung_mode`` picks which level of a frame's ladder the chain works from:

    "aggressive"    the nearest level (the ladder's first rung)  [default]
    "conservative"  the deepest level (the ladder's last rung)
    "ladder"        walk aggressive -> conservative -> bottom

A blueprint that reports only ONE level (SELL_FAILURE_BOTTOM) behaves the same
under all three modes - there is nothing to walk.

CISD - the operator definition
==============================
    "first red candle open of consecutive down candles with IFVG, and then
     price moves and closes above the red candle open - this level is CISD."

So the level is the OPEN of c1, the OLDEST candle of the run, and the bearish
FVG must sit inside that run.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from blue_engine.alpha.Statergy import ocl_keylevel as ocl
from blue_engine.core.time_utils import ist_now

__all__ = [
    "ConfState", "ConformationState", "ConformationModel",
    "RUNG_MODES", "conf_ohlc", "conf_rows", "fractal_lows", "avg_body",
    "is_displacement", "bearish_fvgs", "first_bearish_fvg", "cisd_level",
    "validate_stop", "pick_levels",
]


#: Legal ``rung_mode`` values.
RUNG_MODES: Tuple[str, ...] = ("aggressive", "conservative", "ladder")


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


class ConfState:
    """The chain's position in the sequence. One value at a time."""

    IDLE = "IDLE"                    # no MHTF ladder yet
    AWAIT_MITIGATION = "AWAIT_MITIGATION"
    AWAIT_HUNT = "AWAIT_HUNT"
    AWAIT_IFVG = "AWAIT_IFVG"
    AWAIT_CISD = "AWAIT_CISD"
    AWAIT_CISD_TOUCH = "AWAIT_CISD_TOUCH"
    AWAIT_HTF_OCL = "AWAIT_HTF_OCL"
    AWAIT_LTF = "AWAIT_LTF"


class ConformationState:
    """Mutable bookkeeping for one chain (per signal instance)."""

    __slots__ = (
        "state", "ladder", "rung", "bias", "zone", "mitigated_ts",
        "swing_low", "sweep_ts", "sweep_low", "pending_sweep", "sweep_index",
        "ifvg", "displacement_ts", "cisd_level", "cisd_ts",
        "cisd_touched_ts", "htf_ocl_ts", "ltf_zone", "used_keys",
        "transitions", "mhtf_mode", "mhtf_ts",
        "htf_ladder", "htf_rung", "htf_zone", "mhtf_dead",
        "mhtf_armed_index", "mhtf_inside_logged",
        "mhtf_pattern_index", "mhtf_pattern_ts", "mhtf_stale_logged",
    )

    def __init__(self) -> None:
        self.state = ConfState.IDLE
        self.ladder: list = []
        self.rung = 0
        self.bias = "BUY"
        self.zone = None
        self.mitigated_ts: Optional[str] = None
        self.swing_low: Optional[float] = None
        self.sweep_ts: Optional[str] = None
        self.sweep_low: Optional[float] = None
        self.sweep_index: Optional[int] = None
        self.pending_sweep = None
        self.ifvg = None
        self.displacement_ts: Optional[str] = None
        self.cisd_level: Optional[float] = None
        self.cisd_ts: Optional[str] = None
        self.cisd_touched_ts: Optional[str] = None
        self.htf_ocl_ts: Optional[str] = None
        self.ltf_zone = None
        self.used_keys: set = set()
        self.transitions: list = []
        self.mhtf_mode: Optional[str] = None
        self.mhtf_ts: Optional[str] = None
        #: The DAILY ladder reported by the daily OCL pattern.
        self.htf_ladder: list = []
        self.htf_rung = 0
        self.htf_zone: Optional[float] = None
        self.mhtf_dead = False
        self.mhtf_armed_index: Optional[int] = None
        self.mhtf_pattern_index: Optional[int] = None
        self.mhtf_pattern_ts: Optional[str] = None
        self.mhtf_inside_logged: set = set()
        self.mhtf_stale_logged: set = set()

    def reset(self) -> None:
        self.__init__()


class _ConfHistoryClient:
    """Answers ``client.history`` from a fixed DataFrame - no network."""

    def __init__(self, frame: Any) -> None:
        self._frame = frame

    def history(self, **_kwargs: Any) -> Dict[str, Any]:
        return {"status": "success", "data": self._frame}


# --------------------------------------------------------------------------- #
# bar helpers (pure; no signal object needed - all testable alone)
# --------------------------------------------------------------------------- #


def conf_rows(frame: Any) -> list:
    """Bars / DataFrame / list-of-dicts -> rows with ``timestamp`` + OHLC."""
    rows: list = []
    if frame is None:
        return rows
    if hasattr(frame, "reset_index") and hasattr(frame, "to_dict"):
        try:
            frame = frame.reset_index()
        except Exception:                                    # noqa: BLE001
            pass
        for record in frame.to_dict("records"):
            rows.append(dict(record))
    else:
        for item in frame:
            if isinstance(item, dict):
                rows.append(dict(item))
            else:
                rows.append({"timestamp": getattr(item, "ts", ""),
                             "open": getattr(item, "open", 0.0),
                             "high": getattr(item, "high", 0.0),
                             "low": getattr(item, "low", 0.0),
                             "close": getattr(item, "close", 0.0),
                             "volume": getattr(item, "volume", 0.0)})
    for row in rows:
        if "timestamp" not in row:
            for key in ("ts", "date", "datetime", "index"):
                if key in row:
                    row["timestamp"] = row[key]
                    break
    return rows


def conf_ohlc(row: Any) -> Dict[str, float]:
    """OHLC dict from a row or a Bar."""
    if isinstance(row, dict):
        return {"open": float(row.get("open", 0.0)),
                "high": float(row.get("high", 0.0)),
                "low": float(row.get("low", 0.0)),
                "close": float(row.get("close", 0.0))}
    return {"open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": float(row.close)}


def _is_red(c: Dict[str, float]) -> bool:
    return c["close"] < c["open"]


def fractal_lows(rows: list, lookback: int) -> list:
    """``(confirm_index, low)`` for every strict fractal low in ``rows``.

    ``confirm_index = swing_bar + lookback``: the swing is only knowable that
    many bars later, and every caller must respect it.
    """
    out: list = []
    for i in range(lookback, len(rows) - lookback):
        low = conf_ohlc(rows[i])["low"]
        left = all(conf_ohlc(rows[j])["low"] > low
                   for j in range(i - lookback, i))
        right = all(conf_ohlc(rows[j])["low"] > low
                    for j in range(i + 1, i + lookback + 1))
        if left and right:
            out.append((i + lookback, low))
    return out


def avg_body(rows: list, end: int, n: int) -> Optional[float]:
    """Mean absolute body of the ``n`` bars ending at ``end`` (inclusive)."""
    start = max(0, end - n + 1)
    window = rows[start:end + 1]
    if n <= 0 or len(window) < n:
        return None
    return sum(abs(conf_ohlc(b)["close"] - conf_ohlc(b)["open"])
               for b in window) / len(window)


def is_displacement(row: Any, reference: Optional[float], *,
                    mult: float, close_frac: float) -> bool:
    """Bullish displacement: body > mult x avg body, closing near the high."""
    if reference is None or reference <= 0:
        return False
    c = conf_ohlc(row)
    body = c["close"] - c["open"]
    if body <= 0 or body <= mult * reference:
        return False
    span = c["high"] - c["low"]
    if span <= 0:
        return False
    return (c["high"] - c["close"]) <= close_frac * span


def bearish_fvgs(rows: list, start: int, end: int,
                 min_size: float) -> List[Dict[str, Any]]:
    """All bearish gaps completing in ``[start, end]`` (oldest first).

    Definition: ``rows[i-2].low > rows[i].high`` -> band
    ``[rows[i].high, rows[i-2].low]``.
    """
    out: List[Dict[str, Any]] = []
    for i in range(max(2, start), min(end, len(rows) - 1) + 1):
        first, third = conf_ohlc(rows[i - 2]), conf_ohlc(rows[i])
        if first["low"] > third["high"]:
            low, high = third["high"], first["low"]
            if high - low >= min_size and high > low:
                out.append({"index": i, "low": low, "high": high})
    return out


def first_bearish_fvg(rows: list, start: int, end: int,
                      min_size: float) -> Optional[Dict[str, Any]]:
    """The OLDEST bearish gap in the window - the one the displacement inverts."""
    gaps = bearish_fvgs(rows, start, end, min_size)
    return gaps[0] if gaps else None


def cisd_level(rows: list, displacement_index: int, *,
               max_lookback: int,
               fvg: Optional[Dict[str, Any]] = None
               ) -> Optional[Tuple[int, float]]:
    """CISD = the OPEN of c1, the OLDEST red candle of the down-run.

    The run is the consecutive red candles immediately before the displacement,
    and it must contain the bearish FVG (its 3-candle window sets the floor),
    which is what "consecutive down candles WITH IFVG" means.

    Returns ``(run_start_index, level)`` or ``None`` when the candle
    immediately before the displacement is not red.
    """
    j = displacement_index - 1
    if j < 0 or not _is_red(conf_ohlc(rows[j])):
        return None
    floor = 0 if fvg is None else max(0, int(fvg["index"]) - 2)
    top = j
    steps = 0
    while (j - 1 >= 0 and j - 1 >= floor
           and _is_red(conf_ohlc(rows[j - 1]))
           and steps < max_lookback):
        j -= 1
        steps += 1
        top = j
    return top, conf_ohlc(rows[top])["open"]


def validate_stop(entry: float, swing_low: float,
                  buffer_abs: float) -> Tuple[Optional[float], Optional[str]]:
    """SL = swing low - absolute buffer, with the two hard rules."""
    if swing_low <= 0:
        return None, "swing low must be positive"
    if buffer_abs < 0:
        return None, "sl_buffer must be >= 0"
    sl = swing_low - buffer_abs
    if not sl < swing_low:
        return None, f"SL {sl:.4f} is not below the swing low {swing_low:.4f}"
    if not sl < entry:
        return None, f"SL {sl:.4f} is not below entry {entry:.4f}"
    return sl, None


def pick_levels(aggressive: Optional[float], conservative: Optional[float],
                low_swing: Optional[float] = None) -> list:
    """The level ladder a pattern reports, top -> bottom, de-duplicated."""
    levels: list = []
    for value in (aggressive, conservative, low_swing):
        if value is None:
            continue
        price = float(value)
        if price <= 0 or any(abs(price - seen) < 1e-9 for seen in levels):
            continue
        levels.append(price)
    return levels


# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #


class ConformationModel:
    """The daily conformation chain for ONE ``OCLKeylevelSignal`` instance.

    The model is deliberately given the signal, not a copy of its rules: the
    pattern scan is the class's own ``_scan_htf_pattern`` and the LTF gates are
    the class's own methods. What lives here is only the SEQUENCE - which frame
    to look at, in what order, and what kills a setup.

    Args:
        signal: the owning ``OCLKeylevelSignal``.
        rung_mode: ``"aggressive"``, ``"conservative"`` or ``"ladder"``.
    """

    def __init__(self, signal: Any, *, rung_mode: str = "aggressive") -> None:
        if rung_mode not in RUNG_MODES:
            raise ValueError(f"rung_mode must be one of {RUNG_MODES}; "
                             f"got {rung_mode!r}")
        self.sig = signal
        self.rung_mode = rung_mode
        self.conf = ConformationState()

    # -- config shortcuts --------------------------------------------------- #

    def _cfg(self, key: str, default: Any) -> Any:
        return self.sig.config.get(key, default)

    @property
    def mhtf_interval(self) -> str:
        return getattr(self.sig, "mhtf_interval", "1month")

    @property
    def htf_interval(self) -> str:
        return getattr(self.sig, "htf_interval", "day")

    # -- rung selection ------------------------------------------------------ #

    def _rung_value(self, ladder: List[float], rung: int) -> Optional[float]:
        """Which ladder level the chain works from, given ``rung_mode``.

        ``aggressive`` takes the nearest level and never walks;
        ``conservative`` takes the deepest and never walks;
        ``ladder`` walks top -> bottom.
        """
        if not ladder:
            return None
        if self.rung_mode == "aggressive":
            return ladder[0]
        if self.rung_mode == "conservative":
            return ladder[-1]
        return ladder[rung] if rung < len(ladder) else None

    # -- logging ------------------------------------------------------------ #

    def log(self, state: str, ts: str, reason: str, **detail: Any) -> None:
        """Every transition is logged with its timestamp and reason."""
        self.conf.transitions.append({"ts": ts, "state": state,
                                      "reason": reason,
                                      "detail": dict(detail)})
        extra = " ".join(f"{k}={v}" for k, v in detail.items())
        self.sig.log(f"[CONF {ts}] -> {state}: {reason}"
                     + (f" | {extra}" if extra else ""))

    # -- the MHTF ladder ---------------------------------------------------- #

    def scan(self, interval: str, rows: list, *, use_hm: Optional[bool] = None,
             tail_only: bool = True) -> Optional[Dict[str, Any]]:
        """Run the OCL pattern scanner on ``rows`` (closed bars only).

        ``_scan_htf_pattern`` reads candles through ``client.history`` and the
        wall clock. Both are swapped for the duration of the call and restored
        in ``finally``, so a failed scan cannot leave the live object pointing
        at a fake client.

        ``tail_only=True`` scans the last four bars (the class's own
        behaviour, right for the daily gate). ``False`` searches the whole
        series backwards and returns the most recent pattern anywhere - what a
        MONTHLY key level needs, because demanding red-red-green in the last
        four months left the chain unreachable (measured: INFY has ~29 monthly
        OCLs in 30 years, roughly one a year).
        """
        if not rows or len(rows) < 4:
            return None
        import pandas as pd

        sig = self.sig
        saved = (sig.client, sig.htf_interval, sig.setup_mode, sig.hm_enabled)
        try:
            if tail_only:
                windows = [(len(rows) - 1, rows[-4:])]
            else:
                windows = [(end, rows[max(0, end - 3):end + 1])
                           for end in range(len(rows) - 1, 3 - 1, -1)]
            for end, window in windows:
                sig.client = _ConfHistoryClient(pd.DataFrame(window))
                sig.htf_interval = interval
                if use_hm is not None:
                    sig.hm_enabled = bool(use_hm)
                if not bool(sig._scan_htf_pattern()):       # noqa: SLF001
                    continue
                return {
                    "direction": str(getattr(sig, "action", "BUY")).upper(),
                    "levels": pick_levels(
                        getattr(sig, "_aggressive_level", None),
                        getattr(sig, "_conservative_level", None),
                        getattr(sig, "_low_swing_level", None)),
                    "setup_mode": str(getattr(sig, "setup_mode", saved[2])),
                    "pattern_index": end,
                }
            return None
        except Exception as exc:                              # noqa: BLE001
            sig.log(f"[CONF SCAN] {interval} scan error: {exc}")
            return None
        finally:
            (sig.client, sig.htf_interval, sig.setup_mode,
             sig.hm_enabled) = saved

    def refresh_ladder(self, mhtf_rows: list, ts: str) -> bool:
        """Re-scan the MHTF frame and (re)build the ladder + bias."""
        conf = self.conf
        reading = self.scan(self.mhtf_interval, mhtf_rows, use_hm=False,
                            tail_only=False)
        if reading is None:
            self.sig.log(f"[CONF {ts}] MHTF: no OCL pattern anywhere in "
                         f"{len(mhtf_rows)} {self.mhtf_interval} bars")
            return False
        if not reading["levels"]:
            self.sig.log(f"[CONF {ts}] MHTF pattern had no usable levels")
            return False

        # Age cap: a monthly key level the market has left behind is not a
        # level. INFY's newest monthly OCL was 8 bars old with rungs at
        # 1476.70 / 1446.00 while price traded near 1000.
        pattern_index = int(reading.get("pattern_index") or 0)
        age = max(0, len(mhtf_rows) - 1 - pattern_index)
        cap = int(getattr(self.sig, "mhtf_max_pattern_age", 30))
        if cap and age > cap:
            marker = (reading.get("setup_mode"), pattern_index)
            if marker not in conf.mhtf_stale_logged:
                conf.mhtf_stale_logged.add(marker)
                self.log("MHTF_NO_RECENT_PATTERN", ts,
                         f"newest {self.mhtf_interval} OCL is {age} bars old "
                         f"(cap {cap}); ladder NOT armed",
                         pattern_ts=(str(mhtf_rows[pattern_index]
                                         .get("timestamp"))
                                     if 0 <= pattern_index < len(mhtf_rows)
                                     else None),
                         age=age, cap=cap)
            conf.state = ConfState.IDLE
            return False

        conf.ladder = list(reading["levels"])
        conf.rung = 0
        conf.bias = reading["direction"]
        conf.mhtf_mode = reading["setup_mode"]
        conf.state = ConfState.AWAIT_MITIGATION
        conf.mhtf_dead = False
        conf.mhtf_pattern_index = pattern_index
        conf.mhtf_pattern_ts = (str(mhtf_rows[pattern_index].get("timestamp"))
                                if 0 <= pattern_index < len(mhtf_rows) else ts)
        conf.mhtf_armed_index = max(0, min(pattern_index, len(mhtf_rows) - 1))
        conf.mhtf_inside_logged = set()
        self.sig.log(
            f"[CONF {ts}] MHTF {self.mhtf_interval} {reading['setup_mode']} "
            f"{reading['direction']} ladder={[round(v, 2) for v in conf.ladder]} "
            f"rung_mode={self.rung_mode} "
            f"pattern={conf.mhtf_pattern_ts} (age {age})")
        return True

    # -- the monthly terminal stop ------------------------------------------ #

    def mhtf_broken(self, mhtf_rows: list, ts: str) -> bool:
        """Terminal stop: MONTHLY close below the bottom rung AND below the swept low.

        Only a monthly CLOSE can end the chain. And per the operator, "a close
        inside an already-swept range does not kill the chain": the close must
        also take out ground the chain has not already traded below.

        "Already-swept" is the lowest of the stop-hunt low, the swept swing
        low, and the MHTF bars from ladder-arming up to - but excluding - the
        deciding bar. Including the deciding bar's own low would make every
        close look "inside" a range it just created, and the stop could never
        fire.
        """
        conf = self.conf
        if conf.mhtf_dead or not conf.ladder or not mhtf_rows:
            return conf.mhtf_dead
        bottom = min(conf.ladder)
        close = conf_ohlc(mhtf_rows[-1])["close"]
        if close >= bottom:
            return False

        swept = [v for v in (conf.sweep_low, conf.swing_low) if v is not None]
        if conf.mhtf_armed_index is not None:
            window = mhtf_rows[conf.mhtf_armed_index:-1]
            if window:
                swept.append(min(conf_ohlc(b)["low"] for b in window))
        swept_low = min(swept) if swept else None
        if swept_low is not None and close >= swept_low:
            marker = (round(bottom, 4), round(close, 4))
            if marker not in conf.mhtf_inside_logged:
                conf.mhtf_inside_logged.add(marker)
                self.log("MHTF_INSIDE_RANGE", ts,
                         "monthly closed below the MHTF bottom but INSIDE the "
                         "already-swept range; chain stays alive",
                         bottom=round(bottom, 2), close=round(close, 2),
                         swept_low=round(swept_low, 2))
            return False

        conf.mhtf_dead = True
        self.log("MHTF_DEAD", ts,
                 "monthly closed below the MHTF bottom AND below the swept "
                 "low; the chain stops until a new MHTF pattern forms",
                 bottom=round(bottom, 2), close=round(close, 2),
                 swept_low=None if swept_low is None else round(swept_low, 2))
        return True

    # -- the daily sequence -------------------------------------------------- #

    def _reset_to_cisd_gate(self, conf: ConformationState) -> None:
        conf.swing_low = None
        conf.pending_sweep = None
        conf.ifvg = None
        conf.cisd_level = None
        conf.cisd_touched_ts = None
        conf.htf_ocl_ts = None
        conf.ltf_zone = None
        conf.state = ConfState.AWAIT_CISD_TOUCH

    def step(self, htf_rows: list, ts: str) -> bool:
        """Advance the HTF states by one closed daily bar.

        Returns True when the chain is ready for the LTF frame.
        """
        conf = self.conf
        if not htf_rows or len(htf_rows) < 5:
            return False
        bar = conf_ohlc(htf_rows[-1])
        i = len(htf_rows) - 1

        # ---- 0/1: MHTF key-level mitigation -------------------------------- #
        if conf.state == ConfState.AWAIT_MITIGATION:
            level = self._rung_value(conf.ladder, conf.rung)
            if level is None:
                return False
            key = (round(level, 6), conf.rung)
            if key in conf.used_keys:
                return False
            if bar["low"] <= level:
                conf.zone = level
                conf.mitigated_ts = ts
                conf.used_keys.add(key)
                conf.state = ConfState.AWAIT_HUNT
                self.log(ConfState.AWAIT_HUNT, ts,
                         "daily wick entered the MHTF key level",
                         level=round(level, 2), rung=conf.rung,
                         rung_mode=self.rung_mode)
            return False

        # ---- rung invalidation: a CLOSE below the swing low ----------------- #
        if (conf.swing_low is not None
                and conf.state in (ConfState.AWAIT_CISD,
                                   ConfState.AWAIT_CISD_TOUCH,
                                   ConfState.AWAIT_LTF)
                and bar["close"] < conf.swing_low):
            if conf.state == ConfState.AWAIT_CISD:
                self.log("RUNG_DEAD", ts,
                         "daily closed below the swing low; waiting for the "
                         "next MHTF level",
                         swing_low=round(conf.swing_low, 2),
                         close=round(bar["close"], 2))
                conf.rung += 1
                conf.state = ConfState.AWAIT_MITIGATION
                conf.used_keys.discard(
                    (round(float(conf.ladder[conf.rung - 1]), 6),
                     conf.rung - 1))
            else:
                self.log("DAILY_RUNG_DEAD", ts,
                         "daily closed below the swing low after the daily OCL "
                         "gate; waiting for a new CISD",
                         swing_low=round(conf.swing_low, 2),
                         close=round(bar["close"], 2), htf_rung=conf.htf_rung)
                self._reset_to_cisd_gate(conf)
            conf.swing_low = None
            conf.pending_sweep = None
            conf.ifvg = None
            conf.cisd_level = None
            conf.cisd_touched_ts = None
            conf.htf_ocl_ts = None
            conf.ltf_zone = None
            return False

        # ---- 2: stop hunt --------------------------------------------------- #
        if conf.state == ConfState.AWAIT_HUNT:
            if self._state_hunt(conf, htf_rows, bar, ts, i):
                pass
            return False

        # ---- 3: IFVG -------------------------------------------------------- #
        if conf.state == ConfState.AWAIT_IFVG:
            self._state_ifvg(conf, htf_rows, bar, ts)
            return False

        # ---- 4: CISD confirmation ------------------------------------------- #
        if conf.state == ConfState.AWAIT_CISD:
            required = bool(self._cfg("cisd_close_required", True))
            confirmed = (bar["close"] > conf.cisd_level if required
                         else bar["high"] > conf.cisd_level)
            if confirmed:
                conf.state = ConfState.AWAIT_CISD_TOUCH
                self.log(ConfState.AWAIT_CISD_TOUCH, ts,
                         "closed above the CISD level",
                         cisd=round(conf.cisd_level, 2))
            return False

        # ---- 5: CISD mitigation --------------------------------------------- #
        if conf.state == ConfState.AWAIT_CISD_TOUCH:
            if bar["low"] <= conf.cisd_level:
                conf.cisd_touched_ts = ts
                conf.state = ConfState.AWAIT_HTF_OCL
                self.log(ConfState.AWAIT_HTF_OCL, ts,
                         "price returned to the CISD level; waiting for the "
                         "daily OCL pattern", cisd=round(conf.cisd_level, 2))
            return False

        # ---- 6: daily OCL pattern -> the DAILY ladder ------------------------ #
        if conf.state == ConfState.AWAIT_HTF_OCL:
            reading = self.scan(self.htf_interval, htf_rows, use_hm=False)
            if reading is None or reading["direction"] != "BUY":
                return False
            if not reading["levels"]:
                return False
            conf.htf_ladder = list(reading["levels"])
            conf.htf_rung = 0
            conf.htf_ocl_ts = ts
            conf.htf_zone = self._rung_value(conf.htf_ladder, 0)
            conf.ltf_zone = conf.htf_zone
            conf.state = ConfState.AWAIT_LTF
            self.log(ConfState.AWAIT_LTF, ts,
                     f"daily {reading['setup_mode']} formed; daily ladder armed",
                     ladder=[round(v, 2) for v in conf.htf_ladder],
                     rung_mode=self.rung_mode, cisd=round(conf.cisd_level, 2))
            return True

        # ---- the daily ladder ------------------------------------------------ #
        if conf.state == ConfState.AWAIT_LTF and self.rung_mode != "ladder":
            return True           # aggressive / conservative never walks
        if conf.state == ConfState.AWAIT_LTF and conf.htf_zone is not None:
            if bar["close"] < conf.htf_zone:
                conf.htf_rung += 1
                nxt = self._rung_value(conf.htf_ladder, conf.htf_rung)
                self.log("HTF_RUNG_DEAD", ts,
                         "daily closed below the daily rung level; "
                         + (f"stepping down to {nxt:.2f}" if nxt is not None
                            else "daily ladder exhausted - waiting for a new "
                                 "daily OCL"),
                         broken=round(conf.htf_zone, 2),
                         close=round(bar["close"], 2), rung=conf.htf_rung)
                conf.htf_zone = nxt
                conf.ltf_zone = nxt
                if nxt is None:
                    self._reset_to_cisd_gate(conf)
                return False
        return conf.state == ConfState.AWAIT_LTF

    def _state_hunt(self, conf: ConformationState, htf_rows: list,
                    bar: Dict[str, float], ts: str, i: int) -> bool:
        lookback = int(self._cfg("swing_lookback", 3))
        current = [p for c, p in fractal_lows(htf_rows, lookback) if c <= i]

        if conf.pending_sweep is not None:
            pending = conf.pending_sweep
            # Re-read the fractal every bar: the newest CONFIRMED swing low is
            # the one being swept. Latching onto the first low ever seen made
            # every later sweep measure against a stale level.
            target = current[-1] if current else float(pending["swing_low"])
            if bar["low"] < target < bar["close"]:
                self._confirm_hunt(conf, target, bar["low"], ts, i)
            elif bar["low"] < target:
                conf.pending_sweep = {"swing_low": target, "low": bar["low"],
                                      "index": i}
            else:
                self._confirm_hunt(conf, target, float(pending["low"]), ts,
                                   int(pending.get("index", i)))
            return False

        if not current:
            return False
        swing_low = current[-1]
        if bar["low"] < swing_low < bar["close"]:
            self._confirm_hunt(conf, swing_low, bar["low"], ts, i)
        elif bar["low"] < swing_low:
            # Wick below, close at/below: the confirmation may arrive on a
            # later bar, so this is the normal shape of a sweep.
            conf.pending_sweep = {"swing_low": swing_low, "low": bar["low"],
                                  "index": i}
        return False

    def _confirm_hunt(self, conf: ConformationState, swing_low: float,
                      sweep_low: float, ts: str, index: int) -> None:
        conf.swing_low = swing_low
        conf.sweep_low = sweep_low
        conf.sweep_ts = ts
        conf.sweep_index = index
        conf.pending_sweep = None
        conf.state = ConfState.AWAIT_IFVG
        self.log(ConfState.AWAIT_IFVG, ts,
                 "swept the daily swing low and closed back above it",
                 swing_low=round(swing_low, 2),
                 sweep_low=round(sweep_low, 2))

    def _state_ifvg(self, conf: ConformationState, htf_rows: list,
                    bar: Dict[str, float], ts: str) -> None:
        mult = float(self._cfg("displacement_mult", 1.5))
        avg_bars = int(self._cfg("displacement_avg_bars", 20))
        close_frac = float(self._cfg("displacement_close_frac", 0.33))
        ref = avg_body(htf_rows, len(htf_rows) - 2, avg_bars)
        if not is_displacement(htf_rows[-1], ref, mult=mult,
                               close_frac=close_frac):
            return
        sweep = conf.sweep_index
        if sweep is None:
            return
        # Only gaps that formed AFTER the sweep count. The displacement bar can
        # complete one (its low is the gap's lower edge), so the window ends at
        # the current bar.
        fvg = first_bearish_fvg(htf_rows, sweep + 1, len(htf_rows) - 1,
                                float(self._cfg("fvg_min_size", 0.0)))
        if fvg is None:
            return
        confirm = str(self._cfg("ifvg_confirm", "body_close")).lower()
        inverted = (bar["close"] > fvg["high"] if confirm == "body_close"
                    else bar["high"] > fvg["high"])
        if not inverted:
            return

        conf.ifvg = fvg
        conf.displacement_ts = ts
        found = cisd_level(htf_rows, len(htf_rows) - 1,
                           max_lookback=int(self._cfg("cisd_max_lookback", 10)),
                           fvg=fvg)
        if found is None:
            self.log("REJECTED", ts,
                     "no red candle before the displacement: CISD undefined, "
                     "setup skipped")
            conf.state = ConfState.AWAIT_HUNT
            conf.ifvg = None
            return
        conf.cisd_level = found[1]
        conf.state = ConfState.AWAIT_CISD
        self.log(ConfState.AWAIT_CISD, ts,
                 "bullish displacement inverted the bearish FVG; CISD level = "
                 "open of c1 of the red run",
                 ifvg=[round(fvg["low"], 2), round(fvg["high"], 2)],
                 cisd=round(conf.cisd_level, 2))

    # -- the LTF hand-off ---------------------------------------------------- #

    def entry(self, ltf_df: Any, ts: str) -> Optional[Dict[str, Any]]:
        """STATE 7: LTF mitigates the daily level, then the OCL BUY fires."""
        conf = self.conf
        if conf.state != ConfState.AWAIT_LTF or conf.ltf_zone is None:
            return None
        sig = self.sig
        try:
            bar = ocl.extract_ohlc(ltf_df.iloc[-1])
        except Exception:                                     # noqa: BLE001
            return None
        if bar["low"] > conf.ltf_zone:
            return None
        self.log("LTF_MITIGATED", ts, "LTF mitigated the daily key level",
                 level=round(conf.ltf_zone, 2))

        # From here the STRATEGY owns the trade: its own touch check, RRG
        # filter, HM gate, target, breakeven and trailing exit.
        sig._trigger_level = float(conf.ltf_zone)             # noqa: SLF001
        sig._htf_stop_loss = float(conf.swing_low)            # noqa: SLF001
        sig._sell_failure_armed = bar["close"] > conf.ltf_zone  # noqa: SLF001
        sig._sell_failure_broken = sig._sell_failure_armed    # noqa: SLF001
        if not sig._check_touch(bar["close"], bar):           # noqa: SLF001
            return None
        if sig.ltf_pattern_enabled and not sig._check_ltf_rrg(ltf_df):  # noqa: SLF001
            return None
        if sig.hm_enabled and not sig._hilega_milega_bullish(  # noqa: SLF001
                ltf_df, label="LTF"):
            return None
        intent = sig._fire_intent(bar["close"])               # noqa: SLF001
        if intent is None:
            return None

        buffer_abs = float(self._cfg("sl_buffer", 0.0))
        price = float(intent.get("price") or bar["close"])
        sl, reject = validate_stop(price, float(conf.swing_low), buffer_abs)
        if sl is None:
            sig.log(f"[CONF {ts}] entry refused: {reject}")
            self._reset("stop validation failed: " + reject, ts)
            return None
        intent["stop_loss"] = sl
        intent["htf_stop_loss"] = sl
        intent["target"] = price + sig.target_rr * (price - sl)
        intent["setup_mode"] = sig.setup_mode
        intent["entry_model"] = "conformation"
        intent["rung_mode"] = self.rung_mode
        intent["conformation"] = {
            "mhtf_level": self._rung_value(conf.ladder, conf.rung),
            "swing_low": conf.swing_low,
            "sweep_low": conf.sweep_low,
            "ifvg": (None if conf.ifvg is None
                     else [conf.ifvg["low"], conf.ifvg["high"]]),
            "cisd_level": conf.cisd_level,
            "cisd_ts": conf.cisd_ts,
            "cisd_touched_ts": conf.cisd_touched_ts,
            "htf_ocl_ts": conf.htf_ocl_ts,
            "htf_ladder": list(conf.htf_ladder),
            "ltf_level": conf.ltf_zone,
            "transitions": list(conf.transitions),
        }
        sig.log(f"[CONF {ts}] ENTRY BUY {price} SL={sl:.2f} "
                f"TP={intent['target']:.2f} cisd={conf.cisd_level:.2f} "
                f"swing_low={conf.swing_low:.2f}")
        self._reset("entry fired", ts)
        return intent

    def _reset(self, reason: str, ts: str = "") -> None:
        """Drop the chain but keep the used-rung memory."""
        used = set(self.conf.used_keys)
        stale = set(self.conf.mhtf_stale_logged)
        self.conf.reset()
        self.conf.used_keys = used
        self.conf.mhtf_stale_logged = stale
        self.sig._pattern_detected = False                    # noqa: SLF001
        self.sig._trigger_level = None                        # noqa: SLF001
        self.sig.log(f"[CONF RESET {ts}] {reason}")

    # -- live loop ----------------------------------------------------------- #

    def detect(self) -> Optional[Dict[str, Any]]:
        """The conformation model's replacement for ``detect()``."""
        sig = self.sig
        try:
            if sig._position_open:                            # noqa: SLF001
                return sig._monitor_open_position()           # noqa: SLF001
        except Exception as exc:                              # noqa: BLE001
            sig.log(f"[CONF] position monitor error: {exc}")
        if sig._exhausted:                                    # noqa: SLF001
            return None

        frames = self.fetch_frames()
        if frames is None:
            time.sleep(sig.poll_seconds)
            return None
        mhtf_df, htf_df, ltf_df = frames
        mhtf_rows = conf_rows(mhtf_df)
        htf_rows = conf_rows(htf_df)
        ts = str(htf_rows[-1].get("timestamp")) if htf_rows else ""
        ltf_ts = (str(conf_rows(ltf_df)[-1].get("timestamp"))
                  if ltf_df is not None else ts)

        conf = self.conf
        mhtf_ts = str(mhtf_rows[-1].get("timestamp")) if mhtf_rows else ""
        if conf.mhtf_mode is None or mhtf_ts != conf.mhtf_ts:
            if self.refresh_ladder(mhtf_rows, mhtf_ts):
                conf.mhtf_ts = mhtf_ts
        if conf.state == ConfState.IDLE:
            sig.log("[CONF] no MHTF ladder armed; waiting for a monthly pattern")
            time.sleep(sig.poll_seconds)
            return None
        if self.mhtf_broken(mhtf_rows, mhtf_ts):
            time.sleep(sig.poll_seconds)
            return None
        if not self.step(htf_rows, ts):
            time.sleep(sig.poll_seconds)
            return None
        intent = self.entry(ltf_df, ltf_ts)
        if intent is None:
            time.sleep(sig.poll_seconds)
        return intent

    def fetch_frames(self) -> Optional[tuple]:
        """Three ``client.history`` calls: MHTF, HTF, LTF."""
        sig = self.sig
        try:
            now = ist_now()
            out = []
            for interval, days in ((self.mhtf_interval, 3650),
                                   (self.htf_interval, 400),
                                   (sig.ltf_interval, 5)):
                response = sig.client.history(
                    symbol=sig.symbol, exchange=sig.exchange, interval=interval,
                    start_date=(now - __import__("datetime").timedelta(
                        days=days)).strftime("%Y-%m-%d"),
                    end_date=now.strftime("%Y-%m-%d"))
                if response.get("status") != "success":
                    sig.log(f"[CONF] {interval} history failed: "
                            f"{response.get('message')}")
                    return None
                frame = response.get("data")
                if frame is None or len(frame) < 1:
                    sig.log(f"[CONF] {interval} history empty")
                    return None
                out.append(frame)
            return tuple(out)
        except Exception as exc:                              # noqa: BLE001
            sig.log(f"[CONF] history error: {exc}")
            return None

    # -- offline walk-forward ------------------------------------------------ #

    def evaluate_bars(self, ltf_bars: Any, htf_bars: Any,
                      mhtf_bars: Any) -> Dict[str, Any]:
        """Offline evaluation over historical frames - same chain, no polling.

        Walks the LTF bars once, feeding each one the HTF/MHTF history that had
        CLOSED by that timestamp. Returns the entries plus every transition.

        Exits are not simulated here: the strategy's own stop/target/trail live
        in the live position manager, and a second exit model would be the
        duplication this design avoids.
        """
        mhtf_rows = conf_rows(mhtf_bars)
        htf_rows = conf_rows(htf_bars)
        ltf_rows = conf_rows(ltf_bars)
        if not ltf_rows or not htf_rows:
            return {"signals": [], "transitions": [], "rejections": []}

        def _upto(rows: list, ts: str) -> list:
            lo, hi, found = 0, len(rows) - 1, -1
            while lo <= hi:
                mid = (lo + hi) // 2
                if str(rows[mid].get("timestamp", "")) <= ts:
                    found = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            return rows[:found + 1]

        import pandas as pd

        self.conf = ConformationState()
        signals: list = []
        rejections: list = []
        seen_mhtf = None
        for row in ltf_rows:
            ts = str(row.get("timestamp", ""))
            m_win = _upto(mhtf_rows, ts)
            h_win = _upto(htf_rows, ts)
            if not h_win or len(h_win) < 5:
                continue
            m_ts = str(m_win[-1].get("timestamp")) if m_win else ""
            if m_ts and m_ts != seen_mhtf:
                seen_mhtf = m_ts
                if self.conf.state in (ConfState.IDLE,
                                       ConfState.AWAIT_MITIGATION):
                    self.refresh_ladder(m_win, m_ts)
            if self.conf.state == ConfState.IDLE:
                continue
            if self.mhtf_broken(m_win, m_ts):
                continue
            try:
                if not self.step(h_win, str(h_win[-1].get("timestamp"))):
                    continue
            except Exception as exc:                          # noqa: BLE001
                rejections.append({"ts": ts, "reason": f"step error: {exc}"})
                continue
            window = pd.DataFrame(_upto(ltf_rows, ts)[-120:])
            intent = self.entry(window, ts)
            if intent is not None:
                intent["ts"] = ts
                signals.append(intent)
        return {"signals": signals, "transitions": self.conf.transitions,
                "rejections": rejections}
