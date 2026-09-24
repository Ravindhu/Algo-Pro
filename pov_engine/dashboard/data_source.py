"""
data_source.py - async event loop for the live dashboard.

Owns:
  - the watchlist (resolved from a preset + extra symbols)
  - a quote poll loop (every quote_refresh_seconds)
  - an OCL match tail (every scan_refresh_seconds, watches the
    OclScreenerMatch table for new rows)
  - a queue the render layer reads from

By design the data source emits **events** ("price update for
INFY at 1480", "new match for TCS at 113"), not raw state. The
:class:`LiveDashboard` is responsible for folding events into
its in-memory state and re-rendering.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# IST offset (matches pov_engine.screener.ocl_screener).
_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now_iso() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


@dataclass
class PriceTick:
    symbol: str
    exchange: str
    ltp: float
    bid: float
    ask: float
    volume: int
    open: float
    high: float
    low: float
    prev_close: float
    change_pct: float
    ts: float


@dataclass
class MatchEvent:
    config_id: int
    config_name: str
    symbol: str
    exchange: str
    pattern: str
    action: str
    trigger_level: float
    ltp: Optional[float]
    touched: bool
    reason: str
    ts: float


@dataclass
class TradeEvent:
    symbol: str
    exchange: str
    side: str
    qty: int
    price: float
    state: str
    pov_pct: float
    ts: float


@dataclass
class AlertEvent:
    severity: str  # "info" | "warn" | "error"
    message: str
    ts: float


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


# Functions the dashboard wires up. These are passed in by the
# LiveDashboard so tests / paper mode can stub them.
HistoryFn = Callable[..., Dict[str, Any]]
QuotesFn = Callable[..., Dict[str, Any]]
SleepFn = Callable[[float], Awaitable[None]]
SleepFnSync = Callable[[float], None]


def resolve_watchlist(
    *,
    preset: Optional[str] = None,
    extra_symbols: Optional[Sequence[Dict[str, str]]] = None,
    exchange_filter: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Return the merged watchlist (preset + extras, deduped)."""
    try:
        from pov_engine.screener.presets import resolve_preset
    except Exception:
        resolve_preset = None  # type: ignore[assignment]

    out: List[Dict[str, str]] = []
    seen = set()
    if preset and resolve_preset:
        for r in resolve_preset(preset):
            sym = str(r.get("symbol", "")).strip().upper()
            exch = str(r.get("exchange", "")).strip().upper()
            if sym and exch and (sym, exch) not in seen:
                if exchange_filter and exch != exchange_filter.upper():
                    continue
                seen.add((sym, exch))
                out.append({"symbol": sym, "exchange": exch})
    if extra_symbols:
        for r in extra_symbols:
            try:
                sym = str(r["symbol"]).strip().upper()
                exch = str(r["exchange"]).strip().upper()
            except (KeyError, TypeError):
                continue
            if sym and exch and (sym, exch) not in seen:
                if exchange_filter and exch != exchange_filter.upper():
                    continue
                seen.add((sym, exch))
                out.append({"symbol": sym, "exchange": exch})
    return out


# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------


@dataclass
class DataSourceState:
    """Snapshot of the latest known state for the dashboard."""

    watchlist: List[Dict[str, str]] = field(default_factory=list)
    last_quotes: Dict[str, PriceTick] = field(default_factory=dict)  # key = "SYM|EXCH"
    last_match_ts: Dict[str, float] = field(default_factory=dict)
    last_alerts: List[AlertEvent] = field(default_factory=list)
    started_at: float = 0.0

    def key(self, symbol: str, exchange: str) -> str:
        return f"{symbol}|{exchange}"


