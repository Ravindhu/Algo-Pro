"""
e2e_replay.py - end-to-end replay simulator for the OCL confirmation chain.

==============================================================================
PURPOSE
==============================================================================
Exercise the REAL production path against recorded data, with no broker and
no live market, so the whole chain can be observed before it touches money:

    daily OCL pattern (RRG, BUY-only)
      -> daily keylevel touch            (arms the setup)
      -> 15-min z-scored ROC flip        (confirms momentum)
      -> daily buyer-initiated VWAP band (+ ceiling)
      -> POV slicer slicing child orders
      -> ceiling halt stops slicing

Unlike backtest_ocl_confirm.py (which re-implements the rules as an A/B
comparison), this script imports and runs the ACTUAL classes:

    blue_engine.alpha.Statergy.ocl_keylevel.OCLKeylevelSignal
    blue_engine.pov.slicers.pov.POVSlicer

so what it validates is the shipped code, not a model of it.

==============================================================================
WHAT IS REAL AND WHAT IS SIMULATED - READ THIS
==============================================================================
REAL:
  * the signal's pattern detection, touch check, ROC gate, daily band
  * the slicer's POV volume gate, band rate, ceiling halt, child placement
  * the intent dict passed between them

SIMULATED:
  * the broker. A RecordingClient replays recorded candles for history()
    and returns canned quotes/depth/orderbook. placeorder() does NOT reach
    any broker; it records what would have been sent.
  * the ledger is redirected to a temp file.

THEREFORE: fills reported here are SIMULATED, not executed. Every number is
BACKTESTED/REPLAYED, never live. The run refuses to start unless
BLUE_E2E_CONFIRM=YES is set, so it can never be mistaken for a live run.

==============================================================================
USAGE
==============================================================================
    $env:BLUE_E2E_CONFIRM="YES"
    python Temp/e2e_replay.py --synthetic
    python Temp/e2e_replay.py --daily daily.csv --ltf ltf15.csv

CSV columns: open, high, low, close, volume  (+ optional datetime)
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_ENGINE = os.path.join(_ROOT, "Blue_engine")
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)


# ---------------------------------------------------------------------------
# Safety interlock - this must never be mistaken for a live run
# ---------------------------------------------------------------------------
def _require_opt_in() -> None:
    if os.environ.get("BLUE_E2E_CONFIRM") != "YES":
        print(
            "refusing to run: this is a REPLAY SIMULATOR, not a live runner.\n"
            "It drives the real signal/slicer classes against recorded data\n"
            "with a fake broker, and reports SIMULATED fills.\n\n"
            "If that is what you want, re-run with:\n"
            '    $env:BLUE_E2E_CONFIRM="YES"\n',
            file=sys.stderr,
        )
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# Recorded-data client
# ---------------------------------------------------------------------------
@dataclass
class RecordedOrder:
    """One child order the slicer WOULD have sent."""

    symbol: str
    action: str
    quantity: int
    price_type: str
    price: Optional[float]
    ts: float
    seq: int


@dataclass
class RecordingClient:
    """A stand-in for the OpenAlgo SDK backed by recorded candles.

    history()  -> replays the recorded frame for the requested interval
    quotes()   -> synthesises a quote from the current LTF candle
    depth()    -> synthesises a two-sided book around the LTP
    placeorder -> records the order; touches no broker
    orderbook/cancelorder -> minimal stubs so slicer guards behave
    """

    daily: List[Dict[str, Any]] = field(default_factory=list)
    ltf: List[Dict[str, Any]] = field(default_factory=list)
    orders: List[RecordedOrder] = field(default_factory=list)
    #: Index into the LTF frame that `quotes()` should report.
    ltf_cursor: int = 0
    #: Index into the daily frame.
    daily_cursor: int = 0
    #: Optional injected ask price; when None the LTF close is used.
    ask_override: Optional[float] = None
    #: When True, each quotes() call advances the LTF cursor by one bar.
    #: The POV slicer slices against CUMULATIVE traded volume: if the replay
    #: serves the same candle every poll, volume never grows and the core
    #: POV gate correctly refuses to slice ("vol stalled"). Advancing the
    #: cursor simulates a market where trades keep printing.
    advance_on_quote: bool = False
    #: Set True to make the broker reject orders (failure-path testing).
    reject_orders: bool = False
    order_mode: str = "MARKET"
    #: Minimum bars a history() call must return, ending at the cursor.
    #: A live history call always returns a full ROLLING window; a replay
    #: that returns only "bars up to the cursor" starves the z-score, which
    #: needs roc_period + z_window bars to compute anything at all. With a
    #: truncated prefix the z-score was permanently 'n/a' and the flip could
    #: never fire, so the confirmation gate was untestable on real data.
    min_lookback: int = 120
    _seq: int = 0

    # -- market data -------------------------------------------------------
    def history(self, *, symbol=None, exchange=None, interval=None,
                start_date=None, end_date=None, **kwargs):
        """Return the recorded frame for this interval.

        Three interval classes are served, matching what the production
        stack actually requests:
          * daily  -> the daily frame (OCL pattern + confirmation band)
          * 1m/minute -> the LTF frame, volume included. The POV slicer
            reaches this through volume_cache.get_quote(), which calls
            history(interval="1m") when its WebSocket cache is empty. If
            this path returned no volume the slicer would see
            "volume_cache: no volume data available" and pause.
          * 15minute / other -> the LTF frame (signal touch monitoring)
        """
        iv = str(interval or "").lower()
        wants_records = (
            kwargs.get("source") == "api"
            or iv in ("1m", "1minute", "minute")
        )
        if iv in ("day", "1day", "daily", "d"):
            rows = self.daily[: self.daily_cursor] or self.daily
        else:
            # ROLLING LOOKBACK, mirroring a live history call.
            #
            # The signal asks for a window of LTF bars to compute z-scored
            # ROC over. Returning only ltf[:cursor+1] gives a 1-, 2-, 3-bar
            # frame early in the walk - far below the roc_period + z_window
            # the z-score needs - so z was always 'n/a' and no flip could
            # ever fire. Serving at least min_lookback bars ending at the
            # cursor is what a real feed does, and it makes the gate testable.
            end = min(self.ltf_cursor + 1, len(self.ltf))
            start = max(0, end - max(self.min_lookback, self._lookback_need()))
            rows = self.ltf[start:end] or self.ltf[:end]
        if not rows:
            return {"status": "error", "message": f"no recorded data for {iv}"}
        if wants_records:
            return {"status": "success", "data": [dict(r) for r in rows]}
        return {"status": "success", "data": _Frame(rows)}

    def _lookback_need(self) -> int:
        """Bars the caller's z-score configuration requires.

        Set by run_e2e from the signal config so the served window is never
        shorter than roc_period + z_window. Without this the window size and
        the indicator's requirement could drift apart silently.
        """
        return int(getattr(self, "zscore_bars_needed", 0) or 0)

    def quotes(self, *, symbol=None, exchange=None, **kwargs):
        if not self.ltf:
            return {"status": "error", "message": "no ltf data"}
        if self.advance_on_quote:
            self.ltf_cursor = min(self.ltf_cursor + 1, len(self.ltf) - 1)
        idx = min(self.ltf_cursor, len(self.ltf) - 1)
        row = self.ltf[idx]
        ask = (self.ask_override if self.ask_override is not None
               else float(row["close"]) * 1.0005)
        # Cumulative volume up to the cursor, so the slicer's POV gate sees a
        # growing tape rather than a frozen snapshot.
        cum_vol = sum(float(r["volume"]) for r in self.ltf[: idx + 1])
        return {"status": "success", "data": {
            "ltp": float(row["close"]),
            "ask": ask,
            "bid": float(row["close"]) * 0.9995,
            "volume": cum_vol,
            "upper_circuit_limit": float(row["close"]) * 1.20,
            "lower_circuit_limit": float(row["close"]) * 0.80,
            "tick_size": 0.05,
        }}

    def depth(self, *, symbol=None, exchange=None, **kwargs):
        q = self.quotes(symbol=symbol, exchange=exchange)
        if q.get("status") != "success":
            return q
        d = q["data"]
        ltp = d["ltp"]
        return {"status": "success", "data": {
            "bids": [{"price": ltp * 0.9995, "quantity": 10_000}],
            "asks": [{"price": ltp * 1.0005, "quantity": 10_000}],
            "tick_size": 0.05,
        }}

    def symbol(self, symbol=None, exchange=None, **kwargs):
        return {"status": "success", "data": {
            "symbol": symbol, "exchange": exchange, "lot_size": 1,
            "segment": "EQ", "series": "EQ", "instrumenttype": "EQ",
        }}

    # -- market data (volume_cache fallback contract) -----------------------
    # volume_cache._sdk_fallback() calls client.history(...) with a WINDOW
    # (start_date/end_date) and expects a list of 1-minute records. It then
    # SUMS their volume. To make the POV gate see growth, the window must
    # contain more bars as the replay advances - so the returned slice is
    # proportional to the current cursor rather than always the full frame.
    def _one_minute_window(self) -> List[Dict[str, Any]]:
        """Bars visible to the volume_cache fallback at this cursor.

        Must GROW monotonically with the cursor: the slicer derives
        cumulative volume by summing this window, so a constant window reads
        as "vol stalled" and the POV gate refuses to slice.
        """
        upto = min(self.ltf_cursor + 1, len(self.ltf))
        return [dict(r) for r in self.ltf[:upto]]

    # -- order management --------------------------------------------------
    def placeorder(self, *, strategy=None, symbol=None, action=None,
                   exchange=None, price_type=None, product=None,
                   quantity=None, price=None, **kwargs):
        self._seq += 1
        if self.reject_orders:
            return {"status": "error", "message": "simulated broker reject"}
        self.orders.append(RecordedOrder(
            symbol=str(symbol), action=str(action), quantity=int(quantity),
            price_type=str(price_type or self.order_mode),
            price=None if price is None else float(price),
            ts=time.time(), seq=self._seq,
        ))
        return {"status": "success",
                "order_id": f"SIM-{self._seq:04d}",
                "data": {"order_id": f"SIM-{self._seq:04d}"}}

    def orderbook(self, **kwargs):
        return {"status": "success", "data": []}

    def cancelorder(self, *, order_id=None, strategy=None, **kwargs):
        return {"status": "success", "data": {"order_id": order_id}}


class _Frame:
    """Minimal DataFrame-like shim: the signal uses .iloc, .tolist, len, [col].

    Avoids a pandas dependency in the simulator while still satisfying the
    exact access patterns OCLKeylevelSignal performs.
    """

    def __init__(self, rows: Sequence[Dict[str, Any]]) -> None:
        self._rows = [dict(r) for r in rows]
        self._cols = list(self._rows[0].keys()) if self._rows else []

    def __len__(self) -> int:
        return len(self._rows)

    def __getitem__(self, col):
        if col in ("open", "high", "low", "close", "volume", "datetime", "ask"):
            return _Column([r.get(col) for r in self._rows])
        raise KeyError(col)

    @property
    def index(self):
        return _Index(list(range(len(self._rows))))

    @property
    def iloc(self):
        """Positional accessor, mirroring pandas' df.iloc[...] attribute.

        Must be a PROPERTY, not a method: the production code calls
        ``df.iloc[-1]``, and a method would raise
        "'method' object is not subscriptable".
        """
        return _ILoc(self._rows)

    def iterrows(self):
        """Yield (position, row) pairs, mirroring pandas' iteration API."""
        for i, row in enumerate(self._rows):
            yield i, _Row(row)


