"""5-minute strategy bot: screened symbols -> strategies -> risk gate -> POV child orders.

Flow stage: Algo router, Risk gate, Approved order queue, Audit log, Strategy bot framework, Strategy bot cycle
Status: PHASE 3 IMPLEMENTED (paper and analyze/sandbox). PHASE 1 LIVE SENDER: live mode is
refused unless live_enabled is true AND the injected live startup gate passes. The live
runner (scripts/run/run_strategy_bot_live.py) is the only caller that supplies both.

One cycle (run every interval_minutes):
  1. Mode is "paper" (simulated fills) or "analyze" (OpenAlgo sandbox); live is refused. Skip the cycle unless the live data source is approved in
     config/data_sources.yaml (live_allowed: true). Both checks are fail closed.
  2. Read the screen file. Keep only fresh, selected symbols with a known circuit limit and no
     ASM/GSM flag. Anything missing or unreadable means no symbols.
  3. For each symbol: read closed bars and daily stats (ADV, previous close). No data means skip.
  4. Run each strategy's signal(df). An approved signal becomes a parent plan (full quantity
     checked by the gate). A parent is stored once, however many cycles it appears.
  5. Manage open positions on their last closed bar: stop, target, breakeven and ATR trail
     (exit_manager.py). Exits go through the same gate. A stale last bar is used only for
     max_outage_cycles cycles; after that the exit is held and a CRITICAL alert is raised.
  6. Slice every open parent: child qty = pov share of the last closed bar's volume, never more
     than what is left. Each child goes through the POV executor (gate, queue, client).
  7. A parent is closed when fully sliced, when it is older than max_cycles, when its position
     exits, or when a child order gets a non-success reply (then the parent halts; never resent).
  8. Realized P&L feeds the daily and per-strategy loss counters. A daily loss over the limit
     trips the kill switch.

Every decision is written to the audit log. The bot never builds an order client. The caller
injects the sender, so this module cannot place an order by itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import time

import yaml

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.execution.pnl_store import PnlStore
from quant.execution.pov_executor import PovExecutor
from quant.execution.pov_band import BandConfig, compute_target_pov_rate
from quant.execution.peg import peg_level
from quant.monitor.broker_recon import ReconGate, startup_reconcile
from quant.monitor.feed_health import FeedHealthBook
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch
from quant.risk.pre_gate import PreGate, PreGateConfig
from quant.execution.exit_manager import ExitConfig, Position, evaluate_bar
from quant.strategies.signals.risk_adapter import gate_signal, levels_from_signal

ALLOWED_MODES = ("paper", "analyze", "live")


class PaperSender:
    """Sends children to the paper broker (simulated fills, no network). Injected by the caller."""

    bot_mode = "paper"

    def __init__(self, broker) -> None:
        self._broker = broker

    def place_child(self, intent, api_key, decision, price_type="MARKET", limit_price=None):
        res = self._broker.place(intent, decision)
        filled = res.get("status") == "FILLED"
        return {"status": "success" if filled else "error", "orderid": str(res.get("order_id", "")),
                "avg_price": res.get("avg_price"), "filled_qty": res.get("filled_qty", 0)}


class AnalyzeSender:
    """Sends children through the OpenAlgo sandbox (analyze mode). The caller passes the client in."""

    bot_mode = "analyze"

    def __init__(self, client) -> None:
        self._client = client
        self.limit_wait_seconds = 0.0   # peg mode sets this; 0 keeps the old immediate check
        self._sleep = time.sleep

    def place_child(self, intent, api_key, decision, price_type="MARKET", limit_price=None):
        """Place, then confirm the fill with the order status. Only 'complete' counts as filled."""
        placed = self._client.place_child(intent, api_key, decision, price_type, limit_price)
        if str(placed.get("status", "")).lower() != "success":
            return placed
        order_id = str(placed.get("orderid", ""))
        if not order_id:
            return {"status": "error", "orderid": "", "reason": "no order id; fill not confirmed"}
        if price_type == "LIMIT" and self.limit_wait_seconds > 0:
            self._sleep(self.limit_wait_seconds)
        st = self._client.get_order_status(api_key, order_id, intent.strategy_id)
        data = st.get("data") if isinstance(st, dict) and isinstance(st.get("data"), dict) else {}
        state = str(data.get("order_status", "")).lower()
        avg = data.get("average_price")
        if state == "complete" and isinstance(avg, (int, float)) and avg > 0:
            return {"status": "success", "orderid": order_id, "avg_price": float(avg),
                    "filled_qty": int(intent.qty)}
        if price_type == "LIMIT":
            return self._handle_unfilled_limit(intent, api_key, order_id, state, data)
        return {"status": "error", "orderid": order_id, "reason": f"fill not confirmed: {state or 'no status'}"}

    def _handle_unfilled_limit(self, intent, api_key, order_id, state, data) -> Dict[str, Any]:
        """An entry LIMIT that is not complete is cancelled. A partial fill is flagged, never dropped."""
        avg = data.get("average_price")
        partial = "partial" in state or (isinstance(avg, (int, float)) and avg > 0)
        try:
            cancel = self._client.cancel(order_id, intent.strategy_id, api_key)
        except Exception as exc:  # the cancel itself failed: the order may still be open
            cancel = {"status": "error", "message": repr(exc)}
        cancelled = str(cancel.get("status", "")).lower() == "success"
        if partial:
            return {"status": "error", "orderid": order_id, "partial": True, "cancel_failed": not cancelled,
                    "reason": f"partial entry fill ({state}); position not booked"}
        outcome = "cancelled" if cancelled else "cancel failed"
        return {"status": "error", "orderid": order_id, "cancel_failed": not cancelled,
                "reason": f"limit not filled ({state or 'no status'}); {outcome}"}


class LiveSender(AnalyzeSender):
    """Sends children to the REAL broker through OpenAlgoClient in live mode.

    Built only by scripts/run/run_strategy_bot_live.py after the live startup gate passes.
    It inherits AnalyzeSender's fill check (only OpenAlgo status 'complete' with a positive
    average price counts as filled). Every send still runs the client's own live guard: a
    recorded LiveApproval, an unchanged limits file, the compliance gate, and an approved
    GateDecision for the same intent. If any guard fails, the order is not sent.
    """

    bot_mode = "live"


class BotRefused(RuntimeError):
    """Raised for a configuration the bot must never run with (for example live mode)."""


def _strategy_name(entry) -> str:
    """A strategies entry is a plain name or a mapping with a 'name' key."""
    return str(entry["name"]) if isinstance(entry, dict) else str(entry)


BAND_KEYS = ("vwap_ceiling_pct", "vwap_floor_pct", "min_pov", "max_pov", "passthrough_floor")
BAND_DEFAULTS = {"vwap_ceiling_pct": 0.5, "vwap_floor_pct": 3.0, "min_pov": 0.05,
                 "max_pov": 0.25, "passthrough_floor": "part_pct"}


@dataclass(frozen=True)
class BotConfig:
    mode: str = "analyze"
    live_enabled: bool = False
    interval_minutes: float = 5.0
    pov: float = 0.1
    risk_per_trade: float = 0.0025
    max_cycles: int = 12
    max_outage_cycles: int = 3   # last known bar used for exits for at most this many cycles (3 x 5 min = 15 min)
    breakeven_r: float = 1.0
    trail_mult: float = 2.0
    # POV mode: 'fixed' slices at pov (old behaviour, default). 'band' sets the POV per parent from
    # the VWAP band in pov_band.py: high of band -> lower POV, low of band -> higher POV.
    pov_mode: str = "peg_band_buyer"   # the default. Only 'peg_band_buyer' and 'fixed' are allowed
    pov_band: Dict[str, Any] = field(default_factory=dict)
    peg_wait_seconds: float = 5.0     # peg_band_buyer: wait this long before checking a peg LIMIT
    peg_tick_size: float = 0.05       # peg: BUY goes this far above the best bid (0 turns it off); never to the ask
    peg_bid_fraction: float = 0.5     # peg: a child may take at most this share of the shares at the best bid
    peg_max_attempts: int = 3         # peg_band_buyer: max LIMIT placements per child (bounded reprice)
    vwap_poll_seconds: float = 15.0   # how often the live runner polls quotes for buyer VWAP (band mode only)
    entry_max_slippage_bps: float = 20.0   # entries are LIMIT orders this far above the last close (audit fix 6)
    max_screen_age_minutes: float = 10.0
    screen_path: str = "data/screened/latest.json"
    data_sources_path: str = "config/data_sources.yaml"
    strategies: tuple = field(default_factory=lambda: ("engulfing",))
    # Per-strategy breakeven and trail. Only breakeven_r and trail_mult may be set; a strategy
    # not listed here uses the global values above. Example: {engulfing: {trail_mult: 1.5}}.
    exits: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 < self.entry_max_slippage_bps <= 200:
            raise ValueError("entry_max_slippage_bps must be in (0, 200]")
        names = {_strategy_name(s) for s in self.strategies}
        for name, over in (self.exits or {}).items():
            if name not in names:
                raise ValueError(f"exits names strategy {name!r} which is not in strategies")
            if not isinstance(over, dict) or not set(over) <= {"breakeven_r", "trail_mult"}:
                raise ValueError(f"exits for {name!r} may set only breakeven_r and trail_mult")
            for k, v in over.items():
                if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                    raise ValueError(f"exits {name}.{k} must be a number > 0")
        if not 1 <= self.peg_wait_seconds <= 60:
            raise ValueError("peg_wait_seconds must be in [1, 60]")
        if not 0 <= self.peg_tick_size <= 1:
            raise ValueError("peg_tick_size must be in [0, 1]")
        if not 0 < self.peg_bid_fraction <= 1:
            raise ValueError("peg_bid_fraction must be in (0, 1]")
        if not 1 <= self.peg_max_attempts <= 5:
            raise ValueError("peg_max_attempts must be in [1, 5]")
        if not 5 <= self.vwap_poll_seconds <= 300:
            raise ValueError("vwap_poll_seconds must be in [5, 300]")
        if self.pov_mode not in ("peg_band_buyer", "fixed"):
            raise ValueError("pov_mode must be 'peg_band_buyer' (default) or 'fixed'")
        bad = set(self.pov_band or {}) - set(BAND_KEYS)
        if bad:
            raise ValueError(f"pov_band may set only {sorted(BAND_KEYS)}; got {sorted(bad)}")
        b = {**BAND_DEFAULTS, **(self.pov_band or {})}
        for k in ("vwap_ceiling_pct", "vwap_floor_pct"):
            if not isinstance(b[k], (int, float)) or b[k] <= 0:
                raise ValueError(f"pov_band.{k} must be a number > 0")
        for k in ("min_pov", "max_pov"):
            if not isinstance(b[k], (int, float)) or not 0 < b[k] <= 1:
                raise ValueError(f"pov_band.{k} must be in (0, 1]")
        if b["min_pov"] > b["max_pov"]:
            raise ValueError("pov_band.min_pov must not exceed max_pov")
        if str(b["passthrough_floor"]) not in ("part_pct", "min_pov", "max_pov"):
            raise ValueError("pov_band.passthrough_floor must be part_pct, min_pov or max_pov")

    def exit_config_for(self, strategy: str) -> ExitConfig:
        """Breakeven and trail for one strategy: its own values, else the global ones."""
        over = (self.exits or {}).get(strategy, {})
        return ExitConfig(breakeven_r=float(over.get("breakeven_r", self.breakeven_r)),
                          trail_mult=float(over.get("trail_mult", self.trail_mult)))

    @classmethod
    def from_yaml(cls, path) -> "BotConfig":
        data: Dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        names = set(cls.__dataclass_fields__)
        unknown = set(data) - names
        if unknown:
            raise ValueError(f"unknown bot keys: {sorted(unknown)}")
        if "risk_per_trade" in data:
            raise ValueError("risk_per_trade is set in config/settings.yaml (sizing), not in bot.yaml")
        if "strategies" in data:
            data["strategies"] = tuple(data["strategies"])
        return cls(**data)


def load_bot_config(path, settings_path=None) -> BotConfig:
    """BotConfig from bot.yaml, with risk per trade read from settings.yaml (one source, 2026-11-15).

    settings_path defaults to settings.yaml in the same folder as bot.yaml.
    """
    from dataclasses import replace

    cfg = BotConfig.from_yaml(path)
    settings = Path(settings_path) if settings_path is not None else Path(path).parent / "settings.yaml"
    return replace(cfg, risk_per_trade=RiskLimits.risk_per_trade(settings))


def live_feed_approved(path) -> bool:
    """True only when config/data_sources.yaml sets sources.live_feed.live_allowed to true. Any error is False."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return (data.get("sources") or {}).get("live_feed", {}).get("live_allowed") is True
    except Exception:
        return False