class DataSource:
    """Async event source: quotes + screener feed -> events.

    The :class:`LiveDashboard` reads from ``self.queue`` in its
    render loop. Each item is a tuple ``(kind, payload)`` where
    ``kind`` is one of:
      "price"     -> PriceTick
      "match"     -> MatchEvent
      "trade"     -> TradeEvent
      "alert"     -> AlertEvent
      "tick"      -> str("quote"|"match") indicating a poll completed
    """

    def __init__(
        self,
        *,
        history_fn: HistoryFn,
        quotes_fn: QuotesFn,
        watchlist: Sequence[Dict[str, str]],
        quote_refresh_seconds: float = 3.0,
        scan_refresh_seconds: float = 30.0,
        lookback_days: int = 7,
        sleep_fn: Optional[SleepFn] = None,
        match_poll_fn: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    ) -> None:
        self.history_fn = history_fn
        self.quotes_fn = quotes_fn
        self.watchlist = list(watchlist)
        self.quote_refresh_seconds = max(0.5, float(quote_refresh_seconds))
        self.scan_refresh_seconds = max(2.0, float(scan_refresh_seconds))
        self.lookback_days = int(lookback_days)
        self._sleep = sleep_fn or asyncio.sleep
        self._match_poll_fn = match_poll_fn
        self.queue: asyncio.Queue = asyncio.Queue()
        self.state = DataSourceState(watchlist=list(self.watchlist))
        self.state.started_at = time.time()
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _alert(self, severity: str, message: str) -> None:
        ev = AlertEvent(severity=severity, message=message, ts=time.time())
        self.state.last_alerts.append(ev)
        # Keep the in-memory log bounded.
        if len(self.state.last_alerts) > 200:
            self.state.last_alerts = self.state.last_alerts[-200:]
        self.queue.put_nowait(("alert", ev))

    def _push(self, kind: str, payload: Any) -> None:
        self.queue.put_nowait((kind, payload))

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the data source forever (until stop() is called)."""
        self._alert("info", f"dashboard started: {len(self.watchlist)} symbols")
        quote_task = asyncio.create_task(self._quote_loop())
        match_task = asyncio.create_task(self._match_loop())
        try:
            await asyncio.gather(quote_task, match_task)
        except asyncio.CancelledError:
            pass
        finally:
            quote_task.cancel()
            match_task.cancel()

    async def _quote_loop(self) -> None:
        while not self._stop:
            await self._poll_quotes_once()
            self._push("tick", "quote")
            await self._sleep(self.quote_refresh_seconds)

    async def _match_loop(self) -> None:
        while not self._stop:
            await self._poll_matches_once()
            self._push("tick", "match")
            await self._sleep(self.scan_refresh_seconds)

    async def _poll_quotes_once(self) -> None:
        for entry in self.watchlist:
            symbol = entry["symbol"]
            exchange = entry["exchange"]
            try:
                response = self.quotes_fn(symbol=symbol, exchange=exchange)
            except Exception as exc:
                self._alert("warn", f"quote failed for {symbol}/{exchange}: {exc}")
                continue
            if not isinstance(response, dict) or response.get("status") != "success":
                msg = response.get("message", "unknown") if isinstance(response, dict) else response
                self._alert("warn", f"quote error for {symbol}/{exchange}: {msg}")
                continue
            data = response.get("data") or {}
            tick = self._parse_quote(symbol, exchange, data)
            if tick is None:
                continue
            self.state.last_quotes[self.state.key(symbol, exchange)] = tick
            self._push("price", tick)

    def _parse_quote(self, symbol: str, exchange: str, data: Dict[str, Any]) -> Optional[PriceTick]:
        try:
            ohlc = data.get("ohlc") or {}
            ltp = float(data.get("ltp") or ohlc.get("close") or 0)
            if ltp <= 0:
                return None
            prev_close = float(ohlc.get("close") or 0)
            change_pct = ((ltp - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
            return PriceTick(
                symbol=symbol,
                exchange=exchange,
                ltp=ltp,
                bid=float(data.get("bid") or 0),
                ask=float(data.get("ask") or 0),
                volume=int(data.get("volume") or 0),
                open=float(ohlc.get("open") or 0),
                high=float(ohlc.get("high") or 0),
                low=float(ohlc.get("low") or 0),
                prev_close=prev_close,
                change_pct=change_pct,
                ts=time.time(),
            )
        except (TypeError, ValueError):
            return None

    async def _poll_matches_once(self) -> None:
        """Tail the OclScreenerMatch table for new rows.

        Defaults to a DB-backed poll if ``match_poll_fn`` is not set.
        The poll function returns a list of dicts with the shape of
        OclScreenerMatch.to_dict(), keyed by (symbol, exchange) so
        duplicates can be skipped via the per-key timestamp.
        """
        if self._match_poll_fn is None:
            self._match_poll_fn = self._default_match_poll
        try:
            rows = await asyncio.to_thread(self._match_poll_fn)
        except Exception as exc:
            self._alert("warn", f"match poll failed: {exc}")
            return
        for row in rows:
            try:
                symbol = str(row.get("symbol", "")).upper()
                exchange = str(row.get("exchange", "")).upper()
                if not symbol or not exchange:
                    continue
                key = f"{symbol}|{exchange}"
                ts = float(row.get("matched_at_ts") or 0)
                if not ts:
                    # row has matched_at as ISO string
                    iso = row.get("matched_at")
                    if iso:
                        try:
                            from datetime import datetime
                            ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
                        except Exception:
                            ts = time.time()
                    else:
                        ts = time.time()
                last_seen = self.state.last_match_ts.get(key, 0.0)
                if ts <= last_seen:
                    continue
                self.state.last_match_ts[key] = ts
                ev = MatchEvent(
                    config_id=int(row.get("config_id") or 0),
                    config_name=str(row.get("config_name") or "OCL Screener"),
                    symbol=symbol,
                    exchange=exchange,
                    pattern=str(row.get("pattern") or "RRG"),
                    action=str(row.get("action") or "BUY"),
                    trigger_level=float(row.get("trigger_level") or 0),
                    ltp=float(row["ltp"]) if row.get("ltp") is not None else None,
                    touched=bool(row.get("touched", False)),
                    reason=str(row.get("reason") or ""),
                    ts=ts,
                )
                self._push("match", ev)
            except Exception as exc:
                logger.debug("match event parse error: %s", exc)

    def _default_match_poll(self) -> List[Dict[str, Any]]:
        """Read the last 100 OclScreenerMatch rows from the DB.

        Imports are deferred to keep the module import-time cheap.
        """
        try:
            from database.ocl_screener_db import get_recent_matches
        except Exception as exc:
            logger.debug("match_poll import failed: %s", exc)
            return []
        try:
            from database.ocl_screener_db import OclScreenerMatch
        except Exception:
            OclScreenerMatch = None  # type: ignore[assignment]
        out: List[Dict[str, Any]] = []
        # Query every config the user owns. The dashboard is per-user,
        # so we look up the active configs and tail their matches.
        try:
            from database.ocl_screener_db import get_active_configs
        except Exception:
            get_active_configs = None  # type: ignore[assignment]
        if get_active_configs is None:
            return []
        try:
            configs = get_active_configs()
        except Exception:
            return []
        for cfg in configs:
            try:
                rows = get_recent_matches(cfg.id, limit=50)
            except Exception:
                continue
            for m in rows:
                out.append({
                    "config_id": cfg.id,
                    "config_name": cfg.name,
                    "symbol": m.symbol,
                    "exchange": m.exchange,
                    "pattern": m.pattern,
                    "action": m.action,
                    "trigger_level": m.trigger_level,
                    "ltp": m.ltp,
                    "touched": m.touched,
                    "reason": m.reason,
                    "matched_at": m.matched_at.isoformat() if m.matched_at else None,
                })
        return out