class _Column:
    """Column shim. Strings the values so date comparisons work, and stays
    subscriptable so callers may index or slice it."""

    def __init__(self, values):
        self._v = ["" if v is None else str(v) for v in values]

    def tolist(self):
        return list(self._v)

    def astype(self, _t):
        return self

    def str(self):
        return self

    def __getitem__(self, k):
        return self._v[k]

    def __len__(self):
        return len(self._v)

    def __iter__(self):
        return iter(self._v)


class _Index(_Column):
    """Index shim: yields numeric-looking strings so ``d[:10]`` works."""

    def __init__(self, values):
        self._v = [str(v) for v in values]


class _ILoc:
    def __init__(self, rows):
        self._rows = rows

    def __getitem__(self, k):
        # Slices must yield a FRAME (so .iterrows() keeps working), while an
        # integer index yields a single ROW. pandas behaves the same way, and
        # the production code relies on both forms.
        if isinstance(k, slice):
            return _Frame(self._rows[k])
        return _Row(self._rows[k])


class _Row:
    def __init__(self, row):
        self._row = dict(row)

    def to_dict(self):
        return dict(self._row)

    def get(self, k, default=None):
        return self._row.get(k, default)

    def keys(self):
        return self._row.keys()

    def __contains__(self, k):
        return k in self._row

    def __getitem__(self, k):
        return self._row[k]

    def __iter__(self):
        return iter(self._row)


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------
def synthetic_daily(n: int = 60) -> List[Dict[str, Any]]:
    """Daily candles forming an RRG (bear,bear,bull) at the end."""
    import random
    rng = random.Random(11)
    rows: List[Dict[str, Any]] = []
    px = 22_000.0
    for i in range(n - 3):
        px += rng.gauss(3, 40)
        rows.append(_mk(f"2026-{(i//28)+1:02d}-{(i%28)+1:02d}",
                        px, rng, bearish=rng.random() < 0.5))
    # Candle 1: bearish, Candle 2: bearish, Candle 3: bullish -> RRG
    px -= 120
    rows.append(_mk("2026-06-01", px, rng, bearish=True, body=90))
    px -= 140
    rows.append(_mk("2026-06-02", px, rng, bearish=True, body=110))
    px += 220
    rows.append(_mk("2026-06-03", px, rng, bearish=False, body=180))
    return rows


