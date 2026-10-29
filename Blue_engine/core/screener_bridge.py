"""screener_bridge.py - Screener list -> OCL strategy -> POV execution.

=======================================================================
The missing multi-symbol handoff. Answers the operator's question:

    screener result (stock list)
        -> one OCLKeylevelSignal PER SYMBOL (entry conditions checked)
        -> on a fire: intent (action/qty/stop_loss/target/exit_mode)
        -> RiskGate pre-trade checks
        -> POV slicer children -> broker

Design decisions
----------------
* In-process threads (no subprocesses): each symbol gets its own
  OCLKeylevelSignal instance; a fired intent is executed SEQUENTIALLY
  by the POV slicer under a lock so total exposure stays bounded.
* Exchange is per symbol. ``symbols`` accepts ``"INFY"`` (uses the run
  default ``exchange``) or ``("500319", "BSE")`` pairs, so one screen
  holding both NSE and BSE-only names runs without an override.
* Capital protection first: ``max_active_symbols`` caps how many symbols
  may be mid-setup at once; ``max_concurrent_executions`` caps how many
  slicer executions can run concurrently (default 1 = serialized).
* Fail-closed: a symbol whose signal raises is disabled (recorded), not
  silently retried forever; a broker failure aborts that symbol's
  execution and is logged for the dashboard error monitor.
* Safety: respects the durable kill switch; does not trade outside
  market hours; the OpenAlgo mode (LIVE/PAPER) is read once and logged
  - the mode itself is controlled in the OpenAlgo UI (single source of
  truth).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from blue_engine.alpha.Statergy.ocl_keylevel import OCLKeylevelSignal
from blue_engine.core.client import init_client
from blue_engine.core.time_utils import ist_now

#: A screen's tradable entry: bare symbol (run-default exchange) or a
#: ``(symbol, exchange)`` pair.
SymbolSpec = Union[str, Tuple[str, str]]


def normalize_symbols(specs: Optional[Sequence[SymbolSpec]],
                      default_exchange: str) -> List[Tuple[str, str]]:
    """Return de-duplicated ``[(SYMBOL, EXCHANGE)]`` in input order.

    ``exchange`` may be ``None`` (e.g. a blank CSV cell) in which case the
    run default is used. Order is preserved because the screener's ranking
    decides which names fill the ``max_active_symbols`` slots.
    """
    out: List[Tuple[str, str]] = []
    seen = set()
    for spec in specs or []:
        if isinstance(spec, (tuple, list)):
            sym = str(spec[0] if len(spec) > 0 else "").strip().upper()
            ex = str(spec[1] if len(spec) > 1 and spec[1] else default_exchange)
        else:
            sym, ex = str(spec).strip().upper(), default_exchange
        sym, ex = sym.strip().upper(), ex.strip().upper()
        if not sym or (sym, ex) in seen:
            continue
        seen.add((sym, ex))
        out.append((sym, ex))
    return out


class SymbolWorker(threading.Thread):
    """One OCL signal per symbol, polled until it fires or is disabled."""

    def __init__(self, symbol: str, exchange: str, signal: OCLKeylevelSignal,
                 bridge: "ScreenerBridge", poll_seconds: float = 5.0):
        super().__init__(name=f"ocl-{symbol}-{exchange}", daemon=True)
        self.symbol = symbol
        self.exchange = exchange
        self.key = f"{symbol}|{exchange}"
        self.signal = signal
        self.bridge = bridge
        self.poll_seconds = poll_seconds
        self._stop_event = threading.Event()

    def run(self) -> None:
        self.bridge.log(f"[WORKER] {self.symbol}/{self.exchange} start "
                        f"(poll={self.poll_seconds}s)")
        while not self._stop_event.is_set():
            if self.bridge.kill_tripped():
                self.bridge.log(f"[WORKER] {self.key} kill switch "
                                f"tripped -> stopping")
                return
            try:
                intent = self.signal.detect()
            except Exception as exc:            # noqa: BLE001
                self.bridge.log(f"[WORKER] {self.key} signal error: "
                                f"{exc!r}")
                self.bridge.disable_symbol(self.key, f"signal error: {exc}")
                return
            if intent is not None:
                is_exit = bool((intent.get("_meta") or {}).get("position_exit"))
                self.bridge.log(
                    f"[WORKER] {self.symbol} {'EXIT' if is_exit else 'ENTRY'} "
                    "intent -> executing"
                )
                result = self.bridge.execute_intent(
                    self.symbol, self.exchange, intent)
                if is_exit:
                    return
                if result is None or not result.get("filled", 0):
                    # Entry rejected/unfilled; reset signal state and resume
                    # scanning, rather than supervise a nonexistent position.
                    self.signal._position_open = False
                    self.signal._fired = False
                elif self.bridge.wait_for_exit:
                    actual_filled = int(result.get("filled", 0))
                    self.signal._position_quantity = actual_filled
                    # Keep the signal live to supervise the filled exposure.
                    self.bridge.log(
                        f"[WORKER] {self.symbol} entry handled; supervising "
                        f"filled quantity={actual_filled}"
                    )
                else:
                    return
            self._stop_event.wait(self.poll_seconds)
        self.bridge.log(f"[WORKER] {self.symbol} stopped")

    def stop(self) -> None:
        self._stop_event.set()


class ScreenerBridge:
    """Run OCL on every screener-selected symbol; execute fires via POV."""

    def __init__(self, *, api_key: Optional[str] = None,
                 host: Optional[str] = None, ws_url: Optional[str] = None,
                 exchange: str = "NSE",
                 symbols: Optional[Sequence[SymbolSpec]] = None,
                 strategy_params: Optional[Dict[str, Any]] = None,
                 pov_params: Optional[Dict[str, Any]] = None,
                 max_active_symbols: int = 5,
                 max_concurrent_executions: int = 1,
                 poll_seconds: float = 5.0,
                 risk_config: Optional[Dict[str, Any]] = None,
                 wait_for_exit: bool = True,
                 exit_config: Optional[Dict[str, Any]] = None,
                 log=None):
        self.exchange = exchange.upper()
        self.symbols: List[Tuple[str, str]] = normalize_symbols(
            symbols, self.exchange)
        self.strategy_params = dict(strategy_params or {})
        self.pov_params = dict(pov_params or {})
        self.risk_config = dict(risk_config or {})
        self.exit_config = (None if exit_config is None else dict(exit_config))
        self._protection_failed = False
        self.wait_for_exit = bool(wait_for_exit)
        self.max_active_symbols = max_active_symbols
        self._exec_lock = threading.Semaphore(
            max(1, max_concurrent_executions))
        self._active: Dict[str, SymbolWorker] = {}
        self._disabled: Dict[str, str] = {}
        self._executed: List[Dict[str, Any]] = []
        self._open_positions: Dict[str, int] = {}
        self._log = log or (lambda msg: print(
            f"[BRIDGE {datetime.now().strftime('%H:%M:%S')}] {msg}"))

        self.client = init_client(api_key=api_key, host=host, ws_url=ws_url)
        self.poll_seconds = poll_seconds

    def kill_tripped(self) -> bool:
        """Durable kill-switch check (single source: optim_policy).

        Uses the NON-LATCHING ``peek_kill_switch``: this is a read-only
        polling bridge, and hydrating the process switch here would latch a
        persisted halt into a process that never placed an order.
        """
        from blue_engine.portfolio.optim_policy import peek_kill_switch
        return bool(peek_kill_switch().get("tripped"))

    # ------------------------------------------------------------------ #
    def log(self, msg: str) -> None:
        self._log(msg)

    # ------------------------------------------------------------------ #
    def _live_mode(self) -> bool:
        """True unless OpenAlgo is verifiably in analyze (paper) mode.

        Mirrors ``core.runner``: an unreadable mode is treated as LIVE so the
        risk acknowledgement fails closed instead of assuming orders are
        harmless.
        """
        from blue_engine.core.mode import read_mode
        try:
            return str(read_mode(self.client)).strip().lower() != "analyze"
        except Exception as exc:  # noqa: BLE001 - unverifiable -> live
            self.log(f"[RISK] mode unreadable ({exc!r}); treating run as LIVE")
            return True

    def _risk_view(self) -> Dict[str, Any]:
        """Risk config with the live caps resolved from the broker.

        The runner feeds ``day_pnl``/``open_exposure`` from
        ``core.risk_context``. The bridge used to pass neither, so a
        configured ``max_daily_loss`` could never be evaluated and would either
        block every entry (fail-closed) or, if absent, protect nothing. Here the
        broker positionbook is the default source and an explicit override in
        the risk block still wins, exactly like the runner.
        """
        from blue_engine.core.risk_context import fetch_risk_context

        cfg = json.loads(json.dumps(self.risk_config))  # defensive deep copy
        risk_block = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else cfg
        if not isinstance(risk_block, dict):
            risk_block = {}
            cfg = {"risk": risk_block}
        context: Dict[str, Any] = {}
        if (risk_block.get("max_daily_loss") is not None
                or risk_block.get("max_open_exposure") is not None):
            try:
                context = fetch_risk_context(self.client, cfg)
            except Exception as exc:  # noqa: BLE001 - the gate fails closed
                self.log(f"[RISK] risk context unavailable: {exc!r}")
                context = {}
            for warning in context.get("warnings") or []:
                self.log(f"[RISK] {warning}")
        return {"config": cfg, "context": context}

    def _arrival_price(self, symbol: str, exchange: str) -> Optional[float]:
        """Quote price for the monetary caps (None when unavailable)."""
        try:
            quote = self.client.quotes(symbol=symbol, exchange=exchange)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[RISK] {symbol}/{exchange} quote failed: {exc!r}")
            return None
        data = quote.get("data") if isinstance(quote, dict) else None
        if not isinstance(data, dict) or quote.get("status") != "success":
            return None
        for key in ("ltp", "last_price", "lastPrice", "lp", "close"):
            try:
                price = float(data.get(key))
            except (TypeError, ValueError):
                continue
            if price > 0 and price == price and price != float("inf"):
                return price
        return None

    # ------------------------------------------------------------------ #
    def disable_symbol(self, symbol: str, reason: str) -> None:
        self._disabled[symbol] = reason
        worker = self._active.pop(symbol, None)
        if worker is not None:
            worker.stop()
        self.log(f"[DISABLED] {symbol}: {reason}")

    # ------------------------------------------------------------------ #
    def _build_signal(self, symbol: str,
                      exchange: Optional[str] = None) -> OCLKeylevelSignal:
        params = dict(self.strategy_params)
        params.setdefault("symbol", symbol)
        params.setdefault("exchange", exchange or self.exchange)
        params.setdefault("exec_mode", "CONTINUOUS")
        params.setdefault("one_shot", True)
        return OCLKeylevelSignal(self.client, params)

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Spawn one worker thread per selected symbol (bounded)."""
        if not self.symbols:
            self.log("[BRIDGE] no symbols from screener; nothing to do")
            return
        selected = self.symbols[: self.max_active_symbols]
        skipped = len(self.symbols) - len(selected)
        defaulted = [f"{s}/{e}" for s, e in selected if e == self.exchange]
        self.log(f"[BRIDGE] starting {len(selected)} symbol worker(s)"
                 + (f"; {skipped} skipped (max_active_symbols="
                    f"{self.max_active_symbols})" if skipped else "")
                 + (f"; {len(defaulted)} on default exchange {self.exchange}"
                    if defaulted and len(defaulted) != len(selected) else ""))
        for symbol, exchange in selected:
            key = f"{symbol}|{exchange}"
            if key in self._disabled:
                self.log(f"[BRIDGE] {symbol}/{exchange} already disabled; skip")
                continue
            try:
                signal = self._build_signal(symbol, exchange)
            except Exception as exc:            # noqa: BLE001
                self.disable_symbol(key, f"init error: {exc}")
                continue
            worker = SymbolWorker(symbol, exchange, signal, self,
                                  poll_seconds=self.poll_seconds)
            self._active[key] = worker
            worker.start()
        self.log(f"[BRIDGE] {len(self._active)} worker(s) live; "
                 f"waiting for fires (Ctrl+C to stop)")
        try:
            while any(w.is_alive() for w in self._active.values()):
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.log("[BRIDGE] interrupt -> stopping workers")
            self.stop()

    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        for w in self._active.values():
            w.stop()

    # ------------------------------------------------------------------ #
    def execute_intent(self, symbol: str, exchange: str,
                       intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Execute one fired intent through RiskGate + POV slicer."""
        pos_key = f"{symbol}|{exchange}"
        if self.kill_tripped():
            self.log(f"[EXEC] {symbol}/{exchange} BLOCKED: kill switch tripped")
            return None
        is_exit = bool((intent.get("_meta") or {}).get("position_exit"))
        if intent.get("action") != ("SELL" if is_exit else "BUY"):
            self.log(f"[EXEC] {symbol}/{exchange} skipped: unexpected action "
                     f"{intent.get('action')} (position_exit={is_exit})")
            return None

        with self._exec_lock:
            quantity = int(intent.get("quantity") or 0)
            if is_exit:
                tracked = int(self._open_positions.get(pos_key, 0))
                if tracked <= 0:
                    self.log(f"[EXEC] {symbol}/{exchange} exit ignored: "
                             "no tracked open quantity")
                    return None
                quantity = min(quantity, tracked)
            if quantity <= 0:
                self.log(f"[EXEC] {symbol}/{exchange} skipped: zero quantity")
                return None
            stop_loss = intent.get("stop_loss")
            target = intent.get("target")
            self.log(f"[EXEC] {symbol}/{exchange}: "
                     f"{'SELL' if is_exit else 'BUY'} {quantity} "
                     f"(SL={stop_loss}, TP={target}, "
                     f"exit={intent.get('_meta', {}).get('exit_mode')})")

            # ---- Pre-trade risk gate (fail-closed on breach) ----
            from blue_engine.core.risk_gate import check_intent as risk_check
            from blue_engine.pov.slicers.pov import POVSlicer

            # Money caps need a real price; an exit reuses the entry policy.
            price = intent.get("price")
            if price is None:
                price = self._arrival_price(symbol, exchange)
            risk_view = self._risk_view()
            risk_block = risk_view["config"].get("risk") or risk_view["config"]
            needs_price = (risk_block.get("max_order_notional") is not None
                           or risk_block.get("max_open_exposure") is not None)
            if needs_price and price is None:
                self.log(f"[EXEC] {symbol}/{exchange} BLOCKED: no finite "
                         "positive quote for the monetary risk caps")
                self._record(symbol, exchange, intent, None, "risk_gate_block")
                return None
            decision = risk_check(
                {"action": intent.get("action", "BUY"),
                 "quantity": quantity,
                 "price": price},
                config=risk_view["config"],
                day_pnl=risk_view["context"].get("day_pnl"),
                open_exposure=risk_view["context"].get("open_exposure"),
                require_intent_price=needs_price,
                reduces_exposure=is_exit,
                live=self._live_mode())
            if decision is not None and not decision.approved:
                self.log(f"[EXEC] {symbol}/{exchange} BLOCKED by risk gate: "
                         f"{decision.reason}")
                self._record(symbol, exchange, intent, None, "risk_gate_block")
                return None

            # ---- POV slicer ----
            pov_cfg = dict(self.pov_params)
            if price is not None:
                pov_cfg.setdefault("limit_price", price)
            pov_cfg.setdefault("symbol", symbol)
            pov_cfg.setdefault("exchange", exchange)
            pov_cfg.setdefault("total_quantity", quantity)
            pov_cfg.setdefault("quantity", quantity)
            pov_cfg.setdefault("strategy_label", "OCL-BRIDGE")
            if stop_loss is not None:
                pov_cfg["stop_loss_price"] = stop_loss
            pov_cfg["target_price"] = target
            # For dashboards/runner-side audits too. Actual protective
            # exit evaluation is performed inside POVSlicer.execute().
            if stop_loss is not None:
                pov_cfg.setdefault("stop_loss_price", stop_loss)
            try:
                slicer = POVSlicer(self.client, pov_cfg)
                summary = slicer.execute({
                    "action": intent.get("action", "BUY"),
                    "quantity": quantity,
                    "tag": "OCL-BRIDGE",
                    "reason": str(intent.get("reason", "")),
                    "stop_loss": stop_loss,
                    "target": target,
                    "_meta": intent.get("_meta", {}),
                })
            except Exception as exc:            # noqa: BLE001
                self.log(f"[EXEC] {symbol}/{exchange} slicer error: {exc!r}")
                self._record(symbol, exchange, intent, None,
                             f"slicer error: {exc}")
                return None
            filled = int((summary or {}).get("filled") or 0)
            if is_exit:
                remaining = max(0, self._open_positions.get(pos_key, 0) - filled)
                if remaining:
                    self._open_positions[pos_key] = remaining
                else:
                    self._open_positions.pop(pos_key, None)
            elif filled > 0:
                self._open_positions[pos_key] = min(quantity, filled)
                # Audit 2026-10-29 (P0-2): POVSlicer only RECORDS stop/target
                # in core/protective_orders; it never places or watches a
                # broker order. A non-OCL entry therefore stayed unprotected
                # for the whole supervision window. Protect the fill now.
                self._protect_after_fill(symbol, exchange)
            self._record(symbol, exchange, intent, summary,
                         "closed" if is_exit else "executed")
            self.log(f"[EXEC] {symbol}/{exchange} summary: "
                     f"filled={(summary or {}).get('filled')} "
                     f"orders={(summary or {}).get('child_orders')}")
            return summary

    # ------------------------------------------------------------------ #
    def _protect_after_fill(self, symbol: str, exchange: str) -> None:
        """Place a broker SL-M for a freshly filled position, once.

        ``exit_config=None`` disables the engine and logs LOUDLY, because an
        uncontrolled risk profile is a capital-safety event, not a default.
        """
        if self.exit_config is None:
            self.log(f"[EXIT] {symbol}/{exchange} filled but exit engine is "
                     "not configured - position is UNPROTECTED")
            return
        from blue_engine.core import protective_orders
        from blue_engine.core.exit_engine import ExitConfig, ExitEngine

        engine = ExitEngine(
            self.client, ExitConfig.from_dict(self.exit_config),
            registry=protective_orders,
            kill_check=self.kill_tripped, log=self.log)
        report = engine.run_cycle()
        self.log(f"[EXIT] {symbol}/{exchange} protected="
                 f"{len(report.get('protected') or [])}/{report.get('checked')} "
                 f"placed={len(report.get('placed') or [])} "
                 f"critical={report.get('critical')}")
        for error in report.get("errors") or []:
            self.log(f"[EXIT] {symbol}/{exchange} ERROR: {error}")
        if report.get("critical"):
            self._protection_failed = True

    # ------------------------------------------------------------------ #
    def _record(self, symbol: str, exchange: str, intent: Dict[str, Any],
                summary: Optional[Dict[str, Any]], status: str) -> None:
        self._executed.append({
            "ts": datetime.now().isoformat(),
            "symbol": symbol, "exchange": exchange,
            "status": status,
            "intent": intent, "summary": summary,
        })

    # ------------------------------------------------------------------ #
    def save_report(self, path: Path) -> Path:
        """Write the run report (for the dashboard error monitor).

        Atomic (temp + replace): the dashboard polls this file while the
        bridge is running, and must never read a half-written JSON body.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "as_of": ist_now().isoformat(),
            "exchange": self.exchange,
            "symbols": [{"symbol": s, "exchange": e}
                        for s, e in self.symbols],
            "active": sorted(self._active),
            "disabled": self._disabled,
            "open_positions": dict(self._open_positions),
            "executed": self._executed,
        }, indent=2, default=str)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
        self.log(f"[BRIDGE] report saved -> {path}")
        return path


def run_bridge(symbols: Sequence[SymbolSpec], **kwargs: Any) -> ScreenerBridge:
    """Convenience: build + run the bridge over the screener's list."""
    bridge = ScreenerBridge(symbols=symbols, **kwargs)
    bridge.start()
    return bridge


__all__ = ["ScreenerBridge", "SymbolWorker", "SymbolSpec",
           "normalize_symbols", "run_bridge"]