def read_screen(path, now: datetime, max_age_minutes: float) -> List[Dict[str, Any]]:
    """Screened symbols that are safe to trade. Returns [] for any missing, stale or malformed file.

    File format (written by the screener):
      {"as_of": "2026-11-10T10:15:00", "results": [
         {"symbol": "INFY", "selected": true, "asm_gsm": false, "circuit_pct": 0.2}, ...]}
    """
    import json

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        as_of = datetime.fromisoformat(str(data["as_of"]))
        if (now - as_of).total_seconds() > max_age_minutes * 60 or as_of > now:
            return []
        out = []
        for row in data.get("results", []):
            if row.get("selected") is not True or row.get("asm_gsm") is not False:
                continue
            circuit = row.get("circuit_pct")
            if not isinstance(circuit, (int, float)) or not 0 < float(circuit) <= 1:
                continue
            out.append({"symbol": str(row["symbol"]).upper(), "circuit_pct": float(circuit)})
        return out
    except Exception:
        return []


@dataclass
class _Parent:
    key: str
    intent: TradeIntent
    remaining: int
    circuit_pct: float
    stop: float = 0.0
    target: float = 0.0
    atr: float = 0.0
    strategy: str = ""
    adv_shares: Optional[float] = None
    prev_close: Optional[float] = None
    cycles: int = 0
    seq: int = 0


