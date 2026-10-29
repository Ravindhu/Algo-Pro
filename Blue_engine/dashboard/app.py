"""
app.py - the LiveDashboard orchestrator.

Wires:
  - DataSource     (quotes + screener feed)
  - PovStateMachine (one per watched symbol)
  - PositionTracker (virtual ledger)
  - rich.Layout     (the render tree)

Use from a script (see cli.py) or programmatically:

    from blue_engine.dashboard.app import LiveDashboard, DashboardConfig
    from blue_engine.dashboard.data_source import resolve_watchlist
    cfg = DashboardConfig(watchlist_preset="NIFTY50")
    dash = LiveDashboard(cfg, history_fn=client.history, quotes_fn=client.quotes)
    dash.run()
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .data_source import (
    AlertEvent,
    DataSource,
    MatchEvent,
    PriceTick,
    TradeEvent,
    resolve_watchlist,
)
from .position_tracker import PositionTracker, TradeRecord, VirtualPosition
from .pov_state_machine import PovConfig, PovState, PovStateMachine
from .render import (
    build_layout,
    render_alerts_panel,
    render_detail_panel,
    render_status_bar,
    render_universe_table,
)

logger = logging.getLogger(__name__)


HistoryFn = Callable[..., Dict[str, Any]]
QuotesFn = Callable[..., Dict[str, Any]]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class DashboardConfig:
    """Tuning knobs for the dashboard.

    Sensible defaults are tuned for liquid NSE large caps. Adjust
    the bands to your risk appetite and instrument volatility.
    """

    watchlist_preset: Optional[str] = "NIFTY50"
    extra_symbols: List[Dict[str, str]] = field(default_factory=list)
    exchange_filter: Optional[str] = None
    max_symbols: int = 25

    quote_refresh_seconds: float = 3.0
    scan_refresh_seconds: float = 30.0
    lookback_days: int = 7

    # Starting cash for the virtual ledger.
    starting_cash: float = 100000.0

    # POV tuning.
    mode: str = "short_term"  # "investing" | "short_term"
    qty_per_fire: int = 100
    risk_pct: float = 0.01
    touch_buffer_pct: float = 0.05
    ceiling_pct_above_trigger: float = 1.0
    floor_pct_below_trigger: float = 1.0
    stop_loss_pct_below_trigger: float = 2.0
    pov_min_pct: float = 5.0
    pov_max_pct: float = 25.0

    # Display
    max_universe_rows: int = 25
    max_alerts_lines: int = 12

    def pov_config(self) -> PovConfig:
        return PovConfig(
            mode=self.mode,
            qty_per_fire=self.qty_per_fire,
            risk_pct=self.risk_pct,
            touch_buffer_pct=self.touch_buffer_pct,
            ceiling_pct_above_trigger=self.ceiling_pct_above_trigger,
            floor_pct_below_trigger=self.floor_pct_below_trigger,
            stop_loss_pct_below_trigger=self.stop_loss_pct_below_trigger,
            pov_min_pct=self.pov_min_pct,
            pov_max_pct=self.pov_max_pct,
        )


# ---------------------------------------------------------------------------
# LiveDashboard
# ---------------------------------------------------------------------------


class LiveDashboard:
    """The orchestrator. Wires data source -> state machines -> render."""

    def __init__(
        self,
        config: DashboardConfig,
        *,
        history_fn: HistoryFn,
        quotes_fn: QuotesFn,
        client: Any = None,
        max_ticks_to_process: Optional[int] = None,
    ) -> None:
        self.config = config
        self.history_fn = history_fn
        self.quotes_fn = quotes_fn
        #: The OpenAlgo SDK client, when one is available.
        #:
        #: Only the *mode read* uses this (see core/mode.read_mode): the
        #: terminal must show LIVE vs ANALYZER, and only the SDK can answer
        #: that. It is optional because the terminal is designed to run
        #: read-only without any broker connection -- in that case the theme
        #: intentionally falls back to "unknown" and says so on the banner.
        #:
        #: Note the asymmetry: `history_fn`/`quotes_fn` may be paper stubs,
        #: but a paper stub cannot report the mode, so passing a real client
        #: here is what makes the banner truthful even in paper mode.
        self.client = client
        self._max_ticks_to_process = max_ticks_to_process

        wl = resolve_watchlist(
            preset=config.watchlist_preset,
            extra_symbols=config.extra_symbols,
            exchange_filter=config.exchange_filter,
        )
        if config.max_symbols and len(wl) > config.max_symbols:
            wl = wl[: config.max_symbols]

        pov_cfg = config.pov_config()
        self._machines: Dict[str, PovStateMachine] = {}
        for entry in wl:
            key = f"{entry['symbol']}|{entry['exchange']}"
            self._machines[key] = PovStateMachine(
                symbol=entry["symbol"],
                exchange=entry["exchange"],
                config=pov_cfg,
            )

        self._positions = PositionTracker()
        self._cash = float(config.starting_cash)
        self._candles: Dict[str, List[Dict[str, float]]] = {}

        self._data = DataSource(
            history_fn=history_fn,
            quotes_fn=quotes_fn,
            watchlist=wl,
            quote_refresh_seconds=config.quote_refresh_seconds,
            scan_refresh_seconds=config.scan_refresh_seconds,
            lookback_days=config.lookback_days,
        )
        self._connected = True
        self._last_quote_at: Optional[float] = None
        self._tasks: List[asyncio.Task] = []
        self._stopped = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stop(self) -> None:
        self._stopped = True
        self._data.stop()
        for t in self._tasks:
            try:
                t.cancel()
            except Exception:
                pass

    async def run(self) -> None:
        """Run forever (until stop())."""
        # Try to use rich's Live context for a nice refresh.
        from rich.console import Console
        from rich.live import Live

        console = Console()
        layout = build_layout()
        self._last_render = layout
        self._update_layout(layout)

        async def _pump():
            self._tasks.append(asyncio.create_task(self._data.run()))
            try:
                while not self._stopped:
                    await self._process_one_event_with_timeout(0.1)
                    self._update_layout(layout)
            finally:
                self.stop()

        with Live(layout, console=console, refresh_per_second=10, screen=False) as live:
            try:
                while not self._stopped:
                    await self._process_one_event_with_timeout(0.1)
                    self._update_layout(layout)
                    live.refresh()
            except asyncio.CancelledError:
                pass
            finally:
                self.stop()

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    async def _process_one_event_with_timeout(self, timeout: float) -> bool:
        try:
            kind, payload = await asyncio.wait_for(self._data.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        try:
            self._on_event(kind, payload)
        except Exception as exc:
            logger.exception("event handler error: %s", exc)
        return True

    def _on_event(self, kind: str, payload: Any) -> None:
        if kind == "price":
            self._on_price(payload)
        elif kind == "match":
            self._on_match(payload)
        elif kind == "trade":
            self._on_trade(payload)
        elif kind == "alert":
            # Already pushed by DataSource into state.last_alerts.
            pass
        elif kind == "tick":
            # Heartbeat; the layout re-render reads the latest state.
            pass
        else:
            logger.debug("unknown event kind: %r", kind)

    def _on_price(self, tick: PriceTick) -> None:
        self._last_quote_at = tick.ts
        key = f"{tick.symbol}|{tick.exchange}"
        machine = self._machines.get(key)
        if machine is not None:
            result = machine.tick(ltp=tick.ltp, ts=tick.ts, available_cash=self._cash)
            if result.intent in ("BUY", "SELL") and result.qty > 0:
                # Apply intent to virtual ledger.
                pos = self._positions.get(tick.symbol, tick.exchange)
                if result.intent == "BUY" and machine.action == "BUY":
                    cost = result.qty * tick.ltp
                    if cost <= self._cash:
                        self._cash -= cost
                        prev_cost = (pos.avg_price or 0.0) * pos.qty
                        new_qty = pos.qty + result.qty
                        pos.qty = new_qty
                        pos.avg_price = ((prev_cost + cost) / new_qty) if new_qty else None
                        self._positions.record_trade(
                            TradeRecord(
                                symbol=tick.symbol,
                                exchange=tick.exchange,
                                side="BUY",
                                qty=result.qty,
                                price=tick.ltp,
                                ts=tick.ts,
                                state=result.state.value,
                                pov_pct=result.pov_pct,
                            )
                        )
                        self._data._push("trade", TradeEvent(
                            symbol=tick.symbol, exchange=tick.exchange,
                            side="BUY", qty=result.qty, price=tick.ltp,
                            state=result.state.value, pov_pct=result.pov_pct, ts=tick.ts,
                        ))
                elif result.intent == "SELL" and pos.qty > 0:
                    proceeds = result.qty * tick.ltp
                    self._cash += proceeds
                    avg = pos.avg_price or 0.0
                    pnl = (tick.ltp - avg) * result.qty
                    pos.realized_pnl += pnl
                    pos.qty = max(0, pos.qty - result.qty)
                    if pos.qty == 0:
                        pos.avg_price = None
                    self._positions.record_trade(
                        TradeRecord(
                            symbol=tick.symbol,
                            exchange=tick.exchange,
                            side="SELL",
                            qty=result.qty,
                            price=tick.ltp,
                            ts=tick.ts,
                            state=result.state.value,
                            pov_pct=result.pov_pct,
                        )
                    )
                    self._data._push("trade", TradeEvent(
                        symbol=tick.symbol, exchange=tick.exchange,
                        side="SELL", qty=result.qty, price=tick.ltp,
                        state=result.state.value, pov_pct=result.pov_pct, ts=tick.ts,
                    ))

    def _on_match(self, ev: MatchEvent) -> None:
        key = f"{ev.symbol}|{ev.exchange}"
        machine = self._machines.get(key)
        if machine is None:
            return
        if machine.trigger_level is None:
            machine.set_match(ev.pattern, ev.trigger_level)
        # Pre-fetch HTF candles for the detail panel.
        self._prefetch_candles(ev.symbol, ev.exchange)
        # Also push the match into the alerts feed.
        self._data.state.last_alerts.append(ev)
        if len(self._data.state.last_alerts) > 200:
            self._data.state.last_alerts = self._data.state.last_alerts[-200:]
        # The data_source already pushed the event to its queue; we
        # also push to the alerts panel here so the next render shows
        # the match.
        self._data._push("alert", AlertEvent(severity="info", message=f"match: {ev.symbol} {ev.pattern} @ {ev.trigger_level:.2f}", ts=ev.ts))

    def _on_trade(self, ev: TradeEvent) -> None:
        # Trade events from the state machine -> append to alerts feed.
        self._data.state.last_alerts.append(ev)
        if len(self._data.state.last_alerts) > 200:
            self._data.state.last_alerts = self._data.state.last_alerts[-200:]

    def _prefetch_candles(self, symbol: str, exchange: str) -> None:
        key = f"{symbol}|{exchange}"
        if key in self._candles:
            return
        try:
            from datetime import datetime, timedelta
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=self.config.lookback_days)).strftime("%Y-%m-%d")
            # AUDIT (2026-10-29, LIVE-12): "15minute" is rejected by the API
            # (HTTP 400); the vocabulary is "15m". This call silently returned
            # nothing, so the terminal chart stayed empty.
            response = self.history_fn(
                symbol=symbol, exchange=exchange, interval="15m",
                start_date=start, end_date=end,
            )
            if not isinstance(response, dict) or response.get("status") != "success":
                return
            data = response.get("data")
            if hasattr(data, "iloc"):
                rows = data.to_dict(orient="records")
            elif isinstance(data, list):
                rows = data
            else:
                return
            # Keep last 30 candles for the detail sparkline.
            self._candles[key] = [self._candle_to_dict(r) for r in rows[-30:]]
        except Exception as exc:
            logger.debug("candle prefetch failed for %s/%s: %s", symbol, exchange, exc)

    def _candle_to_dict(self, candle: Any) -> Dict[str, float]:
        try:
            if hasattr(candle, "to_dict"):
                candle = candle.to_dict()
            return {
                "open": float(candle.get("open", 0)),
                "high": float(candle.get("high", 0)),
                "low": float(candle.get("low", 0)),
                "close": float(candle.get("close", 0)),
            }
        except Exception:
            return {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0}

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def _build_universe_row(self, key: str, tick: Optional[PriceTick]) -> Dict[str, Any]:
        symbol, exchange = key.split("|", 1)
        machine = self._machines.get(key)
        pos = self._positions.get(symbol, exchange)
        row: Dict[str, Any] = {
            "symbol": symbol,
            "exchange": exchange,
        }
        if tick is not None:
            row["ltp"] = tick.ltp
            row["change_pct"] = tick.change_pct
            row["bid"] = tick.bid
            row["ask"] = tick.ask
            row["volume"] = tick.volume
            row["high"] = tick.high
            row["low"] = tick.low
            row["open"] = tick.open
            row["prev_close"] = tick.prev_close
            # Spread pct = (ask - bid) / mid * 100
            if tick.bid > 0 and tick.ask > 0:
                mid = (tick.bid + tick.ask) / 2.0
                if mid > 0:
                    row["spread_pct"] = (tick.ask - tick.bid) / mid * 100.0
        else:
            row["ltp"] = 0
        if machine is not None:
            row["state"] = machine.state.value
            row["pattern"] = machine.pattern or "-"
            row["trigger_level"] = machine.trigger_level
            if machine.trigger_level and isinstance(row.get("ltp"), (int, float)) and row["ltp"] > 0:
                row["distance_pct"] = ((row["ltp"] - machine.trigger_level) / machine.trigger_level) * 100.0
        else:
            row["state"] = "WATCHING"
            row["pattern"] = "-"
        row["position"] = pos.qty
        row["avg_price"] = pos.avg_price
        # Unrealized P&L
        if pos.qty > 0 and pos.avg_price and isinstance(row.get("ltp"), (int, float)) and row["ltp"] > 0:
            row["unrealized_pnl"] = (row["ltp"] - pos.avg_price) * pos.qty
        else:
            row["unrealized_pnl"] = 0
        return row

    def _build_universe_rows(self) -> List[Dict[str, Any]]:
        rows = []
        for key, machine in self._machines.items():
            tick = self._data.state.last_quotes.get(key)
            row = self._build_universe_row(key, tick)
            rows.append(row)
        return rows

    def _selected_row(self) -> Optional[Dict[str, Any]]:
        if not self._machines:
            return None
        # Pick the symbol in ACCUMULATING or MATCHED state; fall back
        # to the first one in the watchlist.
        for key, machine in self._machines.items():
            if machine.state in (PovState.ACCUMULATING, PovState.MATCHED, PovState.SLOW):
                tick = self._data.state.last_quotes.get(key)
                return self._build_universe_row(key, tick)
        first_key = next(iter(self._machines))
        tick = self._data.state.last_quotes.get(first_key)
        return self._build_universe_row(first_key, tick)

    def _update_layout(self, layout) -> None:
        rows = self._build_universe_rows()
        layout["universe"].update(
            render_universe_table(rows, max_rows=self.config.max_universe_rows)
        )
        selected = self._selected_row()
        if selected is not None:
            key = f"{selected['symbol']}|{selected['exchange']}"
            candles = self._candles.get(key, [])
        else:
            candles = []
        layout["detail"].update(render_detail_panel(selected, candles))
        layout["alerts"].update(render_alerts_panel(self._data.state.last_alerts, max_lines=self.config.max_alerts_lines))
        realized = self._positions.total_realized_pnl()
        unrealized = sum(
            (row.get("unrealized_pnl", 0) or 0)
            for row in rows
        )
        layout["status"].update(
            render_status_bar(
                connected=self._connected,
                quote_refresh=self.config.quote_refresh_seconds,
                scan_refresh=self.config.scan_refresh_seconds,
                n_symbols=len(self._machines),
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                last_quote_at=self._last_quote_at,
                cash=self._cash,
            )
        )

    # ------------------------------------------------------------------
    # Convenience for the CLI
    # ------------------------------------------------------------------

    def add_symbol(self, symbol: str, exchange: str) -> bool:
        key = f"{symbol}|{exchange}"
        if key in self._machines:
            return False
        self._machines[key] = PovStateMachine(symbol=symbol, exchange=exchange, config=self.config.pov_config())
        return True

    def remove_symbol(self, symbol: str, exchange: str) -> bool:
        key = f"{symbol}|{exchange}"
        return self._machines.pop(key, None) is not None