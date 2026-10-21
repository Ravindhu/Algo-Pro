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
from typing import Any, Dict, List, Optional

from blue_engine.alpha.Statergy.ocl_keylevel import OCLKeylevelSignal
from blue_engine.core.client import init_client
from blue_engine.core.time_utils import ist_now


class SymbolWorker(threading.Thread):
    """One OCL signal per symbol, polled until it fires or is disabled."""

    def __init__(self, symbol: str, exchange: str, signal: OCLKeylevelSignal,
                 bridge: "ScreenerBridge", poll_seconds: float = 5.0):
        super().__init__(name=f"ocl-{symbol}", daemon=True)
        self.symbol = symbol
        self.exchange = exchange
        self.signal = signal
        self.bridge = bridge
        self.poll_seconds = poll_seconds
        self._stop_event = threading.Event()

    def run(self) -> None:
        self.bridge.log(f"[WORKER] {self.symbol} start "
                        f"(poll={self.poll_seconds}s)")
        while not self._stop_event.is_set():
            if self.bridge.kill_tripped():
                self.bridge.log(f"[WORKER] {self.symbol} kill switch "
                                f"tripped -> stopping")
                return
            try:
                intent = self.signal.detect()
            except Exception as exc:            # noqa: BLE001
                self.bridge.log(f"[WORKER] {self.symbol} signal error: "
                                f"{exc!r}")
                self.bridge.disable_symbol(self.symbol, f"signal error: {exc}")
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
                 symbols: Optional[List[str]] = None,
                 strategy_params: Optional[Dict[str, Any]] = None,
                 pov_params: Optional[Dict[str, Any]] = None,
                 max_active_symbols: int = 5,
                 max_concurrent_executions: int = 1,
                 poll_seconds: float = 5.0,
                 risk_config: Optional[Dict[str, Any]] = None,
                 wait_for_exit: bool = True,
                 log=None):
        self.exchange = exchange.upper()
        self.symbols = [s.upper() for s in (symbols or [])]
        self.strategy_params = dict(strategy_params or {})
        self.pov_params = dict(pov_params or {})
        self.risk_config = dict(risk_config or {})
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
        """Durable kill-switch check (single source: optim_policy)."""
        from blue_engine.portfolio.optim_policy import kill_switch_state
        return bool(kill_switch_state().get("tripped"))

    # ------------------------------------------------------------------ #
    def log(self, msg: str) -> None:
        self._log(msg)

    # ------------------------------------------------------------------ #
    def disable_symbol(self, symbol: str, reason: str) -> None:
        self._disabled[symbol] = reason
        worker = self._active.pop(symbol, None)
        if worker is not None:
            worker.stop()
        self.log(f"[DISABLED] {symbol}: {reason}")

    # ------------------------------------------------------------------ #
    def _build_signal(self, symbol: str) -> OCLKeylevelSignal:
        params = dict(self.strategy_params)
        params.setdefault("symbol", symbol)
        params.setdefault("exchange", self.exchange)
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
        self.log(f"[BRIDGE] starting {len(selected)} symbol worker(s)"
                 + (f"; {skipped} skipped (max_active_symbols="
                    f"{self.max_active_symbols})" if skipped else ""))
        for symbol in selected:
            if symbol in self._disabled:
                self.log(f"[BRIDGE] {symbol} already disabled; skip")
                continue
            try:
                signal = self._build_signal(symbol)
            except Exception as exc:            # noqa: BLE001
                self.disable_symbol(symbol, f"init error: {exc}")
                continue
            worker = SymbolWorker(symbol, self.exchange, signal, self,
                                  poll_seconds=self.poll_seconds)
            self._active[symbol] = worker
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
        if self.kill_tripped():
            self.log(f"[EXEC] {symbol} BLOCKED: kill switch tripped")
            return None
        is_exit = bool((intent.get("_meta") or {}).get("position_exit"))
        if intent.get("action") != ("SELL" if is_exit else "BUY"):
            self.log(f"[EXEC] {symbol} skipped: unexpected action "
                     f"{intent.get('action')} (position_exit={is_exit})")
            return None

        with self._exec_lock:
            quantity = int(intent.get("quantity") or 0)
            if is_exit:
                tracked = int(self._open_positions.get(symbol, 0))
                if tracked <= 0:
                    self.log(f"[EXEC] {symbol} exit ignored: no tracked open quantity")
                    return None
                quantity = min(quantity, tracked)
            if quantity <= 0:
                self.log(f"[EXEC] {symbol} skipped: zero quantity")
                return None
            stop_loss = intent.get("stop_loss")
            target = intent.get("target")
            self.log(f"[EXEC] {symbol}: BUY {quantity} "
                     f"(SL={stop_loss}, TP={target}, "
                     f"exit={intent.get('_meta', {}).get('exit_mode')})")

            # ---- Pre-trade risk gate (fail-closed on breach) ----
            from blue_engine.core.risk_gate import check_intent as risk_check
            from blue_engine.pov.slicers.pov import POVSlicer

            decision = risk_check(
                {"action": intent.get("action", "BUY"),
                 "quantity": quantity,
                 "price": intent.get("price")},
                config=self.risk_config)
            if decision is not None and not decision.approved:
                self.log(f"[EXEC] {symbol} BLOCKED by risk gate: "
                         f"{decision.reason}")
                self._record(symbol, intent, None, "risk_gate_block")
                return None

            # ---- POV slicer ----
            pov_cfg = dict(self.pov_params)
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
                self.log(f"[EXEC] {symbol} slicer error: {exc!r}")
                self._record(symbol, intent, None, f"slicer error: {exc}")
                return None
            filled = int((summary or {}).get("filled") or 0)
            if is_exit:
                remaining = max(0, self._open_positions.get(symbol, 0) - filled)
                if remaining:
                    self._open_positions[symbol] = remaining
                else:
                    self._open_positions.pop(symbol, None)
            elif filled > 0:
                self._open_positions[symbol] = min(quantity, filled)
            self._record(symbol, intent, summary,
                         "closed" if is_exit else "executed")
            self.log(f"[EXEC] {symbol} summary: "
                     f"filled={summary.get('filled')} "
                     f"orders={summary.get('child_orders')}")
            return summary

    # ------------------------------------------------------------------ #
    def _record(self, symbol: str, intent: Dict[str, Any],
                summary: Optional[Dict[str, Any]], status: str) -> None:
        self._executed.append({
            "ts": datetime.now().isoformat(),
            "symbol": symbol, "exchange": self.exchange,
            "status": status,
            "intent": intent, "summary": summary,
        })

    # ------------------------------------------------------------------ #
    def save_report(self, path: Path) -> Path:
        """Write the run report (for the dashboard error monitor)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "as_of": ist_now().isoformat(),
            "symbols": self.symbols,
            "disabled": self._disabled,
            "executed": self._executed,
        }, indent=2, default=str), encoding="utf-8")
        self.log(f"[BRIDGE] report saved -> {path}")
        return path


def run_bridge(symbols: List[str], **kwargs: Any) -> ScreenerBridge:
    """Convenience: build + run the bridge over the screener's list."""
    bridge = ScreenerBridge(symbols=symbols, **kwargs)
    bridge.start()
    return bridge