def _mk(date: str, close: float, rng, *, bearish: bool,
        body: float = 60.0) -> Dict[str, Any]:
    if bearish:
        o, c = close + body, close
    else:
        o, c = close - body, close
    hi = max(o, c) + abs(rng.gauss(0, 15))
    lo = min(o, c) - abs(rng.gauss(0, 15))
    return {"datetime": date, "open": round(o, 2), "high": round(hi, 2),
            "low": round(lo, 2), "close": round(c, 2),
            "volume": 1_000_000 + int(abs(rng.gauss(0, 100_000)))}


def synthetic_ltf(n: int = 300, base: float = 21_700.0,
                  breach_ceiling: bool = False,
                  ceiling: Optional[float] = None) -> List[Dict[str, Any]]:
    """15-min candles: decline -> touch -> reversal flip -> continuation.

    When ``breach_ceiling`` is set, the final third of the series is forced
    above ``ceiling`` so the integrated run exercises the ceiling-halt path.
    Without this the synthetic price never reaches the ceiling, and the
    halt - the requirement the operator emphasised - is never proven end to
    end (only in unit tests).
    """
    import random
    rng = random.Random(23)
    rows: List[Dict[str, Any]] = []
    px = base
    breach_from = int(n * 0.75) if breach_ceiling else n + 1
    for i in range(n):
        if i < n // 2:
            px -= rng.uniform(1.5, 6.0)          # decline toward the level
        elif i < breach_from:
            px += rng.uniform(0.5, 3.0)          # the reversal / flip
        elif breach_ceiling and ceiling is not None:
            # Drive decisively above the ceiling so the halt is unambiguous.
            px = max(px + rng.uniform(2.0, 8.0), ceiling * 1.002)
        else:
            px += rng.uniform(1.0, 5.0)
        hi = px + abs(rng.gauss(0, 4))
        lo = px - abs(rng.gauss(0, 4))
        rows.append({
            "datetime": f"2026-06-03 09:{15 + (i % 4) * 15:02d}",
            "open": round(px + rng.gauss(0, 2), 2),
            "high": round(hi, 2), "low": round(lo, 2),
            "close": round(px, 2),
            "volume": 50_000 + int(abs(rng.gauss(0, 10_000))),
        })
    return rows


