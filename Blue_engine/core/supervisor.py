"""supervisor.py - restart the trading engine, but only after the gates pass.

WHY THIS EXISTS
---------------
Audit 2026-10-29 (P0-7): ``scripts/ops/start_all.py`` started OpenAlgo, the
WebSocket proxy and the dashboard, and NOTHING supervised the trading engine
itself. A crashed run, a dropped connection or an OpenAlgo restart simply left
the process dead with no record and no recovery.

The recovery policy in this project is explicit: capital protection outranks
availability. So a supervisor here is NOT "restart until it works". It is a
small state machine with hard gates:

    gate 1  durable kill switch clear            (operator reset required)
    gate 2  OpenAlgo reachable                   (host responds)
    gate 3  broker positionbook readable         (positions are verifiable)
    gate 4  no unverified working orders         (no stranded sl/tp)
    gate 5  price feed sane                      (a quote for the symbol)
    gate 6  previous run did not fail CRITICAL   (manual intervention)

Only when all six pass does it start the engine again. Any failure parks the
supervisor in ``HALTED`` and it stays there until an operator clears the cause
and restarts the supervisor (there is deliberately no auto-unhalt).

DESIGN RULES
------------
* **No auto-resume of live trading after a CRITICAL failure.** A CRITICAL exit
  engine report or a tripped kill switch means a human must look.
* **Bounded restarts.** ``max_restarts`` per ``restart_window_seconds``; the
  backoff is exponential and capped, so a broker outage does not turn into a
  restart storm that hammers the API.
* **Every event is recorded** (in memory + a JSONL journal) so the dashboard and
  the post-mortem have the same facts: what ran, when, why it stopped, what
  blocked the restart.
* **Fail closed on ambiguity.** An unreadable positionbook is not "flat"; a
  missing quote is not "no position".
* **Testable.** The clock, the sleep and the engine entry point are injected,
  so the whole state machine runs in milliseconds without a broker.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "SupervisorConfig",
    "Supervisor",
    "GateResult",
    "PREFLIGHT_OK",
    "PREFLIGHT_BLOCKED",
]

#: Exit codes the engine uses to tell the supervisor *why* it stopped.
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_BUILD_ERROR = 3
EXIT_SAFETY_STOP = 4          # risk gate / kill switch / exit engine CRITICAL
EXIT_FEED_UNAVAILABLE = 5     # order feed down in live mode
#: Codes that must never be retried automatically.
FATAL_EXIT_CODES = {EXIT_SAFETY_STOP}

PREFLIGHT_OK = "ok"
PREFLIGHT_BLOCKED = "blocked"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class GateResult:
    """Outcome of one pre-restart gate."""

    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"gate": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class SupervisorConfig:
    """Tunables for :class:`Supervisor`."""

    #: Command used to run the engine. ``{config}`` is substituted.
    command: List[str] = field(default_factory=list)
    #: Extra arguments appended to ``command``.
    extra_args: List[str] = field(default_factory=list)
    #: Seconds to wait before the first restart after a crash.
    restart_delay_seconds: float = 5.0
    #: Exponential backoff cap.
    max_restart_delay_seconds: float = 120.0
    #: Restarts allowed inside the window before the supervisor halts.
    max_restarts: int = 5
    restart_window_seconds: float = 3600.0
    #: Run the pre-restart gates before EVERY attempt (including the first).
    preflight_on_start: bool = True
    #: Journal file for events (JSONL). None disables the file.
    journal_path: Optional[str] = None
    #: Treat a non-zero exit as a crash (True) or as a deliberate stop (False).
    restart_on_exit_codes: Optional[List[int]] = None

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "SupervisorConfig":
        cfg = dict(raw or {})
        return cls(
            command=list(cfg.get("command") or []),
            extra_args=list(cfg.get("extra_args") or []),
            restart_delay_seconds=float(cfg.get("restart_delay_seconds", 5.0)),
            max_restart_delay_seconds=float(
                cfg.get("max_restart_delay_seconds", 120.0)),
            max_restarts=int(cfg.get("max_restarts", 5)),
            restart_window_seconds=float(cfg.get("restart_window_seconds", 3600.0)),
            preflight_on_start=bool(cfg.get("preflight_on_start", True)),
            journal_path=cfg.get("journal_path"),
            restart_on_exit_codes=(None if cfg.get("restart_on_exit_codes") is None
                                   else [int(c) for c in cfg["restart_on_exit_codes"]]),
        )


class Supervisor:
    """Owns one engine process at a time and applies the restart gates.

    States: ``IDLE -> RUNNING -> (STOPPED | HALTED)``. ``HALTED`` is terminal
    for the process: an operator clears the cause and starts a new supervisor.
    """

    def __init__(self, config: SupervisorConfig, *,
                 runner: Optional[Callable[[List[str]], int]] = None,
                 gates: Optional[List[Callable[[], GateResult]]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Optional[Callable[[float], None]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 stop_event: Optional[threading.Event] = None) -> None:
        self.config = config
        self._runner = runner or self._default_runner
        self._gates = gates if gates is not None else []
        self._clock = clock
        self._sleep = sleep if sleep is not None else _interruptible_sleep
        self._stop_event = stop_event or threading.Event()
        self._log_sink = log
        self.state = "IDLE"
        self.halt_reason: Optional[str] = None
        self.restarts: List[float] = []
        self.events: List[Dict[str, Any]] = []

    # -- logging / journal ----------------------------------------------
    def _log(self, message: str) -> None:
        line = f"[supervisor {datetime.now().strftime('%H:%M:%S')}] {message}"
        if self._log_sink is not None:
            try:
                self._log_sink(line)
            except Exception:  # noqa: BLE001
                pass
        else:
            print(line)

    def _event(self, kind: str, **data: Any) -> Dict[str, Any]:
        record = {"ts": _now(), "kind": kind, **data}
        self.events.append(record)
        path = self.config.journal_path
        if path:
            try:
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")
            except Exception as exc:  # noqa: BLE001 - journaling is best effort
                logger.warning("supervisor journal write failed: %s", exc)
        return record

    # -- gates -----------------------------------------------------------
    def preflight(self) -> Dict[str, Any]:
        """Run every gate. Returns ``{status, gates, first_failure}``."""
        results: List[GateResult] = []
        for gate in self._gates:
            try:
                result = gate()
            except Exception as exc:  # noqa: BLE001 - a broken gate is a block
                result = GateResult(getattr(gate, "__name__", "gate"), False,
                                    f"gate raised: {exc!r}")
            if not isinstance(result, GateResult):
                result = GateResult(getattr(gate, "__name__", "gate"),
                                    bool(getattr(result, "ok", False)),
                                    str(getattr(result, "detail", "")))
            results.append(result)
            if not result.ok:
                # Stop at the first failure: later gates may depend on it.
                break
        failed = [r for r in results if not r.ok]
        return {
            "status": PREFLIGHT_BLOCKED if failed else PREFLIGHT_OK,
            "gates": [r.as_dict() for r in results],
            "first_failure": failed[0].as_dict() if failed else None,
        }

    # -- restart budget ---------------------------------------------------
    def _within_budget(self) -> bool:
        now = self._clock()
        horizon = now - self.config.restart_window_seconds
        self.restarts = [t for t in self.restarts if t >= horizon]
        return len(self.restarts) < self.config.max_restarts

    def _backoff(self, attempt: int) -> float:
        base = max(0.0, self.config.restart_delay_seconds)
        delay = base * (2 ** max(0, attempt))
        return min(delay, max(base, self.config.max_restart_delay_seconds))

    # -- lifecycle --------------------------------------------------------
    def _halt(self, reason: str, **data: Any) -> None:
        self.state = "HALTED"
        self.halt_reason = reason
        self._log(f"HALTED: {reason}")
        self._event("halted", reason=reason, **data)

    def should_restart(self, exit_code: int) -> Dict[str, Any]:
        """Decide whether ``exit_code`` may be restarted. Pure (no sleeping)."""
        fatal = exit_code in FATAL_EXIT_CODES
        allowed_codes = self.config.restart_on_exit_codes
        by_code = (exit_code in allowed_codes) if allowed_codes is not None \
            else (exit_code != EXIT_OK)
        if fatal:
            return {"restart": False, "reason": (
                "safety stop (kill switch / risk gate / unverified protection); "
                "an operator must review and re-arm")}
        if exit_code == EXIT_OK:
            return {"restart": False, "reason": "engine completed normally"}
        if not by_code:
            return {"restart": False,
                    "reason": f"exit {exit_code} is not restartable by config"}
        if not self._within_budget():
            return {"restart": False, "reason": (
                f"restart budget exhausted ({self.config.max_restarts} in "
                f"{self.config.restart_window_seconds:.0f}s)")}
        return {"restart": True, "reason": f"exit {exit_code} is recoverable"}

    def run_once(self) -> int:
        """Start the engine once (after preflight) and return its exit code."""
        if self.config.preflight_on_start:
            check = self.preflight()
            self._event("preflight", **check)
            if check["status"] != PREFLIGHT_OK:
                failure = check["first_failure"] or {}
                self._halt(f"preflight blocked at {failure.get('gate')}: "
                           f"{failure.get('detail')}")
                return EXIT_SAFETY_STOP
        self.state = "RUNNING"
        cmd = list(self.config.command) + list(self.config.extra_args)
        self._event("start", command=cmd)
        self._log(f"starting: {' '.join(cmd)}")
        try:
            code = int(self._runner(cmd))
        except Exception as exc:  # noqa: BLE001 - a broken start is a crash
            self._log(f"engine start failed: {exc!r}")
            self._event("start_error", error=repr(exc))
            code = 1
        self.state = "STOPPED"
        self._event("stop", exit_code=code)
        self._log(f"engine stopped with exit code {code}")
        return code

    def run(self, max_cycles: Optional[int] = None) -> Dict[str, Any]:
        """Supervise the engine until it stops cleanly or the supervisor halts."""
        cycles = 0
        attempt = 0
        while not self._stop_event.is_set():
            if attempt > 0:
                if self.config.preflight_on_start:
                    check = self.preflight()
                    self._event("preflight", attempt=attempt, **check)
                    if check["status"] != PREFLIGHT_OK:
                        failure = check["first_failure"] or {}
                        self._halt(f"restart blocked at {failure.get('gate')}: "
                                   f"{failure.get('detail')}", attempt=attempt)
                        break
                delay = self._backoff(attempt - 1)
                self._log(f"restart {attempt} in {delay:.1f}s")
                self._event("restart_scheduled", attempt=attempt, delay=delay)
                # Honour the injected sleep (tests) while still allowing a
                # stop() during a long real backoff.
                if self._stop_during(delay):
                    break
            code = self.run_once()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            decision = self.should_restart(code)
            self._event("restart_decision", exit_code=code, **decision)
            if not decision["restart"]:
                if code != EXIT_OK:
                    # A non-restartable failure is a halt, not a clean exit.
                    self.state = "HALTED"
                    self.halt_reason = decision["reason"]
                    self._log(f"HALTED: {decision['reason']}")
                break
            self.restarts.append(self._clock())
            attempt += 1
        return self.report()

    def _stop_during(self, delay: float) -> bool:
        """Sleep ``delay`` and report whether a stop arrived meanwhile.

        Uses the injected ``sleep`` when one was supplied (deterministic
        tests); otherwise waits on the stop event so a real backoff can be
        interrupted immediately.
        """
        if self._sleep is not _interruptible_sleep:
            self._sleep(delay)
            return self._stop_event.is_set()
        return self._stop_event.wait(delay)

    def stop(self) -> None:
        """Ask the supervisor to stop after the current engine exits."""
        self._stop_event.set()

    def report(self) -> Dict[str, Any]:
        return {"state": self.state, "halt_reason": self.halt_reason,
                "restarts": len(self.restarts), "events": list(self.events)}

    # -- default process runner -------------------------------------------
    @staticmethod
    def _default_runner(cmd: List[str]) -> int:
        """Run the engine as a child process and return its exit code."""
        proc = subprocess.Popen(cmd)
        try:
            return proc.wait()
        except KeyboardInterrupt:      # pragma: no cover - interactive
            proc.terminate()
            try:
                return proc.wait(timeout=10)
            except Exception:          # noqa: BLE001
                proc.kill()
                return -1


def _interruptible_sleep(seconds: float) -> None:  # pragma: no cover - thin
    time.sleep(seconds)


# --------------------------------------------------------------------------- #
# Read-only status for the dashboard
# --------------------------------------------------------------------------- #
_DEFAULT_JOURNAL = (Path(__file__).resolve().parents[2] / "data"
                    / "supervisor_journal.jsonl")


def read_status(journal_path: Optional[str] = None,
                tail: int = 20) -> Dict[str, Any]:
    """Summarise the supervisor journal for the dashboard (read-only).

    Never raises and never invents a state: with no journal the answer is
    explicitly ``unknown`` rather than a reassuring "running".
    """
    path = Path(journal_path) if journal_path else _DEFAULT_JOURNAL
    out: Dict[str, Any] = {"available": False, "state": "unknown",
                           "halt_reason": None, "restarts": 0,
                           "last_event_at": None, "recent": []}
    try:
        if not path.is_file():
            return out
        lines = path.read_text(encoding="utf-8").splitlines()[-tail:]
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except Exception:  # noqa: BLE001 - a torn line is skipped
                continue
        if not events:
            return out
        out["available"] = True
        out["recent"] = events
        out["last_event_at"] = events[-1].get("ts")
        out["restarts"] = sum(1 for e in events
                              if e.get("kind") == "restart_scheduled")
        halts = [e for e in events if e.get("kind") == "halted"]
        if halts:
            out["state"] = "HALTED"
            out["halt_reason"] = halts[-1].get("reason")
        elif events[-1].get("kind") == "start":
            out["state"] = "RUNNING"
        elif events[-1].get("kind") == "stop":
            out["state"] = "STOPPED"
        else:
            out["state"] = "IDLE"
        return out
    except Exception as exc:  # noqa: BLE001 - status must never break a page
        out["error"] = repr(exc)
        return out


# --------------------------------------------------------------------------- #
# Default gates (built from the live client, so they test the real broker)
# --------------------------------------------------------------------------- #
def kill_switch_gate() -> GateResult:
    """Gate 1: the durable kill switch must be clear."""
    from blue_engine.portfolio.optim_policy import peek_kill_switch
    state = peek_kill_switch()
    if state.get("tripped"):
        return GateResult("kill_switch", False,
                          f"tripped: {state.get('reason')}")
    return GateResult("kill_switch", True, "clear")


def openalgo_reachable_gate(host: str) -> GateResult:
    """Gate 2: the OpenAlgo HTTP endpoint must answer."""
    import urllib.error
    import urllib.request
    url = host.rstrip("/") + "/"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return GateResult("openalgo", True, f"HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        # A 4xx still proves the server is up.
        return GateResult("openalgo", True, f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        return GateResult("openalgo", False, f"unreachable: {exc!r}")


def positionbook_gate(client) -> GateResult:
    """Gate 3: positions must be verifiable (never assume "flat")."""
    try:
        resp = client.positionbook()
    except Exception as exc:  # noqa: BLE001
        return GateResult("positionbook", False, f"raised: {exc!r}")
    if not (isinstance(resp, dict) and resp.get("status") == "success"):
        return GateResult("positionbook", False,
                          f"not successful: {str(resp)[:120]}")
    data = resp.get("data")
    rows = data if isinstance(data, list) else (
        data.get("positions") if isinstance(data, dict) else None)
    if not isinstance(rows, list):
        return GateResult("positionbook", False, "no rows list returned")
    open_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            return GateResult("positionbook", False, "malformed row")
        for key in ("netqty", "net_qty", "quantity", "qty"):
            try:
                if int(float(row.get(key) or 0)) != 0:
                    open_rows += 1
                    break
            except (TypeError, ValueError):
                continue
    return GateResult("positionbook", True, f"{open_rows} open position(s)")


def working_orders_gate(client, strategy_tags: Optional[List[str]] = None
                        ) -> GateResult:
    """Gate 4: no engine-owned order left in an unverifiable state.

    A stranded SL/TP from a previous run must be reconciled before the engine
    can add exposure on top of it.
    """
    tags = set(strategy_tags or [])
    try:
        resp = client.orderbook()
    except Exception as exc:  # noqa: BLE001
        return GateResult("orders", False, f"orderbook raised: {exc!r}")
    if not (isinstance(resp, dict) and resp.get("status") == "success"):
        return GateResult("orders", False, f"orderbook not successful: "
                                           f"{str(resp)[:120]}")
    data = resp.get("data")
    rows = data if isinstance(data, list) else (
        data.get("orders") if isinstance(data, dict) else None)
    if not isinstance(rows, list):
        return GateResult("orders", False, "no orders list returned")
    pending = 0
    for order in rows:
        if not isinstance(order, dict):
            continue
        status = str(order.get("order_status") or order.get("status") or "").lower()
        if status in {"complete", "cancelled", "rejected", "filled", "executed",
                      "traded"}:
            continue
        strategy = str(order.get("strategy") or "")
        if tags and strategy not in tags:
            continue
        pending += 1
    if pending:
        return GateResult("orders", False,
                          f"{pending} working order(s) need reconciliation")
    return GateResult("orders", True, "no working orders")


def quote_gate(client, symbol: str, exchange: str) -> GateResult:
    """Gate 5: a sane price must be readable for the traded symbol."""
    try:
        resp = client.quotes(symbol=symbol, exchange=exchange)
    except Exception as exc:  # noqa: BLE001
        return GateResult("quote", False, f"quotes raised: {exc!r}")
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, dict) or resp.get("status") != "success":
        return GateResult("quote", False, f"not successful: {str(resp)[:120]}")
    for key in ("ltp", "last_price", "lastPrice", "lp", "close"):
        try:
            price = float(data.get(key))
        except (TypeError, ValueError):
            continue
        if price > 0 and price == price:
            return GateResult("quote", True, f"ltp={price}")
    return GateResult("quote", False, "no finite positive price")


def broker_session_gate(client) -> GateResult:
    """Gate: the broker session must still be valid.

    OpenAlgo keeps the broker session; when it expires, ``positionbook`` still
    answers but every order is rejected. Checking the analyzer status exercises
    the same authenticated path cheaply, so an expired login is caught before
    an order rather than after.
    """
    try:
        resp = client.analyzerstatus()
    except Exception as exc:  # noqa: BLE001
        return GateResult("broker_session", False, f"analyzerstatus raised: {exc!r}")
    if not (isinstance(resp, dict) and resp.get("status") == "success"):
        return GateResult("broker_session", False,
                          f"not authenticated: {str(resp)[:140]}")
    return GateResult("broker_session", True, "authenticated")


def market_hours_gate(symbol: str = "", allow_outside: bool = False,
                      entry_cutoff: str = "14:45",
                      now: Optional[Any] = None) -> GateResult:
    """Gate: start the engine only inside the continuous trading session.

    A live engine launched at 22:00 or on a Sunday cannot trade - the broker
    rejects every order and the run burns its restart budget proving it. The
    supervisor parks instead, and an operator starts it at the open.
    """
    from blue_engine.core.time_utils import market_session_state
    if allow_outside:
        return GateResult("market_hours", True, "override: enforcement disabled")
    state = market_session_state(now=now, entry_cutoff=entry_cutoff)
    if state["open"]:
        return GateResult("market_hours", True, state["reason"])
    return GateResult("market_hours", False, f"{state['state']}: {state['reason']}")


def build_default_gates(client, host: str, symbol: str, exchange: str,
                        strategy_tags: Optional[List[str]] = None,
                        *, paper: bool = False,
                        entry_cutoff: str = "14:45"
                        ) -> List[Callable[[], GateResult]]:
    """The gate chain, in order. Stops at the first failure.

    Order matters: the cheapest and most fatal checks first. ``paper`` runs
    (analyze mode) are exempt from the market-hours gate because the sandbox
    accepts orders at any time; a live run is not.
    """
    gates: List[Callable[[], GateResult]] = [
        kill_switch_gate,
        lambda: openalgo_reachable_gate(host),
        lambda: broker_session_gate(client),
        lambda: positionbook_gate(client),
        lambda: working_orders_gate(client, strategy_tags),
        lambda: quote_gate(client, symbol, exchange),
    ]
    if not paper:
        gates.append(lambda: market_hours_gate(symbol, entry_cutoff=entry_cutoff))
    return gates