class _BotBook:
    """Read-only view of the bot's open positions, shaped for monitor/broker_recon.PositionsView."""

    def __init__(self, bot: "StrategyBot") -> None:
        self._bot = bot

    def open_positions(self) -> Dict[str, Position]:
        return dict(self._bot._positions)


class StrategyBot:
    def __init__(
        self,
        *,
        cfg: BotConfig,
        strategies: List[Any],
        bars,
        sender,
        audit,
        api_key: str,
        read_funds: Callable[[], float],
        limits: RiskLimits,
        kill: KillSwitch,
        safe: SafeMode,
        halt: HaltHandler,
        state: Optional[RiskState] = None,
        alerts=None,
        pre_gate: Optional[PreGate] = None,
        pnl_store: Optional[PnlStore] = None,
        feed_health=None,
        live_gate: Optional[Callable[[], Tuple[bool, List[str]]]] = None,
        stops=None,
        position_store=None,
        entry_guard: Optional[Callable[[], Tuple[bool, List[str]]]] = None,
        vwap_provider: Optional[Callable[[str], Optional[float]]] = None,
        vwap_tick: Optional[Callable[[datetime], None]] = None,
        buyer_share_provider: Optional[Callable[[str], Optional[float]]] = None,
        depth_provider: Optional[Callable[[str], Any]] = None,
    ) -> None:
        if cfg.mode not in ALLOWED_MODES:
            raise BotRefused(f"bot refuses mode {cfg.mode!r}; only {ALLOWED_MODES} is allowed")
        if cfg.mode == "live":
            # Live needs three things: the config switch, a gate function, and a gate that passes.
            if not cfg.live_enabled:
                raise BotRefused("live mode needs live_enabled: true in config/bot.yaml")
            if live_gate is None:
                raise BotRefused("live mode needs the live startup gate")
            if stops is None:
                raise BotRefused("live mode needs broker stop orders (SL-M)")
            if position_store is None:
                raise BotRefused("live mode needs the position store")
            if entry_guard is None:
                raise BotRefused("live mode needs the per-cycle entry guard")
            live_ok, live_reasons = live_gate()
            if not live_ok:
                raise BotRefused("live startup gate refused: " + "; ".join(live_reasons))
        elif cfg.live_enabled:
            raise BotRefused("live_enabled needs mode live; paper and analyze never send live orders")
        if getattr(sender, "bot_mode", None) != cfg.mode:
            raise BotRefused(f"sender is for {getattr(sender, 'bot_mode', None)!r} but bot mode is {cfg.mode!r}")
        if not api_key:
            raise ValueError("bot needs an API key")
        self.cfg = cfg
        self._vwap_provider = vwap_provider  # buyer-initiated VWAP per symbol; None = not available
        self.vwap_tick = vwap_tick           # called by the runner between cycles to poll quotes
        self._buyer_share_provider = buyer_share_provider  # buyer share of volume per symbol (buyer mode)
        self._depth_provider = depth_provider              # depth reply per symbol (peg mode)
        self._strategies = list(strategies)
        self._bars = bars
        self._audit = audit
        self._read_funds = read_funds
        self._limits = limits
        self._kill = kill
        self._safe = safe
        self._halt = halt
        self.state = state or RiskState()
        self._funds = 0.0
        self._parents: Dict[str, _Parent] = {}
        self._positions: Dict[str, Position] = {}
        self._last_bar: Dict[str, Dict[str, Any]] = {}  # last closed bar per symbol, used during a feed outage
        self.recon: Optional[ReconGate] = None  # set by run_startup_recon; None = not yet run (entries blocked)
        self._exit_cfg = ExitConfig(breakeven_r=cfg.breakeven_r, trail_mult=cfg.trail_mult)
        self._done: set = set()
        self.alerts = alerts                      # CRITICAL/WARNING go here; None = audit log only
        self._booked: Dict[str, float] = {}       # notional added to open exposure, per position key
        self._marks: Dict[str, float] = {}        # mark-to-market P&L each open position has added to daily P&L
        self._cycle_no = 0                        # cycle counter, used to age the last known bar
        self._pnl_day = None                      # day the realized P&L counters belong to
        self._pnl_store = pnl_store               # persists counters across restarts; None = in-memory only
        self._exit_held_qty: Optional[int] = None  # held qty of the exit being sent (read by _child_gate)
        self._stops = stops                        # broker SL-M stops: place / status / cancel (None = paper)
        self._pos_store = position_store           # restart-safe position book (None = in-memory only)
        self._entry_guard = entry_guard            # re-checked every cycle in live (egress IP)
        self._faults: set = set()                  # ("stop"|"partial", key): entries and slicing blocked while any exists
        self._restored_day = None                  # day of a restored book (keeps marks and done set on first cycle)
        # Pre-gate: capital-protection checks BEFORE the risk gate (calendar, blackout, gap,
        # handoff contract, stress/VaR, auction window, feed health). Exits bypass it; it can
        # only refuse, never approve.
        # Caps come from risk_limits.yaml through ``limits``, not from a bare
        # PreGateConfig(). With defaults, max_sector_pct/max_beta were permanently
        # None, so the book-level checks could never run in production even though
        # they were tested. Unset stays None, and the check refuses when enabled -
        # this only makes the operator's setting reachable.
        self._pre_gate = pre_gate if pre_gate is not None else PreGate(
            PreGateConfig(
                max_sector_pct=getattr(limits, "max_sector_pct", None),
                max_beta=getattr(limits, "max_beta", None),
                max_cvar=getattr(limits, "max_cvar", None),
                max_drawdown=getattr(limits, "max_drawdown", None),
            ))
        # Quote-feed health. One counter per symbol; a latched PAUSED symbol blocks
        # new entries for that symbol only. Exits are never blocked by it.
        self.feed_health = feed_health if feed_health is not None else FeedHealthBook()
        self._executor = PovExecutor(
            gate=self._child_gate, sender=sender, api_key=api_key, audit=audit, pov=cfg.pov,
        )

    # --- gate used by the POV executor for every child ---
    def _child_gate(self, intent: TradeIntent):
        held = self._exit_held_qty if intent.kind == "exit" else None
        return check_intent(
            intent, self.state, self._limits, self._kill, self._safe,
            available_funds=self._funds, halt=self._halt, held_qty=held,
        )

    def _log(self, kind: str, **payload: Any) -> None:
        self._audit.append(kind, payload)

    def _alert(self, level: str, message: str) -> None:
        self._log("alert", level=level, message=message)
        if self.alerts is not None:
            self.alerts.emit(level, "strategy_bot", message)

    def _roll_day(self, now: datetime) -> None:
        """New trading day: daily and per-strategy realized P&L start again at zero."""
        if self._pnl_day == now.date():
            return
        keep = self._pnl_day is None and self._restored_day == now.date()
        self._pnl_day = now.date()
        self.state.daily_pnl = 0.0
        self.state.strategy_pnl = {}
        if not keep:  # a restored book from today keeps its marks and parents done today
            self._marks = {}
            self._done.clear()
        self._restore_counters(now)
        self._persist_positions()
        self._log("pnl_day_reset", day=now.date().isoformat(), restored=keep)

    def _restore_counters(self, now: datetime) -> None:
        """Restart-safe counters: reload today's P&L from the store if it is newer.

        A process restart must not forget the morning's losses (AGENTS gap, fixed 2026-11-11).
        After restore, the kill switch is tripped again if the stored day already breached.
        """
        if self._pnl_store is None:
            return
        stored = self._pnl_store.load(now.date())
        if stored is None:
            return
        restored = stored["daily_pnl"]
        if abs(restored) > abs(self.state.daily_pnl) or \
                sum(abs(v) for v in stored["strategy_pnl"].values()) > \
                sum(abs(v) for v in self.state.strategy_pnl.values()):
            self.state.daily_pnl = stored["daily_pnl"]
            self.state.strategy_pnl = dict(stored["strategy_pnl"])
            self._log("pnl_counters_restored", daily_pnl=self.state.daily_pnl)
            limit = self._limits.max_daily_loss
            if limit is not None and self.state.daily_pnl <= -abs(float(limit)) and not self._kill.active:
                self._kill.trip(f"restored daily loss {self.state.daily_pnl:.2f} at or past limit",
                                source="automatic")
                self._alert("CRITICAL", "restored daily loss at limit: kill switch on")

    def _persist_counters(self) -> None:
        if self._pnl_store is not None and self._pnl_day is not None:
            self._pnl_store.save(self._pnl_day, self.state.daily_pnl, self.state.strategy_pnl)

    def _record_pnl(self, strategy: str, realized: float) -> None:
        """Add a realized P&L amount. Trips the kill switch when the daily loss limit is reached."""
        self.state.daily_pnl += realized
        self.state.strategy_pnl[strategy] = self.state.strategy_pnl.get(strategy, 0.0) + realized
        self._log("pnl_realized", strategy=strategy, amount=round(realized, 2),
                  daily_pnl=round(self.state.daily_pnl, 2))
        self._persist_counters()
        limit = self._limits.max_daily_loss
        if limit is not None and self.state.daily_pnl <= -abs(float(limit)):
            if not self._kill.active:
                self._kill.trip(f"daily loss {self.state.daily_pnl:.2f} reached limit {abs(float(limit)):.2f}",
                                source="automatic")
            self._alert("CRITICAL", "daily loss limit reached: kill switch on, no new entries")

    def run_startup_recon(self, fetch_broker_positions, *, require_broker: bool = False) -> ReconGate:
        """Call once at startup, after restoring state. Entries stay blocked until it allows them.

        fetch_broker_positions None in paper mode gives "skipped" (entries allowed). Live must pass a reader."""
        self.recon = startup_reconcile(_BotBook(self), fetch_broker_positions,
                                       audit=self._audit, require_broker=require_broker)
        self._log("startup_recon", status=self.recon.status, mismatches=self.recon.mismatches,
                  error=self.recon.error)
        return self.recon

    def _entries_allowed(self) -> bool:
        return self.recon is not None and self.recon.entries_allowed

    # --- one cycle ---
    def cycle(self, now: datetime) -> Dict[str, Any]:
        report: Dict[str, Any] = {"time": now.isoformat(), "parents_opened": 0, "children_sent": 0,
                                  "skipped": []}
        self._cycle_no += 1
        self._roll_day(now)
        self._check_broker_stops()
        self._resync_stops()
        if not live_feed_approved(self.cfg.data_sources_path):
            self._log("cycle_skipped", reason="live data source not approved (data_sources.yaml)")
            report["skipped"].append("data source not approved")
            if self._positions:
                # No bars means no stop checks. Open positions are unmanaged until the source is approved.
                self._log("positions_unmanaged", count=len(self._positions), keys=list(self._positions))
                self._alert("CRITICAL", f"{len(self._positions)} open position(s) unmanaged: "
                                        "live data source not approved")
            return report

        self._funds = self._safe_funds()
        symbols = read_screen(self.cfg.screen_path, now, self.cfg.max_screen_age_minutes)
        self._log("screen", count=len(symbols), symbols=[s["symbol"] for s in symbols])

        bars_cache: Dict[str, Any] = {}
        guard_ok = self._run_entry_guard()
        entries_ok = self._entries_allowed() and guard_ok and not self._faults
        if not entries_ok:
            if not self._entries_allowed():
                reason = "startup recon not allowed entries"
            elif not guard_ok:
                reason = "entry guard refused"
            else:
                reason = "open fault: " + ", ".join(sorted(f"{a}:{b}" for a, b in self._faults))
            self._log("entries_blocked", reason=reason, status=getattr(self.recon, "status", "not run"))
            report["skipped"].append("entries blocked: " + reason)
            symbols = []
        for item in symbols:
            sym = item["symbol"]
            df = self._bars.bars(sym, now)
            if df is None:
                # A symbol with no fresh bars is a FEED failure, not a quiet market.
                # Count it: enough consecutive misses latch PAUSED for this symbol
                # and the pre-gate then refuses its entries. Exits are unaffected.
                self.feed_health.record_failure(sym, "no fresh bars")
                report["skipped"].append(f"{sym}: no fresh bars")
                continue
            self.feed_health.record_success(sym)
            bars_cache[sym] = df
            self._remember_bar(sym, df)
            stats = self._bars.daily_stats(sym, now)
            if stats is None:
                report["skipped"].append(f"{sym}: no daily stats")
                continue
            for strat in self._strategies:
                self._scan(strat, sym, df, item["circuit_pct"], stats, now, report)

        self._manage_exits(bars_cache, now, report)
        if entries_ok:
            self._slice_parents(bars_cache, now, report)
        self._mark_positions_to_market(bars_cache, now, report)
        return report

    def _mark_positions_to_market(self, bars_cache: Dict[str, Any], now: datetime,
                                  report: Dict[str, Any]) -> None:
        """Open positions join daily and per-strategy P&L at their last closed-bar mark, every cycle.

        Each position remembers the mark it last added (self._marks). Each cycle only the change
        is added, for that position alone, so a falling book trips the daily loss limit before a
        stop realizes the loss. A mark is reversed when the position's average entry changes or
        the position closes (fixed 2026-11-15: the old code double-counted closed trades and
        added every change to the strategy counter once per open position).
        """
        for key, pos in self._positions.items():
            df = bars_cache.get(pos.symbol)
            if df is None:
                continue  # no fresh mark without bars: keep the previous mark
            px = float(df["close"].iloc[-2])  # last closed bar
            sign = 1.0 if pos.side == "BUY" else -1.0
            new_mark = sign * (px - pos.entry) * pos.qty
            delta = new_mark - self._marks.get(key, 0.0)
            self._marks[key] = new_mark
            if abs(delta) < 0.005:
                continue
            self._apply_mark_delta(pos.strategy, delta)
            self._log("mtm_update", key=key, mark=round(new_mark, 2), delta=round(delta, 2))
        limit = self._limits.max_daily_loss
        if limit is not None and self.state.daily_pnl <= -abs(float(limit)) and not self._kill.active:
            self._kill.trip(f"daily loss incl. unrealized {self.state.daily_pnl:.2f} "
                            f"reached limit {abs(float(limit)):.2f}", source="automatic")
            self._alert("CRITICAL", "daily loss limit (mark-to-market) reached: kill switch on")
        self._persist_counters()
        self._persist_positions()

    def _safe_funds(self) -> float:
        try:
            value = float(self._read_funds())
            return value if value > 0 else 0.0
        except Exception:
            return 0.0  # no funds known: entries are refused

    def _scan(self, strat, sym, df, circuit_pct, stats, now, report) -> None:
        try:
            sig = (strat.signal_for(sym, df, now) if hasattr(strat, "signal_for")
                   else strat.signal(df))
        except Exception as exc:
            self._log("strategy_error", strategy=strat.name, symbol=sym, error=repr(exc))
            return
        if sig is None:
            return
        if str(sig.get("side", "")).upper() != "BUY":
            self._log("long_only_refused", strategy=strat.name, symbol=sym, side=str(sig.get("side")))
            return
        if not isinstance(sig.get("atr"), (int, float)) or float(sig["atr"]) <= 0:
            self._log("parent_skipped", strategy=strat.name, symbol=sym, reason="no ATR in signal")
            return
        key = f"{sym}|{strat.name}|{str(sig.get('side'))}|{sig.get('time')}"
        if key in self._parents or key in self._done:
            return
        lot = stats.get("lot_size", 1)
        if not isinstance(lot, (int, float)) or lot < 1 or int(lot) != lot:
            self._log("parent_skipped", strategy=strat.name, symbol=sym, reason=f"bad lot size {lot!r}")
            return
        # Pre-gate (capital protection): calendar, handoff contract, auction window and
        # feed health. The overnight gap and stress/VaR checks run inside gate_signal on
        # the real intent (proper ref price + size).
        pre = self._pre_gate.check(
            TradeIntent(intent_id=f"{key}-PRE", strategy_id=strat.name, symbol=sym,
                        side="BUY", qty=1, ref_price=float(df["close"].iloc[-2]), kind="entry",
                        prev_close=None, trade_day=now.date()),
            now=now, feed_book=self.feed_health,
        )
        if not pre.ok:
            self._log("pre_gate_refused", key=key, reasons=list(pre.reasons))
            return
        res = gate_signal(
            sig, symbol=sym, strategy_id=strat.name, equity=self._funds,
            risk_fraction=self.cfg.risk_per_trade, state=self.state, limits=self._limits,
            kill=self._kill, safe=self._safe, halt=self._halt, available_funds=self._funds,
            adv_shares=stats["adv_shares"], prev_close=stats["prev_close"],
            circuit_pct=circuit_pct, asm_gsm=False, trade_day=now.date(), lot_size=int(lot),
            pre_gate=self._pre_gate, now=now, feed_book=self.feed_health,
        )
        self._log("parent_gate", key=key, approved=res.ok, reason=res.reason)
        if not res.ok or res.intent is None:
            return
        self.state.seen_ids.add(res.intent.intent_id)
        self._done.add(key)
        _, stop, target = levels_from_signal(sig)
        self._parents[key] = _Parent(key=key, intent=res.intent, remaining=res.intent.qty,
                                     circuit_pct=circuit_pct, stop=stop, target=target,
                                     atr=float(sig["atr"]), strategy=strat.name,
                                     adv_shares=stats["adv_shares"], prev_close=stats["prev_close"])
        report["parents_opened"] += 1

    def _pov_for(self, symbol: str, df, side: str) -> Tuple[float, str]:
        """POV fraction for one parent's child. Fixed mode: cfg.pov. Band mode: the VWAP band from
        pov_band.py, using the last closed close. No verified VWAP -> cfg.pov (logged)."""
        if self.cfg.pov_mode not in ("band", "band_buyer", "peg_band_buyer"):
            return self.cfg.pov, "fixed"
        vwap = self._vwap_provider(symbol) if self._vwap_provider is not None else None
        if vwap is None or not vwap > 0:
            self._log("pov_band_fallback", symbol=symbol, reason="no verified buyer VWAP")
            return self.cfg.pov, "fallback"
        b = {**BAND_DEFAULTS, **(self.cfg.pov_band or {})}
        band = BandConfig(
            vwap=float(vwap),
            vwap_ceiling_pct=float(b["vwap_ceiling_pct"]),
            vwap_floor_pct=float(b["vwap_floor_pct"]),
            min_pov_pct=float(b["min_pov"]) * 100.0,
            max_pov_pct=float(b["max_pov"]) * 100.0,
            part_pct=self.cfg.pov * 100.0,
            passthrough_floor=str(b["passthrough_floor"]),
            action=str(side).upper(),
        )
        ltp = float(df["close"].iloc[-2])  # last closed bar
        decision = compute_target_pov_rate(ltp=ltp, config=band)
        self._log("pov_band", symbol=symbol, ltp=ltp, vwap=float(vwap), reason=decision.reason,
                  rate=decision.rate / 100.0)
        return decision.rate / 100.0, decision.reason

    def _slice_volume(self, symbol: str, bar_volume: float) -> Tuple[float, str]:
        """Volume the POV applies to. Buyer mode: last bar volume x buyer share of volume today.
        No share known -> the full bar volume (logged). Other modes: unchanged."""
        if self.cfg.pov_mode not in ("buyer", "band_buyer", "peg_band_buyer"):
            return bar_volume, "volume"
        share = self._buyer_share_provider(symbol) if self._buyer_share_provider is not None else None
        if share is None:
            self._log("pov_buyer_fallback", symbol=symbol, reason="no buyer share yet")
            return bar_volume, "fallback"
        return bar_volume * share, "buyer"

    def _peg_on(self, p: "_Parent") -> bool:
        """Peg applies to BUY entries only. Exits stay MARKET so they always try to fill."""
        return self.cfg.pov_mode == "peg_band_buyer" and p.intent.kind == "entry" and p.intent.side == "BUY"

    def _peg_price_now(self, symbol: str, cap: Optional[float]) -> Optional[Tuple[float, int]]:
        """(best bid price, shares at that bid), never above cap. None if unavailable (caller uses the fixed path)."""
        if self._depth_provider is None:
            return None
        try:
            depth = self._depth_provider(symbol)
        except Exception as exc:  # noqa: BLE001 - a quote problem must not stop the bot
            self._log("peg_depth_error", symbol=symbol, error=repr(exc))
            return None
        return peg_level("BUY", depth, cap, self.cfg.peg_tick_size)

    def _submit_peg(self, p: "_Parent", qty: int, px: float, cap: Optional[float]):
        """One child as a LIMIT at the best bid. If it is not filled and was cancelled cleanly, reprice
        from the current bid, up to peg_max_attempts. A partial fill or a failed cancel stops at once."""
        outcome = None
        child = None
        qty_cap = qty
        for attempt in range(self.cfg.peg_max_attempts):
            if attempt > 0:
                level = self._peg_price_now(p.intent.symbol, cap)
                if level is None:
                    break
                px, bid_qty = level
                qty = min(qty_cap, int(bid_qty * self.cfg.peg_bid_fraction))
                if qty < 1:
                    break
                p.seq += 1
                self._log("peg_reprice", key=p.key, attempt=attempt + 1, px=px)
            child = PovExecutor.make_child(p.intent, qty, p.seq)
            outcome = self._executor.submit_child(child, "LIMIT", px)
            if not outcome.sent:
                break
            resp = outcome.response or {}
            if str(resp.get("status", "")).lower() == "success":
                break
            if resp.get("partial") or resp.get("cancel_failed"):
                break
            if "limit not filled" not in str(resp.get("reason", "")):
                break
        return outcome, child

    def _slice_parents(self, bars_cache: Dict[str, Any], now: datetime, report: Dict[str, Any]) -> None:
        for key, p in list(self._parents.items()):
            if self._faults:
                report["skipped"].append("slicing paused: open fault")
                break  # never add exposure while a stop or a fill is unresolved
            p.cycles += 1
            if p.remaining <= 0 or p.cycles > self.cfg.max_cycles:
                self._close(key, "fully sliced" if p.remaining <= 0 else "max cycles reached")
                continue
            df = bars_cache.get(p.intent.symbol)
            if df is None:
                df = self._bars.bars(p.intent.symbol, now)
            if df is None:
                report["skipped"].append(f"{p.intent.symbol}: no bars for slice")
                continue
            if p.intent.kind == "entry":
                pov, why = self._pov_for(p.intent.symbol, df, p.intent.side)
            else:
                pov, why = self.cfg.pov, "exit_fixed"  # exits are never banded, bought-share sized or halted
            if pov <= 0:
                self._log("pov_band_halt", key=key, reason=why)
                continue
            raw_volume = float(df["volume"].iloc[-2])  # last closed bar
            if p.intent.kind == "entry":
                bar_volume, _vol_why = self._slice_volume(p.intent.symbol, raw_volume)
            else:
                bar_volume, _vol_why = raw_volume, "exit_fixed"
            peg_px = None
            peg_bid_qty = 0
            cap = None
            if self._peg_on(p):
                cap = self._entry_price(p, df)[1]
                level = self._peg_price_now(p.intent.symbol, cap)
                peg_px = level[0] if level is not None else None
                peg_bid_qty = level[1] if level is not None else 0
                if why == "fallback" or _vol_why == "fallback" or peg_px is None:
                    # Single fallback for peg mode: fixed POV on full bar volume, old entry price.
                    self._log("peg_fallback_fixed", key=key, band=why, volume=_vol_why, peg=peg_px is not None)
                    pov = self.cfg.pov
                    bar_volume = raw_volume
                    peg_px = None
            qty = self._executor.child_qty(p.remaining, bar_volume, pov)
            if qty < 1:
                self._log("child_zero", key=key, remaining=p.remaining, bar_volume=bar_volume)
                continue
            if peg_px is not None:
                qty = min(qty, int(peg_bid_qty * self.cfg.peg_bid_fraction))  # best-bid size cap
                if qty < 1:
                    self._log("peg_thin_bid", key=key, bid_qty=peg_bid_qty)
                    continue
            p.seq += 1
            try:
                if peg_px is not None:
                    outcome, child = self._submit_peg(p, qty, peg_px, cap)
                else:
                    child = PovExecutor.make_child(p.intent, qty, p.seq)
                    price_type, limit_px = self._entry_price(p, df)
                    outcome = self._executor.submit_child(child, price_type, limit_px)
            except Exception as exc:
                self._close(key, f"child error: {exc!r}")
                continue
            if not outcome.sent:
                self._log("child_not_sent", key=key, reasons=list(outcome.reasons))
                continue
            resp = outcome.response or {}
            status = str(resp.get("status", "")).lower()
            if peg_px is not None and status != "success" and "limit not filled" in str(resp.get("reason", "")) \
                    and not resp.get("partial") and not resp.get("cancel_failed"):
                self._log("peg_unfilled", key=key)  # cancelled cleanly; parent stays open for the next cycle
                continue
            if status != "success":
                if resp.get("partial"):
                    self._faults.add(("partial", key))
                    self._alert("CRITICAL", f"{p.intent.symbol}: partial entry fill; position not booked; "
                                            "entries blocked until reconciled with the broker")
                if resp.get("cancel_failed"):
                    self._alert("CRITICAL", f"{p.intent.symbol}: unfilled entry could not be cancelled; check the broker")
                self._close(key, f"order reply not success: {status or 'missing'}; parent halted")
                continue
            p.remaining -= qty
            added = qty * p.intent.ref_price  # sent counts as exposure (conservative)
            self.state.open_notional += added
            self._booked[key] = self._booked.get(key, 0.0) + added  # released by the same amount on exit
            self._record_fill(p, qty, outcome.response or {})
            report["children_sent"] += 1

    def _record_fill(self, p: _Parent, qty: int, response: Dict[str, Any]) -> None:
        """First accepted child opens the position. Later children add quantity.

        Entry is the volume-weighted average fill price. It drives breakeven, the risk unit R and P&L."""
        avg = response.get("avg_price")
        fill = float(avg) if isinstance(avg, (int, float)) and avg > 0 else float(p.intent.ref_price)
        pos = self._positions.get(p.key)
        if pos is None:
            self._positions[p.key] = Position(
                key=p.key, symbol=p.intent.symbol, side=p.intent.side, qty=qty, entry=fill,
                stop=p.stop, target=p.target, atr=p.atr, adv_shares=p.adv_shares,
                prev_close=p.prev_close, circuit_pct=p.circuit_pct, strategy=p.strategy,
            )
            self._log("position_open", key=p.key, qty=qty, entry=fill, stop=p.stop, target=p.target)
        else:
            self._reverse_mark(p.key, pos)  # the old mark used the old average entry
            pos.entry = (pos.entry * pos.qty + fill * qty) / (pos.qty + qty)
            pos.qty += qty
            self._log("position_add", key=p.key, qty=qty, total=pos.qty, avg_entry=round(pos.entry, 4))
        self._sync_broker_stop(p.key)
        self._persist_positions()

    def _remember_bar(self, sym: str, df) -> Dict[str, Any]:
        """Store the last closed bar of a symbol, with the cycle it was seen in. Used only when the feed is down."""
        bar = {"open": float(df["open"].iloc[-2]), "high": float(df["high"].iloc[-2]),
               "low": float(df["low"].iloc[-2]), "close": float(df["close"].iloc[-2]),
               "time": str(df.index[-2]), "cycle": self._cycle_no}
        self._last_bar[sym] = bar
        return bar

    def _manage_exits(self, bars_cache: Dict[str, Any], now: datetime, report: Dict[str, Any]) -> None:
        """Check each open position on its last closed bar. Stop, target, breakeven and trail."""
        for key, pos in list(self._positions.items()):
            if pos.exit_halted:
                continue
            df = bars_cache.get(pos.symbol)
            if df is None:
                df = self._bars.bars(pos.symbol, now)
            if df is not None:
                bar = self._remember_bar(pos.symbol, df)
            else:
                # Feed outage: manage the position on the last known closed bar (owner decision 2026-11-10).
                bar = self._last_bar.get(pos.symbol)
                if bar is None:
                    self._log("exit_no_bars", key=key, symbol=pos.symbol, note="no bar ever seen; held")
                    self._alert("WARNING", f"{pos.symbol}: no bar seen; position held")
                    report["skipped"].append(f"{pos.symbol}: no bars to manage position")
                    continue
                age = self._cycle_no - int(bar.get("cycle", self._cycle_no))
                if age > self.cfg.max_outage_cycles:
                    # Old prices are not used for a stop. The position is held and a person is alerted.
                    self._log("exit_held_stale_bar", key=key, symbol=pos.symbol, bar_time=bar["time"],
                              age_cycles=age, max_outage_cycles=self.cfg.max_outage_cycles)
                    self._alert("CRITICAL", f"{pos.symbol}: bar feed down {age} cycles; exit held, check the position")
                    report["skipped"].append(f"{pos.symbol}: feed down too long, exit held")
                    continue
                self._log("exit_used_last_bar", key=key, symbol=pos.symbol, bar_time=bar["time"], age_cycles=age)
                report["skipped"].append(f"{pos.symbol}: feed down, managed on last known bar")
            if pos.last_bar_time == bar["time"]:
                continue  # this bar was already evaluated for this position
            pos.last_bar_time = bar["time"]
            old_stop = pos.stop
            price, reason = evaluate_bar(pos, bar["open"], bar["high"], bar["low"],
                                         self.cfg.exit_config_for(pos.strategy))
            if price is None:
                if pos.stop != old_stop:
                    self._log("stop_moved", key=key, old=old_stop, new=pos.stop, be=pos.be_done)
                    # The trail or breakeven moved the bot's stop: the broker stop must follow it.
                    self._sync_broker_stop(key)
                continue
            self._send_exit(key, pos, price, reason, now, report)

    def _send_exit(self, key: str, pos: Position, price: float, reason: str,
                   now: datetime, report: Dict[str, Any]) -> None:
        if pos.broker_stop_id:
            # The broker stop must go before the bot sells, or the two could both fill.
            res = self._cancel_stop(key, pos)
            if res == "closed":
                return  # the broker stop had already filled: the position is closed
            if res == "failed":
                self._log("exit_held_stop_cancel", key=key, reason=reason)
                self._alert("CRITICAL", f"{pos.symbol}: broker stop could not be cancelled; exit held, check the broker")
                report["skipped"].append(f"{pos.symbol}: stop cancel unclear, exit held")
                return
        intent = TradeIntent(
            intent_id=f"{key}-EXIT", strategy_id=pos.strategy, symbol=pos.symbol,
            side=pos.exit_side, qty=pos.qty, ref_price=float(price), kind="exit",
            prev_close=pos.prev_close, circuit_pct=pos.circuit_pct, adv_shares=pos.adv_shares,
            trade_day=now.date(),
        )
        self._exit_held_qty = pos.qty  # the gate refuses a SELL larger than this
        try:
            outcome = self._executor.submit_child(intent)
        except Exception as exc:
            pos.exit_halted = True
            self._restore_stop_fault(key, pos)
            self._alert("CRITICAL", f"{pos.symbol}: exit error, position halted: {exc!r}")
            self._log("exit_halted", key=key, error=repr(exc))
            return
        finally:
            self._exit_held_qty = None
        if not outcome.sent:
            self._restore_stop_fault(key, pos)
            self._log("exit_not_sent", key=key, reason=reason, reasons=list(outcome.reasons))
            self._alert("WARNING", f"{pos.symbol}: exit not sent ({reason}); will retry on next bar")
            return
        response = outcome.response or {}
        status = str(response.get("status", "")).lower()
        if status != "success":
            pos.exit_halted = True
            self._restore_stop_fault(key, pos)
            self._alert("CRITICAL", f"{pos.symbol}: exit reply unclear, position halted; check the broker")
            self._log("exit_halted", key=key, status=status or "missing",
                      note="exit reply unclear; not resent; check the broker")
            return
        fill = response.get("avg_price")
        fill_px = float(fill) if isinstance(fill, (int, float)) and fill > 0 else float(price)
        self._finish_exit(key, pos, fill_px, reason, price)
        report["exits_sent"] = report.get("exits_sent", 0) + 1

    def _finish_exit(self, key: str, pos: Position, fill_px: float, reason: str, price: float) -> None:
        """A position is closed (by the bot's exit or by its broker stop): books, P&L, parent, save."""
        self._positions.pop(key, None)
        # The mark is part of daily P&L until now. Reverse it, so the realized P&L below replaces it.
        self._reverse_mark(key, pos)
        self._close(key, f"position exited ({reason}); parent closed")
        booked = self._booked.pop(key, pos.qty * pos.entry)
        self.state.open_notional = max(0.0, self.state.open_notional - booked)
        sign = 1.0 if pos.side == "BUY" else -1.0
        self._record_pnl(pos.strategy, sign * (fill_px - pos.entry) * pos.qty)
        self._faults.discard(("stop", key))
        self._log("exit_sent", key=key, reason=reason, qty=pos.qty, price=price, fill=fill_px)
        self._persist_positions()

    def _restore_stop_fault(self, key: str, pos: Position) -> None:
        """A failed exit may have cancelled the broker stop. If so, the stop must be placed again."""
        if not pos.broker_stop_id:
            self._faults.add(("stop", key))

    def _entry_price(self, p: _Parent, df) -> Tuple[str, Optional[float]]:
        """Entries are LIMIT orders a set band above the last close. Exits are never limited."""
        if p.intent.kind != "entry" or p.intent.side != "BUY":
            return "MARKET", None
        last_close = float(df["close"].iloc[-2])
        band = self.cfg.entry_max_slippage_bps / 10_000
        return "LIMIT", round(last_close * (1 + band), 2)

    def _run_entry_guard(self) -> bool:
        """Per-cycle guard (live: egress IP). A refusal blocks new entries only; exits still run."""
        if self._entry_guard is None:
            return True
        try:
            ok, reasons = self._entry_guard()
        except Exception as exc:
            ok, reasons = False, [f"entry guard error: {exc!r}"]
        if not ok:
            self._log("entry_guard_refused", reasons=list(reasons))
            self._alert("CRITICAL", "new entries blocked: " + "; ".join(reasons))
        return bool(ok)

    def _persist_positions(self) -> None:
        """Save the open book (positions, stop ids, exposure, marks, parents done today)."""
        if self._pos_store is None:
            return
        from dataclasses import fields as _fields
        positions = [{f.name: getattr(pos, f.name) for f in _fields(pos) if f.init}
                     for pos in self._positions.values()]
        self._pos_store.save({
            "day": self._pnl_day.isoformat() if self._pnl_day else None,
            "open_notional": round(float(self.state.open_notional), 2),
            "booked": {k: round(float(v), 2) for k, v in self._booked.items()},
            "marks": {k: round(float(v), 2) for k, v in self._marks.items()},
            "done": sorted(self._done),
            "positions": positions,
        })

    def restore_positions(self, now: datetime) -> int:
        """Load the saved book. Call once at start, before run_startup_recon. Returns positions restored."""
        if self._pos_store is None:
            return 0
        doc = self._pos_store.load()
        if doc is None:
            return 0
        same_day = doc.get("day") == now.date().isoformat()
        restored: Dict[str, Position] = {}
        for d in doc.get("positions", []):
            try:
                pos = Position(**d)
            except (TypeError, ValueError) as exc:
                self._log("position_restore_refused", error=repr(exc))
                self._alert("CRITICAL", "a saved position could not be read; check the broker before entries")
                self._faults.add(("partial", "restore"))
                continue
            restored[pos.key] = pos
        self._positions = restored
        self.state.open_notional = float(doc.get("open_notional", 0.0))
        self._booked = {str(k): float(v) for k, v in doc.get("booked", {}).items()}
        if same_day:
            self._marks = {str(k): float(v) for k, v in doc.get("marks", {}).items()}
            self._done = set(str(k) for k in doc.get("done", []))
            self._restored_day = now.date()
        for key, pos in restored.items():
            if not pos.broker_stop_id:
                self._faults.add(("stop", key))  # no broker stop on record: place one before anything else
        self._log("positions_restored", count=len(restored), same_day=same_day)
        return len(restored)

    def _sync_broker_stop(self, key: str) -> None:
        """Make the broker stop cover the position's full quantity at the bot's stop price."""
        if self._stops is None:
            return
        pos = self._positions.get(key)
        if pos is None:
            return
        if (pos.broker_stop_id and pos.broker_stop_qty == pos.qty
                and round(pos.broker_stop_trigger, 2) == round(pos.stop, 2)):
            self._faults.discard(("stop", key))
            return
        if pos.broker_stop_id:
            res = self._cancel_stop(key, pos)
            if res == "closed":
                return
            if res == "failed":
                self._faults.add(("stop", key))
                self._alert("CRITICAL", f"{pos.symbol}: cannot replace the broker stop; new entries blocked")
                return
        exit_side = "SELL" if pos.side == "BUY" else "BUY"
        try:
            res = self._stops.place_stop(pos.symbol, exit_side, pos.qty, pos.stop, pos.strategy)
        except Exception as exc:
            res = {"status": "error", "reason": repr(exc)}
        order_id = str(res.get("orderid", "")) if str(res.get("status", "")).lower() == "success" else ""
        if order_id:
            pos.broker_stop_id = order_id
            pos.broker_stop_qty = pos.qty
            pos.broker_stop_trigger = round(pos.stop, 2)
            self._faults.discard(("stop", key))
            self._log("stop_placed", key=key, order_id=order_id, qty=pos.qty, trigger=pos.stop)
        else:
            self._faults.add(("stop", key))
            self._alert("CRITICAL", f"{pos.symbol}: broker stop not placed ({res.get('reason', 'no order id')}); "
                                    "new entries blocked; bot-side stop still active")
        self._persist_positions()

    def _cancel_stop(self, key: str, pos: Position) -> str:
        """Cancel the broker stop. Returns 'cancelled', 'closed' (it had already filled) or 'failed'."""
        if self._stops is None or not pos.broker_stop_id:
            return "cancelled"
        res = self._stops.cancel_stop(pos.broker_stop_id, pos.strategy)
        if str(res.get("status", "")).lower() == "success":
            self._log("stop_cancelled", key=key, order_id=pos.broker_stop_id)
            pos.broker_stop_id = ""
            pos.broker_stop_qty = 0
            self._persist_positions()
            return "cancelled"
        st = self._stops.stop_status(pos.broker_stop_id, pos.strategy)
        avg = st.get("avg_price")
        if str(st.get("order_status", "")).lower() == "complete" and isinstance(avg, (int, float)) and avg > 0:
            self._alert("WARNING", f"{pos.symbol}: broker stop had already filled at {avg}")
            self._finish_exit(key, pos, float(avg), "broker stop filled", float(avg))
            return "closed"
        self._log("stop_cancel_failed", key=key, order_id=pos.broker_stop_id, reason=res.get("reason"))
        return "failed"

    def _check_broker_stops(self) -> None:
        """Each cycle: a broker stop that filled closes its position; a rejected one is placed again."""
        if self._stops is None:
            return
        for key, pos in list(self._positions.items()):
            if not pos.broker_stop_id:
                continue
            try:
                st = self._stops.stop_status(pos.broker_stop_id, pos.strategy)
            except Exception as exc:
                self._log("stop_status_error", key=key, error=repr(exc))
                continue
            state = str(st.get("order_status", "")).lower()
            avg = st.get("avg_price")
            if state == "complete" and isinstance(avg, (int, float)) and avg > 0:
                self._alert("WARNING", f"{pos.symbol}: broker stop filled at {avg}")
                self._finish_exit(key, pos, float(avg), "broker stop filled", float(avg))
            elif state in ("rejected", "cancelled", "canceled"):
                self._alert("CRITICAL", f"{pos.symbol}: broker stop {state}; placing it again")
                pos.broker_stop_id = ""
                pos.broker_stop_qty = 0
                self._faults.add(("stop", key))
                self._persist_positions()

    def _resync_stops(self) -> None:
        """Place any missing broker stop again (a fault from an earlier cycle)."""
        for kind, key in list(self._faults):
            if kind != "stop":
                continue
            if key not in self._positions:
                self._faults.discard((kind, key))
                continue
            self._sync_broker_stop(key)

    def _apply_mark_delta(self, strategy: str, delta: float) -> None:
        """Add a mark-to-market change to the daily and per-strategy counters."""
        self.state.daily_pnl += delta
        self.state.strategy_pnl[strategy] = self.state.strategy_pnl.get(strategy, 0.0) + delta

    def _reverse_mark(self, key: str, pos: Position) -> None:
        """Take back the mark a position added, so its realized P&L can replace it."""
        old = self._marks.pop(key, 0.0)
        if old:
            self._apply_mark_delta(pos.strategy, -old)

    def _close(self, key: str, reason: str) -> None:
        self._parents.pop(key, None)
        self._log("parent_closed", key=key, reason=reason)


def run_loop(bot: StrategyBot, *, clock: Callable[[], datetime], sleep: Callable[[float], None],
             max_cycles: Optional[int] = None) -> int:
    """Run cycles every interval_minutes. An error in one cycle is logged and the loop goes on."""
    done = 0
    while max_cycles is None or done < max_cycles:
        try:
            bot.cycle(clock())
        except BotRefused:
            raise
        except Exception as exc:
            bot._log("cycle_error", error=repr(exc))
        done += 1
        if max_cycles is None or done < max_cycles:
            sleep(bot.cfg.interval_minutes * 60)
    return done
