"""
backtest_ocl_confirm.py - walk-forward backtest harness for the OCL keylevel
confirmation gate (build-plan step 7).

==============================================================================
WHAT THIS DOES, AND WHAT IT DELIBERATELY IS NOT
==============================================================================
This harness answers ONE question:

    does requiring a z-scored ROC momentum flip AFTER a keylevel touch
    improve the trade population, compared with firing on the touch alone?

It is an A/B comparison, not a profit simulator. It reports, for the same
bars and the same levels, the two candidate rule sets side by side:

    RULE BUY-THE-TOUCH : enter on the bar that touches the level
    RULE WAIT-FOR-FLIP : enter only when z-ROC confirms afterwards

The honest framing: this measures whether the filter *changes the quality of
the selected trades* under stated cost assumptions. It does not prove
profitability, and it says nothing about live slippage, partial fills,
broker rejects or data gaps.

==============================================================================
ASSUMPTIONS - read these before believing any number this prints
==============================================================================
1. FILL MODEL: a signal evaluated on bar t's close fills at bar t+1's OPEN.
   This is the no-look-ahead convention. The confirmation layer reads bar t's
   close, so bar t+1's open is the earliest honest fill.
2. COSTS: flat round-trip cost in basis points, applied once per trade.
   Default 8 bps. Set to 0 to see the gross effect. Real Indian costs
   (brokerage + STT + exchange + GST + stamp) vary by product and size -
   pass your own number; the default is a placeholder, not a measurement.
3. EXIT: fixed horizon - the trade closes N bars after entry at that bar's
   open. This is a deliberate simplification so the harness can isolate ENTRY
   quality. It is NOT a strategy; no stop-loss, no target, no trailing rule.
4. ONE POSITION AT A TIME: no pyramiding, no overlapping trades per symbol.
5. NO SURVIVORSHIP HANDLING: whatever symbols you feed in are what you get.
6. NO LOOK-AHEAD IN THE Z-SCORE: zscored_roc() is already trailing-only, and
   this harness additionally only permits a flip on the newest bar.

==============================================================================
USAGE
==============================================================================
    python Temp/backtest_ocl_confirm.py --csv data.csv --levels 22000,22150

    CSV needs columns: open, high, low, close, volume
    (a `datetime` column is used if present, otherwise bar index is used)

WALK-FORWARD: pass --folds N to split the series into N consecutive
non-overlapping segments and report each separately. If the edge only shows
up in one fold, it is noise, not an edge.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Make blue_engine importable when this script is run directly from Temp/.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_ENGINE = os.path.join(_ROOT, "src", "quant", "blue_engine")
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)

from blue_engine.alpha.confirm.roc import find_flip, zscored_roc  # noqa: E402
from blue_engine.core.band import BuyerInitiatedDailyVWAP          # noqa: E402


# ===========================================================================
# Configuration
# ===========================================================================
@dataclass
class BacktestConfig:
    """All knobs in one place so a walk-forward sweep can mutate them safely."""

    roc_period: int = 10
    z_window: int = 100
    entry_z: float = 1.0
    touch_tolerance: float = 0.0
    flat_cost_bps: float = 8.0
    hold_bars: int = 20
    action: str = "BUY"
    #: Bars per trading day in the series under test. Used to convert a
    #: level's validity from days into bars. 375 = a 1-minute session.
    bars_per_day: int = 375
    #: Days a keylevel stays actionable after its pattern forms.
    level_valid_days: int = 10
    #: First touch only (operator rule): once a level arms a setup, later
    #: touches of the SAME level cannot start a new setup.
    first_touch_only: bool = True


# ===========================================================================
# Bracket detection - the shared "where is a level" logic
# ===========================================================================
def touch_indices(
    closes: Sequence[float],
    lows: Sequence[float],
    highs: Sequence[float],
    level: float,
    *,
    action: str = "BUY",
    tolerance: float = 0.0,
    active_from: int = 0,
    active_until: Optional[int] = None,
) -> List[int]:
    """Bar indices where price TOUCHES ``level`` on the way into it.

    A touch is a crossing event, not a containment test. A level that sits
    below the whole series is "<=level" on almost every bar, so a naive
    containment test reports hundreds of "touches" and makes both rules
    select identical trades.

    BUY  (demand level approached from above):
         the bar's low reaches the level AND the bar opened above it.
    SELL (supply level approached from below):
         the bar's high reaches the level AND the bar opened below it.

    The ``active_from``/``active_until`` bounds restrict touches to the
    period the level is actually live. Without them a level formed months
    earlier would keep arming setups, which is how the first real-data run
    produced 12 trades from 19 levels with 11 setups never touched at all.
    """
    upper = len(closes) if active_until is None else min(active_until,
                                                         len(closes))
    hits: List[int] = []
    for i in range(max(0, int(active_from)), upper):
        if action == "BUY":
            arrived_from_above = (closes[i - 1] > level if i > 0
                                  else lows[i] <= level <= highs[i])
            if lows[i] <= (level + tolerance) and arrived_from_above:
                hits.append(i)
        else:
            arrived_from_below = closes[i - 1] < level if i > 0 else True
            if highs[i] >= (level - tolerance) and arrived_from_below:
                hits.append(i)
    return hits


def opens_above(
    highs: Sequence[float], lows: Sequence[float],
    closes: Sequence[float], i: int, level: float,
) -> bool:
    """Bar 0 fallback: treat the level as arrived-at if the bar spans it."""
    return lows[i] <= level <= highs[i]


# ===========================================================================
# The two rules being compared
# ===========================================================================
@dataclass
class LevelSpec:
    """A keylevel together with the window during which it is actionable.

    WHY THIS EXISTS
    ---------------
    A keylevel is not a permanent property of a stock. It is created by a
    specific OCL pattern on a specific date, and it decays: the supply/demand
    imbalance that formed the level is repaired over the following days, and
    after that the level carries no information.

    Treating levels as timeless produced a measurably broken experiment. With
    level-only matching, 19 daily levels were tested against four months of
    1-minute bars; 11 of 12 armed setups were 'never_touched' because levels
    formed in February (e.g. 1365) sat far outside the August price range
    (~1000). The A/B comparison then had almost no trades and could conclude
    nothing.

    Carrying the formation bar and a bounded window means each level is only
    tested while it is actually live, which is both economically correct and
    what makes the trade population large enough to measure.
    """

    price: float
    #: Bar index in the series where the pattern completed (the level's birth).
    formed_idx: int = 0
    #: Trading days the level stays active. Beyond this it is stale.
    valid_days: int = 10
    #: Bars per day in the series being tested (1-minute bars: 375).
    bars_per_day: int = 375
    pattern: str = "RRG"

    @property
    def active_from(self) -> int:
        """First bar at which the level may be touched."""
        return max(0, int(self.formed_idx))

    @property
    def active_until(self) -> int:
        """Last bar at which the level may be touched (exclusive).

        Bounded by valid_days so a stale level cannot arm a setup long after
        the imbalance that created it has been absorbed.
        """
        if self.valid_days <= 0:
            return 10 ** 12                      # effectively unbounded
        return int(self.formed_idx) + int(self.valid_days) * int(self.bars_per_day)

    def is_active(self, bar_idx: int) -> bool:
        return self.active_from <= bar_idx < self.active_until


@dataclass
class Trade:
    """One simulated round trip."""

    entry_idx: int
    entry_price: float
    exit_idx: int
    exit_price: float
    level: float
    gross_bps: float
    net_bps: float
    rule: str
    bars_waited: Optional[int] = None
    confirm_z: Optional[float] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "rule": self.rule,
            "entry_idx": self.entry_idx,
            "entry_price": self.entry_price,
            "exit_idx": self.exit_idx,
            "exit_price": self.exit_price,
            "level": self.level,
            "gross_bps": self.gross_bps,
            "net_bps": self.net_bps,
            "bars_waited": self.bars_waited,
            "confirm_z": self.confirm_z,
        }


@dataclass
class SetupOutcome:
    """Every armed setup, whether it traded or was rejected.

    The filter's whole job is REJECTION. If rejected setups are dropped
    silently, the A/B comparison is meaningless - we would be comparing the
    survivors of one rule against the population of the other. So every
    armed setup is recorded here, with the reason it did or did not trade.
    """

    level: float
    arm_idx: int
    outcome: str          # "entered" | "no_flip" | "no_room" | "blocked"
    flip_idx: Optional[int] = None
    bars_waited: Optional[int] = None
    confirm_z: Optional[float] = None


def _close_trade(
    *,
    rule: str,
    entry_idx: int,
    entry_price: float,
    level: float,
    action: str,
    opens: Sequence[float],
    cfg: BacktestConfig,
    bars_waited: Optional[int] = None,
    confirm_z: Optional[float] = None,
) -> Optional[Trade]:
    """Close a trade at a fixed horizon; return None if the data runs out.

    Returns the trade with gross and net basis-point returns. Net subtracts
    the flat round-trip cost assumption.
    """
    exit_idx = entry_idx + cfg.hold_bars
    if exit_idx >= len(opens):
        return None                      # horizon extends past the data
    exit_price = opens[exit_idx]
    if entry_price <= 0:
        return None

    if action == "BUY":
        gross_bps = (exit_price - entry_price) / entry_price * 10_000.0
    else:
        gross_bps = (entry_price - exit_price) / entry_price * 10_000.0

    return Trade(
        entry_idx=entry_idx,
        entry_price=entry_price,
        exit_idx=exit_idx,
        exit_price=exit_price,
        level=level,
        gross_bps=gross_bps,
        net_bps=gross_bps - cfg.flat_cost_bps,
        rule=rule,
        bars_waited=bars_waited,
        confirm_z=confirm_z,
    )


def _level_spec(item, default_bars_per_day: int = 375) -> LevelSpec:
    """Normalise a levels entry into a LevelSpec.

    Accepts either a bare price (treated as an always-active level, which
    preserves the old behaviour for callers that do not care about windows)
    or a dict/tuple carrying the formation bar and validity.
    """
    if isinstance(item, LevelSpec):
        return item
    if isinstance(item, (int, float)):
        return LevelSpec(price=float(item), valid_days=0,
                         bars_per_day=default_bars_per_day)
    if isinstance(item, dict):
        return LevelSpec(
            price=float(item["price"]),
            formed_idx=int(item.get("formed_idx", 0)),
            valid_days=int(item.get("valid_days", 10)),
            bars_per_day=int(item.get("bars_per_day", default_bars_per_day)),
            pattern=str(item.get("pattern", "RRG")),
        )
    if isinstance(item, (tuple, list)) and len(item) >= 2:
        return LevelSpec(
            price=float(item[0]), formed_idx=int(item[1]),
            valid_days=int(item[2]) if len(item) > 2 else 10,
            bars_per_day=default_bars_per_day,
        )
    raise TypeError(f"unsupported level entry: {item!r}")


def _proxy_buyer(
    highs: Sequence[float], lows: Sequence[float],
    closes: Sequence[float], i: int,
) -> Optional[bool]:
    """OHLCV-only buyer-initiated proxy: close in the upper half of the range.

    This is an APPROXIMATION of the live ``ltp >= ask`` classification, used
    only because OHLCV carries no quote side. Any harness result depends on
    it, so the printed output labels the classification explicitly.
    """
    rng = highs[i] - lows[i]
    if rng <= 0:
        return None                 # no information -> excluded (fail closed)
    return (closes[i] - lows[i]) / rng >= 0.5


def run_touch_rule(
    data: Dict[str, Sequence[float]],
    levels: Sequence[Any],
    cfg: BacktestConfig,
) -> List[Trade]:
    """RULE A: enter at the next open after a keylevel touch. No confirmation.

    Each level is only considered while its validity window is open, so a
    stale level cannot arm a setup months after its pattern formed.
    """
    opens, lows, highs = data["open"], data["low"], data["high"]
    trades: List[Trade] = []
    consumed: set = set()
    next_free = 0

    for raw in levels:
        spec = _level_spec(raw, cfg.bars_per_day)
        level = spec.price
        if level in consumed:
            continue
        for i in touch_indices(
            data["close"], lows, highs, level,
            action=cfg.action, tolerance=cfg.touch_tolerance,
            active_from=spec.active_from, active_until=spec.active_until,
        ):
            if i < next_free:                 # one position at a time
                continue
            entry_idx = i + 1                 # fill at NEXT bar's open
            if entry_idx >= len(opens):
                continue
            trade = _close_trade(
                rule="touch", entry_idx=entry_idx,
                entry_price=opens[entry_idx], level=level,
                action=cfg.action, opens=opens, cfg=cfg,
            )
            if trade is None:
                continue
            trades.append(trade)
            consumed.add(level)
            next_free = trade.exit_idx + 1
            break                            # this level is spent
    return trades


def run_flip_rule(
    data: Dict[str, Sequence[float]],
    levels: Sequence[Any],
    cfg: BacktestConfig,
) -> Tuple[List[Trade], List[SetupOutcome]]:
    """RULE B: arm on touch, enter only after a z-ROC flip confirms.

    HTF-LEVEL SEMANTICS (operator correction)
    -----------------------------------------
    In the live system the trigger level comes from a single HTF pattern
    detection (see OCLKeylevelSignal._scan_htf_pattern). One pattern yields
    ONE level, and that setup is spent once it resolves. "First touch only"
    is therefore an HTF-pattern lifecycle rule, NOT a rule about repeated
    LTF touches of the same price - the LTF is expected to touch a level
    many times while the setup stays armed.

    So each element of ``levels`` here represents one HTF setup. Once its
    setup resolves (entered or rejected) the next level is considered. The
    LTF is free to oscillate around its level as much as it likes while the
    setup waits for a flip.

    Returns (trades, outcomes). ``outcomes`` includes rejected setups so the
    selection effect is measurable rather than invisible.
    """
    closes, opens = data["close"], data["open"]
    lows, highs, vols = data["low"], data["high"], data["volume"]

    z_series = zscored_roc(closes, period=cfg.roc_period, z_window=cfg.z_window)

    trades: List[Trade] = []
    outcomes: List[SetupOutcome] = []
    next_free = 0

    for raw in levels:
        spec = _level_spec(raw, cfg.bars_per_day)
        level = spec.price
        hits = touch_indices(
            closes, lows, highs, level,
            action=cfg.action, tolerance=cfg.touch_tolerance,
            active_from=spec.active_from, active_until=spec.active_until,
        )
        if not hits:
            outcomes.append(SetupOutcome(level=level, arm_idx=-1,
                                         outcome="never_touched"))
            continue

        # The setup arms at the first touch that does not collide with an
        # already-open position. Later touches are the LTF oscillating
        # around an armed level - which is normal and expected.
        arm_idx = next((h for h in hits if h >= next_free), None)
        if arm_idx is None:
            outcomes.append(SetupOutcome(level=level, arm_idx=hits[-1],
                                         outcome="blocked"))
            continue
        if arm_idx + 1 >= len(closes):
            outcomes.append(SetupOutcome(level=level, arm_idx=arm_idx,
                                         outcome="no_room"))
            continue

        # Daily buyer-initiated VWAP band, opened at the arming touch.
        # Classification here uses the OHLCV proxy (close in the upper half
        # of the day's range) because this harness has no live quotes. That
        # is an APPROXIMATION of the ltp>=ask rule, and is labelled as such
        # in the output rather than presented as real tape classification.
        acc = BuyerInitiatedDailyVWAP()
        acc.reset(level=level, action=cfg.action, start_bar=arm_idx)
        acc.add_daily_candle(
            price=closes[arm_idx], volume=vols[arm_idx],
            buyer_initiated=_proxy_buyer(highs, lows, closes, arm_idx),
        )

        # Scan forward for the first confirming flip. The setup stays armed
        # for as long as it takes (operator-confirmed: no bar discard).
        flip_idx: Optional[int] = None
        for j in range(arm_idx + 1, len(closes)):
            acc.add_daily_candle(
                price=closes[j], volume=vols[j],
                buyer_initiated=_proxy_buyer(highs, lows, closes, j),
            )
            if z_series[j] is None or z_series[j - 1] is None:
                continue
            if find_flip(
                z_series, start_idx=j,
                entry_z=cfg.entry_z, direction=cfg.action,
            ) == j:
                flip_idx = j
                break

        if flip_idx is None:
            # REJECTED: the touch happened, momentum never confirmed.
            outcomes.append(SetupOutcome(
                level=level, arm_idx=arm_idx, outcome="no_flip",
                bars_waited=len(closes) - 1 - arm_idx,
            ))
            continue

        entry_idx = flip_idx + 1         # fill at NEXT bar's open
        if entry_idx >= len(opens):
            outcomes.append(SetupOutcome(
                level=level, arm_idx=arm_idx, outcome="no_room",
                flip_idx=flip_idx, bars_waited=flip_idx - arm_idx,
            ))
            continue
        trade = _close_trade(
            rule="flip", entry_idx=entry_idx,
            entry_price=opens[entry_idx], level=level,
            action=cfg.action, opens=opens, cfg=cfg,
            bars_waited=flip_idx - arm_idx,
            confirm_z=z_series[flip_idx],
        )
        if trade is None:
            # Entry found but the exit horizon runs past the data.
            outcomes.append(SetupOutcome(
                level=level, arm_idx=arm_idx, outcome="no_room",
                flip_idx=flip_idx, bars_waited=flip_idx - arm_idx,
            ))
            continue
        trades.append(trade)
        outcomes.append(SetupOutcome(
            level=level, arm_idx=arm_idx, outcome="entered",
            flip_idx=flip_idx, bars_waited=flip_idx - arm_idx,
            confirm_z=z_series[flip_idx],
        ))
        next_free = trade.exit_idx + 1
    return trades, outcomes


# ===========================================================================
# Metrics
# ===========================================================================
def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5


def _t_stat(xs: Sequence[float]) -> Optional[float]:
    """One-sample t against zero. None when there are not enough samples.

    A t-stat is reported ONLY as a screening number. With fewer than ~30
    trades it is not meaningful, and with many correlated trades on the same
    instrument it overstates significance. Do not treat it as proof.
    """
    n = len(xs)
    if n < 2:
        return None
    sd = _stdev(xs)
    if sd <= 0:
        return None
    return _mean(xs) / (sd / (n ** 0.5))


def summarise(trades: Sequence[Trade], rule: str) -> Dict[str, object]:
    """Aggregate one rule's trades into the metrics that matter for a filter."""
    if not trades:
        return {
            "rule": rule, "trades": 0, "win_rate_pct": 0.0,
            "avg_net_bps": 0.0, "median_net_bps": 0.0,
            "total_net_bps": 0.0, "stdev_bps": 0.0, "t_stat": None,
            "worst_bps": 0.0, "best_bps": 0.0, "avg_bars_waited": None,
        }
    nets = [t.net_bps for t in trades]
    med = sorted(nets)[len(nets) // 2]
    waits = [t.bars_waited for t in trades if t.bars_waited is not None]
    return {
        "rule": rule,
        "trades": len(trades),
        "win_rate_pct": sum(1 for n in nets if n > 0) / len(nets) * 100.0,
        "avg_net_bps": _mean(nets),
        "median_net_bps": med,
        "total_net_bps": sum(nets),
        "stdev_bps": _stdev(nets),
        "t_stat": _t_stat(nets),
        "worst_bps": min(nets),
        "best_bps": max(nets),
        "avg_bars_waited": _mean(waits) if waits else None,
    }


def _fmt(v: object, width: int = 10) -> str:
    if v is None:
        return "n/a".rjust(width)
    if isinstance(v, float):
        return f"{v:,.2f}".rjust(width)
    return str(v).rjust(width)


def print_table(rows: Sequence[Dict[str, object]], title: str) -> None:
    """Plain aligned output - no charts, no colour, greppable."""
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    hdr = ("rule", "trades", "win%", "avg_net", "median", "total_net",
           "stdev", "t", "worst", "best", "wait")
    widths = (10, 7, 7, 10, 9, 11, 9, 7, 8, 8, 7)
    print("".join(h.rjust(w) for h, w in zip(hdr, widths)))
    print("-" * 78)
    for r in rows:
        cells = (
            r["rule"], r["trades"], r["win_rate_pct"], r["avg_net_bps"],
            r["median_net_bps"], r["total_net_bps"], r["stdev_bps"],
            r["t_stat"], r["worst_bps"], r["best_bps"], r["avg_bars_waited"],
        )
        print("".join(_fmt(c, w) for c, w in zip(cells, widths)))
    print("-" * 78)


# ===========================================================================
# Walk-forward split
# ===========================================================================
def split_folds(n_bars: int, folds: int) -> List[Tuple[int, int]]:
    """Split ``n_bars`` into ``folds`` consecutive (start, end) segments."""
    if folds < 1:
        raise ValueError("folds must be >= 1")
    size = n_bars // folds
    out: List[Tuple[int, int]] = []
    for k in range(folds):
        start = k * size
        end = n_bars if k == folds - 1 else (k + 1) * size
        out.append((start, end))
    return out


def slice_data(
    data: Dict[str, Sequence[float]], start: int, end: int
) -> Dict[str, Sequence[float]]:
    """Slice every column to [start, end)."""
    return {k: v[start:end] for k, v in data.items()}


# ===========================================================================
# Data loading
# ===========================================================================
def load_csv(path: str) -> Dict[str, List[float]]:
    """Load OHLCV columns from a CSV using the standard library only.

    Kept dependency-free on purpose: this harness must run in the venv on
    this machine, which does NOT have pandas guarantees outside the package.
    """
    import csv

    cols: Dict[str, List[float]] = {
        "open": [], "high": [], "low": [], "close": [], "volume": [],
    }
    with open(path, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in cols if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(
                f"CSV missing required column(s): {missing}; "
                f"found {reader.fieldnames}"
            )
        for row in reader:
            for c in cols:
                cols[c].append(float(row[c]))
    if not cols["close"]:
        raise ValueError(f"CSV {path} contained no data rows")
    return cols


def synthetic_data(n: int = 900, seed: int = 7) -> Dict[str, List[float]]:
    """Deterministic synthetic series for harness validation.

    NOT market data. Used only so the harness can be exercised end to end
    and so the regression suite has a fixed, reproducible input. Any output
    from this series is a property of the harness, not of any market.
    """
    import random

    rng = random.Random(seed)
    price = 22_000.0
    closes: List[float] = []
    for i in range(n):
        # A gentle drift plus a periodic component so the series actually
        # revisits levels instead of trending away from them.
        drift = 0.15
        wave = 6.0 * (1 if (i // 60) % 2 == 0 else -1)
        price += drift + wave * 0.02 + rng.gauss(0, 9.0)
        closes.append(round(price, 2))

    highs = [round(c + abs(rng.gauss(0, 4.0)), 2) for c in closes]
    lows = [round(c - abs(rng.gauss(0, 4.0)), 2) for c in closes]
    opens = [round((closes[i - 1] if i else closes[0]), 2)
             for i in range(len(closes))]
    vols = [1000 + int(abs(rng.gauss(0, 250))) for _ in closes]
    return {"open": opens, "high": highs, "low": lows,
            "close": closes, "volume": vols}


# ===========================================================================
# Main
# ===========================================================================
def compare(
    data: Dict[str, Sequence[float]],
    levels: Sequence[float],
    cfg: BacktestConfig,
    folds: int = 1,
    label: str = "FULL SAMPLE",
) -> Dict[str, object]:
    """Run both rules over the data (optionally fold by fold) and report."""
    n = len(data["close"])
    results: Dict[str, object] = {"label": label, "bars": n, "folds": []}

    for k, (start, end) in enumerate(split_folds(n, folds)):
        seg = slice_data(data, start, end)
        if len(seg["close"]) < cfg.roc_period + cfg.z_window + cfg.hold_bars + 2:
            print(f"\n[fold {k+1}] skipped: only {len(seg['close'])} bars "
                  f"(need > {cfg.roc_period + cfg.z_window + cfg.hold_bars + 2} "
                  f"for ROC({cfg.roc_period}) + z_window({cfg.z_window}) + "
                  f"hold({cfg.hold_bars}))")
            continue

        touch_trades = run_touch_rule(seg, levels, cfg)
        flip_trades, outcomes = run_flip_rule(seg, levels, cfg)
        rows = [
            summarise(touch_trades, "touch"),
            summarise(flip_trades, "flip"),
        ]
        print_table(rows, f"{label} - FOLD {k+1}/{folds}  "
                          f"(bars {start}-{end}, {len(seg['close'])} bars)")

        # --- Rejection accounting: the filter's actual mechanism ---
        entered = [o for o in outcomes if o.outcome == "entered"]
        no_flip = [o for o in outcomes if o.outcome == "no_flip"]
        other = [o for o in outcomes if o.outcome not in ("entered", "no_flip")]
        print(f"  setups armed   : {len(outcomes)}")
        print(f"    entered      : {len(entered)}")
        print(f"    rejected     : {len(no_flip)}  (touched, no momentum flip)")
        for tag in sorted({o.outcome for o in other}):
            print(f"    {tag:<12} : {sum(1 for o in other if o.outcome == tag)}")
        if outcomes:
            rej_pct = len(no_flip) / len(outcomes) * 100.0
            print(f"    rejection rate: {rej_pct:.1f}% of armed setups")

        # Net difference: the number the whole exercise exists to produce.
        a, b = rows[0], rows[1]
        if a["trades"] and b["trades"]:
            print(f"  -> flip adds {b['avg_net_bps'] - a['avg_net_bps']:+,.2f} "
                  f"bps avg per trade, with "
                  f"{b['trades'] - a['trades']:+d} trades "
                  f"({(b['trades']/a['trades'] - 1) * 100:+.1f}% count change)")
            if b["trades"] == a["trades"]:
                print("     WARNING: identical trade counts - the filter is "
                      "selecting the same setups, so it is not filtering.")
        else:
            print("  -> one rule produced no trades; comparison not defined")

        results["folds"].append({
            "fold": k + 1, "touch": a, "flip": b,
            "setups_armed": len(outcomes),
            "rejected_no_flip": len(no_flip),
        })
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="A/B backtest: keylevel touch vs touch+z-ROC confirmation")
    ap.add_argument("--csv", help="OHLCV csv (open,high,low,close,volume)")
    ap.add_argument("--levels", help="comma-separated level prices")
    ap.add_argument("--levels-json",
                    help="JSON file of level specs carrying formation bar and "
                         "validity window (produced by make_real_levels.py)")
    ap.add_argument("--valid-days", type=int, default=10,
                    help="days a level stays actionable after its pattern "
                         "forms (used only for plain --levels)")
    ap.add_argument("--synthetic", action="store_true",
                    help="use the deterministic synthetic series (NO market data)")
    ap.add_argument("--folds", type=int, default=1,
                    help="walk-forward segments (default 1 = whole sample)")
    ap.add_argument("--entry-z", type=float, default=1.0)
    ap.add_argument("--roc-period", type=int, default=10)
    ap.add_argument("--z-window", type=int, default=100)
    ap.add_argument("--cost-bps", type=float, default=8.0)
    ap.add_argument("--hold-bars", type=int, default=20)
    ap.add_argument("--action", default="BUY", choices=("BUY", "SELL"))
    args = ap.parse_args(argv)

    cfg = BacktestConfig(
        roc_period=args.roc_period,
        z_window=args.z_window,
        entry_z=args.entry_z,
        flat_cost_bps=args.cost_bps,
        hold_bars=args.hold_bars,
        action=args.action,
    )

    if args.synthetic:
        data = synthetic_data()
        label = "SYNTHETIC (not market data)"
    elif args.csv:
        data = load_csv(args.csv)
        label = os.path.basename(args.csv)
    else:
        print("error: pass --csv <path> or --synthetic", file=sys.stderr)
        return 2

    if args.levels_json:
        import json as _json
        with open(args.levels_json, encoding="utf-8") as fh:
            levels = _json.load(fh)
        print(f"[levels] {len(levels)} level spec(s) loaded from "
              f"{args.levels_json} (each carries its own validity window)")
    elif args.levels:
        levels = [float(x) for x in args.levels.split(",") if x.strip()]
    else:
        # No levels given: bracket the observed range. These carry no
        # formation bar, so they are treated as always-active - useful only
        # as a smoke test, never as a real signal test.
        lo, hi = min(data["low"]), max(data["high"])
        span = hi - lo
        levels = [round(lo + span * f, 2) for f in (0.25, 0.5, 0.75)]
        print(f"[levels] none supplied; using range brackets {levels}")

    print("=" * 78)
    print("OCL CONFIRMATION A/B BACKTEST")
    print("=" * 78)
    print(f"data        : {label}")
    print(f"bars        : {len(data['close'])}")
    print(f"levels      : {levels}")
    print(f"rule config : ROC({cfg.roc_period}) z_window={cfg.z_window} "
          f"entry_z={cfg.entry_z} action={cfg.action}")
    print(f"fills       : signal on bar t close -> fill at bar t+1 open")
    print(f"exit        : {cfg.hold_bars} bars after entry, at the open")
    print(f"costs       : {cfg.flat_cost_bps} bps flat round trip")
    print(f"folds       : {args.folds}")
    print()
    print("NOTE: numbers below are BACKTESTED, not live. Costs are a flat")
    print("      assumption, not a measured slippage model. Fewer than ~100")
    print("      trades per fold is too small to conclude anything.")

    compare(data, levels, cfg, folds=args.folds, label=label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())