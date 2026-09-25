"""
portfolio/tracker.py — Phase 1: the equity / P&L accounting core.

Why this file exists
--------------------
Every metric in Phases 2–5 (Sharpe, VaR, attribution, optimiser inputs) is
computed from *one* thing: a trustworthy **equity curve**. Today the dashboard
shows live positions from the in-process ``PositionTracker``, which has no
history and no charge handling — so there is nothing to compute a return series
from. This module fixes that.

What it does
------------
1. **Cost basis** — weighted-average price per symbol, updated on every fill.
   Selling realises P&L against the *average* cost of the open lot, which is the
   convention Indian brokers (Zerodha/Upstox/etc.) use in the contract note, so
   the engine's numbers reconcile with the broker's.
2. **Charges** — a fill is not free. We apply the same India delivery model the
   portfolio engine uses for *planning* (``optim.cost_aware``) so realised P&L
   is net of STT / brokerage / GST / stamp / slippage. Without this, a
   high-turnover strategy looks profitable in the ledger and loses money live.
3. **Day snapshots** — end-of-day we freeze (date, gross exposure, cash, fees,
   realised, unrealised, NAV) and append to ``data/portfolio_nav/YYYY-MM.jsonl``.
   That file *is* the equity curve; Phases 2–5 read it and never recompute.
4. **Returns** — ``daily_returns()`` converts the NAV path into a return series,
   the input every risk/performance metric needs.

Design rules (same as the rest of the package)
----------------------------------------------
* **Pure core, best-effort I/O.** ``EquityTracker`` does no disk I/O. The
  ``store_nav()`` / ``read_nav()`` helpers at the bottom are the only functions
  that touch the filesystem, and they never raise (a bookkeeping miss must
  never abort a trading run — same rule as ``core/ledger.py``).
* **No OpenAlgo imports.** Positions arrive from the caller (dashboard tracker
  or the broker's ``positionbook`` via the provider layer).
* **IST day boundary.** Indian markets close 15:30 IST; "a day" here means the
  IST trading date, not UTC. Using UTC would split one session across two rows
  every evening.

Worked example (bear with the arithmetic — it shows *both* legs pay charges)
---------------------------------------------------------------------------
>>> t = EquityTracker(starting_cash=1_000_000.0)
>>> t.on_fill("NIFTY", "NSE_INDEX", "BUY", 10, 22000.0, charges_bps=12.0)   # fees 264.0
>>> t.on_fill("NIFTY", "NSE_INDEX", "SELL", 10, 22500.0, charges_bps=12.0)  # fees 270.0
>>> round(t.total_fees, 2)            # 220000*12bps + 225000*12bps
534.0
>>> round(t.realized_pnl, 2)          # gross (22500-22000)*10, before fees
5000.0
>>> snap = t.snapshot({"NIFTY": 22500.0})
>>> round(snap["nav"] - t.starting_cash, 2)   # 5000 gross - 534 fees
4466.0

The lesson this example is here to make: **charges are per side**. A round trip
pays them twice, so the plausible-looking "+5000" is really "+4466" — 10.7% of
the gross edge is gone. Any strategy whose edge is a few bps per trade is
underwater once this is applied.

Note on the cost model
----------------------
``charges_bps`` is *per side*, expressed in basis points of traded notional.
Pass it from ``optim.cost_aware.estimate_cost_bps()`` to stay consistent with
the planner, or hard-code your broker's schedule. Default 12 bps ≈ India
delivery average (STT 10 bps + brokerage/GST/stamp ≈ 2 bps) excluding slippage.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.results import safe_float

logger = logging.getLogger(__name__)

#: IST is UTC+5:30 and has no DST — a fixed offset is correct year-round.
IST = timezone(timedelta(hours=5, minutes=30))

#: Per-side charges in bps of traded notional (India delivery baseline).
#: STT 10 bps + brokerage ~3 + GST/stamp/SEBI ~1 ≈ 14 bps; 12 is a fair default
#: for a discount broker and is *deliberately conservative* (see module docstring).
DEFAULT_CHARGES_BPS: float = 12.0

# <repo>/data/portfolio_nav/YYYY-MM.jsonl
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_NAV_DIR = _REPO_ROOT / "data" / "portfolio_nav"

__all__ = [
    "EquityTracker", "Lot", "Fill", "ist_date", "nav_path",
    "store_nav", "read_nav", "daily_returns", "periods_per_year",
    "DEFAULT_NAV_DIR", "DEFAULT_CHARGES_BPS",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def ist_date(ts: Optional[float] = None) -> str:
    """The IST (Asia/Kolkata) trading date for ``ts`` as ``YYYY-MM-DD``.

    Indian markets are 09:15–15:30 IST, so the session date must be IST, not
    UTC. At 00:30 IST the UTC date is still the previous day — booking P&L
    against the UTC date would put late-evening trades on the wrong day.
    """
    stamp = float(ts if ts is not None else time.time())
    return datetime.fromtimestamp(stamp, tz=IST).strftime("%Y-%m-%d")


def nav_path(month: Optional[str] = None, *,
             directory: Optional[Path] = None) -> Path:
    """Path of the NAV file for ``YYYY-MM`` (defaults to the current IST month)."""
    m = month or ist_date()[:7]
    base = Path(directory) if directory else DEFAULT_NAV_DIR
    return base / f"{m}.jsonl"


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

@dataclass
class Lot:
    """An open position in one symbol, tracked at weighted-average cost."""

    symbol: str
    exchange: str
    qty: int = 0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    fees: float = 0.0

    def market_value(self, mark: Optional[float]) -> float:
        """Signed market value at ``mark`` (falls back to cost when unmarked)."""
        px = safe_float(mark)
        if px is None:
            px = self.avg_price
        return self.qty * px

    def unrealized_pnl(self, mark: Optional[float]) -> float:
        px = safe_float(mark)
        if px is None or self.qty == 0:
            return 0.0
        return (px - self.avg_price) * self.qty

    def to_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "exchange": self.exchange,
                "qty": self.qty, "avg_price": round(self.avg_price, 6),
                "realized_pnl": round(self.realized_pnl, 4),
                "fees": round(self.fees, 4)}


@dataclass
class Fill:
    """One execution. Timestamps are epoch seconds (IST-aware formatting above)."""

    symbol: str
    exchange: str
    side: str            # "BUY" | "SELL"
    qty: int
    price: float
    ts: float = field(default_factory=time.time)
    charges_bps: float = DEFAULT_CHARGES_BPS

    @property
    def notional(self) -> float:
        return abs(self.qty) * float(self.price)

    @property
    def charges(self) -> float:
        """Charges for this fill, in currency units."""
        return self.notional * (float(self.charges_bps) / 10_000.0)


# --------------------------------------------------------------------------- #
# The tracker
# --------------------------------------------------------------------------- #

class EquityTracker:
    """Weighted-average position ledger + NAV/equity-curve builder.

    The tracker is **stateful but pure**: it owns no files, opens no sockets,
    and never raises on bad input (a malformed fill is logged and skipped).
    Callers drive it with fills and marks; it answers with P&L and a NAV.
    """

    def __init__(self, starting_cash: float = 0.0, *,
                 charges_bps: float = DEFAULT_CHARGES_BPS,
                 log: Optional[Any] = None):
        self.starting_cash: float = float(starting_cash)
        self.cash: float = float(starting_cash)
        self.charges_bps: float = float(charges_bps)
        self._lots: Dict[str, Lot] = {}
        self._fills: List[Fill] = []
        self._log = log

    # ---- logging -------------------------------------------------------- #
    def _logmsg(self, msg: str) -> None:
        if callable(self._log):
            self._log(f"[tracker] {msg}")
        else:
            logger.info("[tracker] %s", msg)

    # ---- keys ----------------------------------------------------------- #
    @staticmethod
    def _key(symbol: str, exchange: str) -> str:
        return f"{symbol}|{exchange}"

    def lot(self, symbol: str, exchange: str = "") -> Lot:
        """Return (creating if needed) the lot for a symbol."""
        k = self._key(symbol, exchange)
        if k not in self._lots:
            self._lots[k] = Lot(symbol=symbol, exchange=exchange)
        return self._lots[k]

    # ---- fills ---------------------------------------------------------- #
    def on_fill(self, symbol: str, exchange: str, side: str, qty: int,
                price: float, *, ts: Optional[float] = None,
                charges_bps: Optional[float] = None) -> Optional[Fill]:
        """Apply one fill. Returns the recorded :class:`Fill`, or None if skipped.

        Weighted-average accounting:

        * **BUY**  — average price moves toward the new price::

              avg' = (qty*avg + q*px) / (qty + q)

          Cash falls by notional + charges.
        * **SELL** — realises ``(px - avg) * q`` against the *average* cost of
          the open lot, reduces ``qty``, and leaves ``avg`` unchanged (the
          standard broker convention). Cash rises by notional − charges.
        * **Flip** — selling more than the open lot closes it and opens a short
          at the fill price; that is handled explicitly rather than silently
          producing a negative-cost lot.

        Charges are booked to the lot *and* to cash, so both realised P&L and
        NAV are net of costs.
        """
        q = int(safe_float(qty) or 0)
        px = safe_float(price)
        sd = str(side or "").upper().strip()
        if q <= 0 or px is None or px <= 0 or sd not in ("BUY", "SELL"):
            self._logmsg(f"skipped invalid fill {symbol} {side} {qty}@{price}")
            return None
        if charges_bps is None:
            charges_bps = self.charges_bps

        fill = Fill(symbol=symbol, exchange=exchange, side=sd, qty=q,
                    price=px, ts=float(ts if ts is not None else time.time()),
                    charges_bps=float(charges_bps))
        lot = self.lot(symbol, exchange)

        if sd == "BUY":
            # Opening/closing a short, or adding to a long — both are "add".
            if lot.qty >= 0:
                new_qty = lot.qty + q
                lot.avg_price = ((lot.qty * lot.avg_price) + q * px) / new_qty
                lot.qty = new_qty
            else:
                closing = min(q, abs(lot.qty))
                # Buying back a short realises (avg - px) per share.
                lot.realized_pnl += (lot.avg_price - px) * closing
                lot.qty += q
                if lot.qty > 0:            # flipped long — new cost basis
                    lot.avg_price = px
                elif lot.qty == 0:
                    lot.avg_price = 0.0
            self.cash -= (q * px) + fill.charges
        else:  # SELL
            if lot.qty > 0:
                closing = min(q, lot.qty)
                lot.realized_pnl += (px - lot.avg_price) * closing
                lot.qty -= q
                if lot.qty < 0:            # flipped short — new cost basis
                    lot.avg_price = px
                elif lot.qty == 0:
                    lot.avg_price = 0.0
            else:
                new_qty = abs(lot.qty) + q
                lot.avg_price = ((abs(lot.qty) * lot.avg_price) + q * px) / new_qty
                lot.qty -= q
            self.cash += (q * px) - fill.charges

        lot.fees += fill.charges
        self._fills.append(fill)
        self._logmsg(f"{sd} {q} {symbol}@{px:.2f} charges={fill.charges:.2f} "
                     f"pos={lot.qty} avg={lot.avg_price:.2f} cash={self.cash:,.0f}")
        return fill

    def on_trades(self, trades: Any, *, default_exchange: str = "") -> int:
        """Bulk-apply an iterable of trade-like objects (duck-typed).

        Accepts anything with ``symbol/side/qty/price`` attributes — that covers
        the dashboard's ``TradeRecord`` and the broker's position rows — plus
        plain dicts. Returns the number of fills applied.
        """
        n = 0
        for t in trades or []:
            if isinstance(t, dict):
                g = t.get
            else:
                g = lambda k, _d=None, _t=t: getattr(_t, k, _d)  # noqa: E731
            fill = self.on_fill(
                str(g("symbol", "") or ""),
                str(g("exchange", default_exchange) or default_exchange),
                str(g("side", "") or ""),
                int(safe_float(g("qty")) or 0),
                safe_float(g("price")) or 0.0,
                ts=safe_float(g("ts")),
            )
            n += 1 if fill else 0
        return n

    # ---- valuation ------------------------------------------------------ #
    def exposure(self, marks: Dict[str, float]) -> float:
        """Gross exposure = sum |qty * mark| across open lots."""
        total = 0.0
        for lot in self._lots.values():
            mv = lot.market_value(marks.get(lot.symbol))
            total += abs(mv)
        return total

    def net_exposure(self, marks: Dict[str, float]) -> float:
        """Net exposure = sum qty * mark (signed; shorts subtract)."""
        return sum(lot.market_value(marks.get(lot.symbol))
                   for lot in self._lots.values())

    def unrealized_pnl(self, marks: Dict[str, float]) -> float:
        return sum(lot.unrealized_pnl(marks.get(lot.symbol))
                   for lot in self._lots.values())

    @property
    def realized_pnl(self) -> float:
        return sum(lot.realized_pnl for lot in self._lots.values())

    @property
    def total_fees(self) -> float:
        return sum(lot.fees for lot in self._lots.values())

    def nav(self, marks: Dict[str, float]) -> float:
        """Net asset value = cash + open position value (marked).

        Cash already includes the cost of open positions, so NAV = cash + Σ(qty·mark).
        Note: for a long this counts the position once (cash was debited on entry
        and the mark adds market value), which is the standard broker NAV.
        """
        return self.cash + self.net_exposure(marks)

    # ---- snapshot ------------------------------------------------------- #
    def snapshot(self, marks: Dict[str, float], *,
                 ts: Optional[float] = None,
                 extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Freeze one end-of-day equity row (the raw material of the curve).

        Returns a JSON-serialisable dict; ``nav`` and the P&L fields are NET of
        charges (``total_fees`` is reported separately for transparency).
        """
        stamp = float(ts if ts is not None else time.time())
        upnl = self.unrealized_pnl(marks)
        nav = self.nav(marks)
        row: Dict[str, Any] = {
            "ts": stamp,
            "date": ist_date(stamp),
            "nav": round(nav, 4),
            "cash": round(self.cash, 4),
            "gross_exposure": round(self.exposure(marks), 4),
            "net_exposure": round(self.net_exposure(marks), 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "unrealized_pnl": round(upnl, 4),
            "total_fees": round(self.total_fees, 4),
            "total_pnl": round(self.realized_pnl + upnl, 4),
            "starting_cash": round(self.starting_cash, 4),
            "return_since_start": (round(nav / self.starting_cash - 1.0, 6)
                                   if self.starting_cash else None),
            "n_positions": sum(1 for lot in self._lots.values() if lot.qty != 0),
            "positions": [lot.to_dict() for lot in self._lots.values()
                          if lot.qty != 0],
        }
        if extra:
            row.update(extra)
        return row

    def reset_realized(self) -> Dict[str, float]:
        """Zero each lot's realised P&L (call at a period boundary).

        Returns the realised P&L that was cleared, so the caller can book it
        into a period ledger. Fees are *not* cleared — they are cumulative
        (they are a cost of the whole account, not of one period).
        """
        cleared = {lot.symbol: lot.realized_pnl for lot in self._lots.values()}
        for lot in self._lots.values():
            lot.realized_pnl = 0.0
        return cleared

    # ---- introspection -------------------------------------------------- #
    def positions(self) -> List[Dict[str, Any]]:
        return [lot.to_dict() for lot in self._lots.values() if lot.qty != 0]

    def fills(self, limit: Optional[int] = None) -> List[Fill]:
        return self._fills[-limit:] if limit else list(self._fills)

    def describe(self) -> Dict[str, Any]:
        return {"starting_cash": self.starting_cash, "cash": round(self.cash, 2),
                "open_positions": len(self.positions()),
                "fills": len(self._fills),
                "realized_pnl": round(self.realized_pnl, 2),
                "total_fees": round(self.total_fees, 2),
                "charges_bps": self.charges_bps}


# --------------------------------------------------------------------------- #
# Persistence (the ONLY I/O in this module; best-effort, never raises)
# --------------------------------------------------------------------------- #

def store_nav(row: Dict[str, Any], *, directory: Optional[Path] = None
              ) -> Optional[Path]:
    """Append one NAV snapshot to ``data/portfolio_nav/YYYY-MM.jsonl``.

    Monthly files (not daily) because the equity curve is small and a monthly
    file is far cheaper to read back than 250 one-line files. Never raises:
    a bookkeeping failure must not abort a trading run.
    """
    try:
        month = str(row.get("date") or ist_date())[:7]
        path = nav_path(month, directory=directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return path
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never abort a run
        logger.warning("store_nav failed: %s", exc)
        return None


def read_nav(*, months: int = 12, directory: Optional[Path] = None
             ) -> List[Dict[str, Any]]:
    """Read the NAV history, oldest first, as the most recent ``months`` of DATA.

    Why not "the last N files": because files are monthly but history is not
    necessarily contiguous. If the store holds 2024-07..2026-09 (27 files) and a
    caller asks for ``months=12``, slicing the last 12 FILES would return a set
    spanning 2025-10..2026-09 — which is contiguous and fine — but a *sparse*
    store (gaps from an outage, a re-clone, a selective backfill) would return a
    disjoint set, and ``daily_returns`` would then compute returns *across the
    gaps*. That manufactured a -86% annual return from a +9.65% curve.

    We therefore filter by DATE, not by file count: read the files, then keep
    only rows inside the trailing ``months`` window. Malformed lines are skipped
    so a torn tail write is safe.
    """
    base = Path(directory) if directory else DEFAULT_NAV_DIR
    if not base.is_dir():
        return []
    # Read every file, then window by date. Cheap: the store is one line per
    # session, so even 20 years is a few thousand rows.
    files = sorted(base.glob("*.jsonl"))
    rows: List[Dict[str, Any]] = []
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        rows.append(obj)
        except OSError as exc:
            logger.debug("read_nav failed for %s: %s", path, exc)
    rows.sort(key=lambda r: safe_float(r.get("ts")) or 0.0)
    if not rows or months <= 0:
        return rows
    # Window on the DATE field (ist_date string) so ordering is unambiguous.
    import datetime as _dt
    last_date = str(rows[-1].get("date") or "")[:10]
    if len(last_date) != 10:
        return rows
    try:
        cutoff = (_dt.date.fromisoformat(last_date)
                  - _dt.timedelta(days=int(30.44 * months))).isoformat()
    except ValueError:
        return rows
    return [r for r in rows if str(r.get("date") or "")[:10] >= cutoff]


def daily_returns(nav_rows: List[Dict[str, Any]]) -> List[float]:
    """Convert a NAV path into a RETURN series, oldest first.

    Collapses multiple snapshots on the same IST date to the **last** one
    (end-of-day), then returns ``nav_t / nav_{t-1} - 1`` for each consecutive
    pair present in the store.

    IMPORTANT — the spacing is whatever the store holds, not necessarily daily.
    A live desk writes one row per session, so the series is daily. A *backfilled*
    store may hold one row per **monthly rebalance**, making the series monthly.
    Callers must annualise with :func:`periods_per_year` (below), NOT with a
    hard-coded 252 — annualising a monthly series at 252 overstates volatility by
    sqrt(252/12) = 4.6x, which turns a 10% book into a "46%" book.

    Rows with a non-positive or missing NAV are dropped rather than producing a
    spurious -100% return. When a date carries both a reconstructed row
    (``source='backfill'``) and a live row, the LIVE one wins — reconstructed
    history must never overwrite what actually happened.
    """
    by_day: Dict[str, Tuple[float, bool]] = {}      # date -> (nav, is_live)
    for row in nav_rows or []:
        d = str(row.get("date") or "")[:10]
        nav = safe_float(row.get("nav"))
        if not d or nav is None or nav <= 0:
            continue
        is_live = str(row.get("source") or "").lower() != "backfill"
        prev = by_day.get(d)
        if prev is None:
            by_day[d] = (nav, is_live)
        elif is_live and not prev[1]:
            by_day[d] = (nav, is_live)          # live beats reconstructed
        elif is_live == prev[1]:
            by_day[d] = (nav, is_live)          # same provenance: last wins (EOD)
    days = sorted(by_day)
    out: List[float] = []
    for prev, cur in zip(days, days[1:]):
        a = by_day[prev][0]
        b = by_day[cur][0]
        if a > 0:
            out.append(b / a - 1.0)
    return out


def periods_per_year(nav_rows: List[Dict[str, Any]], *,
                     default: float = 252.0) -> float:
    """Infer the annualisation factor from the AVERAGE spacing of the store.

    This is the difference between a risk report that means something and one
    that is off by 4.6x. A live store is ~1 row per trading day (spacing ~1, so
    the factor is ~252). A backfilled store rebalanced monthly has spacing ~30
    calendar days (factor ~12).

    Derivation: count the observations, measure the calendar span, and scale to
    a year. Deliberately calendar-based (365.25 days) rather than session-based,
    because a sparse store's session count is unknowable.

    Falls back to ``default`` when there are fewer than 3 rows or no usable
    dates, so a caller never divides by zero.
    """
    import datetime as _dt
    ds: List[_dt.date] = []
    for row in nav_rows or []:
        s = str(row.get("date") or "")[:10]
        try:
            ds.append(_dt.date.fromisoformat(s))
        except ValueError:
            continue
    ds = sorted(set(ds))
    if len(ds) < 3:
        return float(default)
    span_days = (ds[-1] - ds[0]).days
    if span_days <= 0:
        return float(default)
    obs_per_year = (len(ds) - 1) * 365.25 / span_days
    if obs_per_year <= 0 or obs_per_year > 366:
        return float(default)
    return round(float(obs_per_year), 4)