def load_csv(path: str) -> List[Dict[str, Any]]:
    import csv
    rows: List[Dict[str, Any]] = []
    with open(path, "r", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append({
                "datetime": row.get("datetime") or row.get("date") or "",
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row["volume"]),
            })
    return rows


# ---------------------------------------------------------------------------
# The end-to-end run
# ---------------------------------------------------------------------------
def run_e2e_quiet(daily: List[Dict[str, Any]], ltf: List[Dict[str, Any]],
                  *, ceiling_pct: float = 0.5) -> Dict[str, Any]:
    """Discover the ceiling WITHOUT running the slicer.

    The probe only needs the intent the signal produces - running the full
    slicer here as well would double the wall-clock cost of every breach
    run for no extra information, and made the simulator appear to hang.
    So the probe stops after Phase 2.
    """
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = run_e2e(daily, ltf, ceiling_pct=ceiling_pct,
                         quiet=True, signal_only=True)
    band = ((result.get("intent") or {}).get("_meta") or {}).get("band") or {}
    return {"ceiling": band.get("ceiling_price"), "result": result}


def run_e2e(daily: List[Dict[str, Any]], ltf: List[Dict[str, Any]],
            *, confirm_enabled: bool = True, ceiling_pct: float = 0.5,
            max_ltf_steps: Optional[int] = None,
            quiet: bool = False, signal_only: bool = False,
            roc_period: int = 5, z_window: int = 20,
            entry_z: float = 0.8, ltf_interval: str = "15minute") -> Dict[str, Any]:
    """Drive the real signal + slicer through a replayed session."""
    from blue_engine.alpha.Statergy.ocl_keylevel import OCLKeylevelSignal
    from blue_engine.pov.slicers.pov import POVSlicer

    client = RecordingClient(daily=daily, ltf=ltf)
    # The slicer slices against growing cumulative volume; the client must
    # advance its tape on each quote or the POV gate correctly stalls.
    client.advance_on_quote = True
    # Tell the client how many bars the z-score needs, so the rolling window
    # it serves is never shorter than roc_period + z_window. Without this the
    # z-score stayed permanently 'n/a' and the confirmation gate could not be
    # exercised at all.
    client.zscore_bars_needed = roc_period + z_window + 10
    # The slicer slices against growing cumulative volume; the client must
    # advance its tape on each quote or the POV gate correctly stalls.
    client.advance_on_quote = True

    signal_cfg = {
        "symbol": "NIFTY", "exchange": "NSE",
        "htf_interval": "day", "ltf_interval": ltf_interval,
        "band_interval": "day", "band_ceiling_pct": ceiling_pct,
        "pattern_type": "RRG", "min_body_pct": 0.1,
        "trade_direction": "AUTO",
        "action": "BUY", "quantity": 1000,
        "poll_seconds": 0, "one_shot": True,
        "exec_mode": "ONE_SHOT",
        "ltf_touch_buffer": 25.0,
        "confirm_enabled": confirm_enabled,
        "roc_period": roc_period, "z_window": z_window, "entry_z": entry_z,
        "strategy_label": "E2E-OCL",
    }

    # --- Phase 1: find the level from the DAILY pattern ---
    sig = OCLKeylevelSignal(client, signal_cfg)
    print("\n--- PHASE 1: daily OCL pattern scan ---")
    ok = sig._scan_htf_pattern()
    print(f"pattern detected : {ok}")
    print(f"trigger level    : {sig._trigger_level}")
    if not ok or sig._trigger_level is None:
        return {"phase": "pattern", "level": None, "intent": None,
                "orders": client.orders, "filled": 0}

    # --- Phase 2: walk the LTF, stepping the replay cursor ---
    print("\n--- PHASE 2: LTF walk (touch -> confirm -> intent) ---")
    intent: Optional[Dict[str, Any]] = None
    steps = 0
    limit = max_ltf_steps or len(ltf)
    for i in range(len(ltf)):
        client.ltf_cursor = i
        client.daily_cursor = len(daily)      # full daily history available
        steps += 1
        result = sig._monitor_ltf_touch()
        if i % 25 == 0:
            print(f"  step {i:>3}: state={sig._confirm.state if sig._confirm else 'n/a'} "
                  f"ltp={ltf[i]['close']:.2f}")
        if result:
            intent = result
            print(f"  step {i:>3}: FIRED")
            break
        if steps >= limit:
            break

    if intent is None:
        print("\nRESULT: no intent produced "
              "(pattern, touch, or confirmation never completed)")
        return {"phase": "no_intent", "level": sig._trigger_level,
                "intent": None, "orders": client.orders, "filled": 0}

    if signal_only:
        # Probe mode: the ceiling is already known from the intent, so stop
        # here rather than paying for a second slicer execution.
        return {"phase": "signal_only", "level": sig._trigger_level,
                "intent": intent, "orders": client.orders, "filled": 0}

    # --- Phase 3: hand the intent to the REAL slicer ---
    print("\n--- PHASE 3: POV slicer execution ---")
    band = (intent.get("_meta") or {}).get("band") or {}
    print(f"intent action/qty : {intent['action']} {intent['quantity']}")
    print(f"band buyer_vwap   : {band.get('buyer_vwap')}")
    print(f"band ceiling      : {band.get('ceiling_price')}")
    print(f"band classification: {band.get('classification')}")

    slicer_cfg = {
        "action": intent["action"], "quantity": intent["quantity"],
        "symbol": "NIFTY", "exchange": "NSE",
        "order_mode": "MARKET",
        "participation_pct": 25.0,
        # The replay drives quotes, not the wall clock. duration must be long
        # enough for the loop body to actually run several polls - at 0.05s
        # the loop's end_ts had already passed before the first quote fetch,
        # so the body never executed and the ceiling halt was never reached
        # (ceiling_halts stayed 0 while the tape sat above the ceiling).
        # 0.4s at 0.01s intervals yields roughly 40 polls: plenty to exercise
        # both the slice path and the halt, while still finishing instantly.
        "duration": 0.4, "interval": 0.01,
        "max_slice_quantity": 200,
        "strategy_label": "E2E-OCL",
        "min_pov_pct": 5.0, "max_pov_pct": 25.0,
    }
    slicer = POVSlicer(client, slicer_cfg)

    # Count ceiling halts and shipped slices from the slicer's own log so the
    # integrated halt behaviour is observable, not just unit-tested.
    seen = {"polls": 0, "halts": 0, "slices": 0}
    real_log = slicer.log

    def counting_log(msg: str) -> None:
        if "CEILING HALT" in msg:
            seen["halts"] += 1
        if "child placed" in msg:
            seen["slices"] += 1
        if not quiet:
            real_log(msg)

    slicer.log = counting_log

    # Execute the slicer for real over the replay window.
    #
    # The slicer's ceiling check is driven by the quotes it polls WHILE
    # SLICING. For the halt to be exercised, the replayed tape during slicing
    # must sit above the ceiling - so if a breach exists, park the cursor on
    # the breach tail before executing. Otherwise the slicer would sample the
    # confirmation bars (below the ceiling) and legitimately never halt.
    if band.get("ceiling_price") is not None:
        ceiling_px = float(band["ceiling_price"])
        breach_idx = next(
            (i for i in range(len(ltf)) if ltf[i]["close"] > ceiling_px),
            None,
        )
        if breach_idx is not None:
            client.ltf_cursor = breach_idx
            print(f"tape parked on breach bar {breach_idx} "
                  f"({ltf[breach_idx]['close']:.2f} > ceiling {ceiling_px:.2f})")
        else:
            print(f"no bar in the replayed tape exceeds the ceiling "
                  f"{ceiling_px:.2f}; the halt cannot be exercised")

    summary = slicer.execute(intent)

    print(f"\nslicer summary    : {summary}")
    print(f"ceiling halts seen: {summary.get('ceiling_halts', 0)}")
    print(f"child orders sent : {len(client.orders)}")

    return {"phase": "complete", "level": sig._trigger_level,
            "intent": intent, "orders": client.orders,
            "filled": summary.get("filled", 0), "summary": summary}


# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="End-to-end replay of the OCL confirmation chain")
    ap.add_argument("--synthetic", action="store_true",
                    help="use built-in synthetic daily + 15min frames")
    ap.add_argument("--daily", help="daily OHLCV csv")
    ap.add_argument("--ltf", help="15-minute OHLCV csv")
    ap.add_argument("--confirmed", action="store_true", default=True,
                    help="run with the confirmation gate ON (default)")
    ap.add_argument("--no-confirm", dest="confirmed", action="store_false",
                    help="run the legacy touch-only path for comparison")
    ap.add_argument("--ceiling-pct", type=float, default=0.5)
    ap.add_argument("--breach-ceiling", action="store_true",
                    help="extend the synthetic tail above the computed "
                         "ceiling to exercise the halt path")
    args = ap.parse_args(argv)

    _require_opt_in()

    if args.synthetic:
        daily = synthetic_daily()
        # First pass: build the LTF, run the chain, read back the ceiling the
        # signal computed, then REBUILD the LTF with a tail that breaches it.
        # This lets one command exercise the touch -> confirm -> slice path
        # AND the ceiling-halt path in the same session.
        ltf = synthetic_ltf()
        if args.breach_ceiling:
            probe = run_e2e_quiet(daily, ltf, ceiling_pct=args.ceiling_pct)
            ceiling = probe.get("ceiling")
            if ceiling is None:
                print("[breach] no ceiling could be derived; "
                      "run continues without a forced breach")
            else:
                print(f"[breach] forcing the LTF tail above the "
                      f"computed ceiling {ceiling:.2f}")
                ltf = synthetic_ltf(breach_ceiling=True, ceiling=ceiling)
    elif args.daily and args.ltf:
        daily, ltf = load_csv(args.daily), load_csv(args.ltf)
    else:
        print("error: pass --synthetic or both --daily and --ltf",
              file=sys.stderr)
        return 2

    print("=" * 78)
    print("OCL CONFIRMATION - END-TO-END REPLAY (SIMULATED, NOT LIVE)")
    print("=" * 78)
    print(f"daily candles : {len(daily)}")
    print(f"ltf candles   : {len(ltf)}")
    print(f"confirm gate  : {'ON' if args.confirmed else 'OFF (legacy)'}")
    print(f"ceiling pct   : +{args.ceiling_pct}% on the daily buyer VWAP")
    print("\nNOTE: the broker is SIMULATED. placeorder() records orders and")
    print("      fills nothing. Every figure below is REPLAYED, not live.")

    result = run_e2e(daily, ltf, confirm_enabled=args.confirmed,
                     ceiling_pct=args.ceiling_pct)

    print("\n" + "=" * 78)
    print("RESULT")
    print("=" * 78)
    print(f"phase           : {result['phase']}")
    print(f"keylevel        : {result['level']}")
    print(f"filled (sim)    : {result['filled']}")
    if result.get("orders"):
        print("recorded child orders:")
        for o in result["orders"][:15]:
            print(f"  #{o.seq} {o.action} {o.quantity} @ "
                  f"{o.price if o.price is not None else 'MARKET'} ({o.price_type})")
    else:
        print("recorded child orders: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())