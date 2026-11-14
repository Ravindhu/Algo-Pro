"""5-minute strategy bot: screened symbols -> strategies -> risk gate -> POV child orders.

Flow stage: Algo router, Risk gate, Approved order queue, Audit log, Strategy bot framework
Status: PHASE 3 IMPLEMENTED (paper and analyze/sandbox only; live mode is refused in code)

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
from typing import Any, Callable, Dict, List, Optional

import yaml

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.execution.pnl_store import PnlStore
from quant.execution.pov_executor import PovExecutor
from quant.monitor.broker_recon import ReconGate, startup_reconcile
from quant.monitor.feed_health import FeedHealthBook
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch
from quant.risk.pre_gate import PreGate, PreGateConfig
from quant.execution.exit_manager import ExitConfig, Position, evaluate_bar
from quant.strategies.signals.risk_adapter import gate_signal, levels_from_signal

ALLOWED_MODES = ("paper", "analyze")


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

    def place_child(self, intent, api_key, decision, price_type="MARKET", limit_price=None):
        """Place, then confirm the fill with the order status. Only 'complete' counts as filled."""
        placed = self._client.place_child(intent, api_key, decision, price_type, limit_price)
        if str(placed.get("status", "")).lower() != "success":
            return placed
        order_id = str(placed.get("orderid", ""))
        if not order_id:
            return {"status": "error", "orderid": "", "reason": "no order id; fill not confirmed"}
        st = self._client.get_order_status(api_key, order_id, intent.strategy_id)
        data = st.get("data") if isinstance(st, dict) and isinstance(st.get("data"), dict) else {}
        state = str(data.get("order_status", "")).lower()
        avg = data.get("average_price")
        if state == "complete" and isinstance(avg, (int, float)) and avg > 0:
            return {"status": "success", "orderid": order_id, "avg_price": float(avg),
                    "filled_qty": int(intent.qty)}
        return {"status": "error", "orderid": order_id, "reason": f"fill not confirmed: {state or 'no status'}"}


class BotRefused(RuntimeError):
    """Raised for a configuration the bot must never run with (for example live mode)."""


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
    max_screen_age_minutes: float = 10.0
    screen_path: str = "data/screened/latest.json"
    data_sources_path: str = "config/data_sources.yaml"
    strategies: tuple = field(default_factory=lambda: ("engulfing",))

    @classmethod
    def from_yaml(cls, path) -> "BotConfig":
        data: Dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        names = set(cls.__dataclass_fields__)
        unknown = set(data) - names
        if unknown:
            raise ValueError(f"unknown bot keys: {sorted(unknown)}")
        if "strategies" in data:
            data["strategies"] = tuple(data["strategies"])
        return cls(**data)


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
    ) -> None:
        if cfg.live_enabled:
            raise BotRefused("live_enabled must stay false for the bot in this phase")
        if cfg.mode not in ALLOWED_MODES:
            raise BotRefused(f"bot refuses mode {cfg.mode!r}; only {ALLOWED_MODES} is allowed")
        if getattr(sender, "bot_mode", None) != cfg.mode:
            raise BotRefused(f"sender is for {getattr(sender, 'bot_mode', None)!r} but bot mode is {cfg.mode!r}")
        if not api_key:
            raise ValueError("bot needs an API key")
        self.cfg = cfg
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
        self._cycle_no = 0                        # cycle counter, used to age the last known bar
        self._pnl_day = None                      # day the realized P&L counters belong to
        self._pnl_store = pnl_store               # persists counters across restarts; None = in-memory only
        self._exit_held_qty: Optional[int] = None  # held qty of the exit being sent (read by _child_gate)
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
        self._pnl_day = now.date()
        self.state.daily_pnl = 0.0
        self.state.strategy_pnl = {}
        self._restore_counters(now)
        self._done.clear()
        self._log("pnl_day_reset", day=now.date().isoformat())

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
        entries_ok = self._entries_allowed()
        if not entries_ok:
            self._log("entries_blocked", reason="startup recon not allowed entries",
                      status=getattr(self.recon, "status", "not run"))
            report["skipped"].append("entries blocked: startup recon")
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
        """Delivery-style mark-to-market: unrealized P&L of open positions joins the daily
        and per-strategy counters on every cycle, so a falling book trips the daily loss
        limit BEFORE the stop realizes the loss (AGENTS gap, fixed 2026-11-11).

        Only the DELTA from the previous mark is added; realized exits keep working the same
        because a realized exit removes the position (its last mark is re-based on close).
        """
        if not self._positions:
            return
        marks: Dict[str, float] = {}
        for key, pos in self._positions.items():
            df = bars_cache.get(pos.symbol)
            if df is None:
                continue  # no fresh mark without bars; keep the previous mark
            px = float(df["close"].iloc[-2])  # last closed bar
            sign = 1.0 if pos.side == "BUY" else -1.0
            marks[key] = sign * (px - pos.entry) * pos.qty
        # Replace the unrealized component wholesale (delta-only adds would drift).
        unrealized_now = sum(marks.values())
        marked_before = getattr(self, "_mtm_unrealized", 0.0)
        delta = unrealized_now - marked_before
        if abs(delta) < 0.005:
            return
        self._mtm_unrealized = unrealized_now
        self.state.daily_pnl += delta
        # Spread the delta across each position's strategy counter.
        for sym_key, m in marks.items():
            strategy = self._positions[sym_key].strategy
            self.state.strategy_pnl[strategy] = self.state.strategy_pnl.get(strategy, 0.0) + delta
        self._log("mtm_update", unrealized=round(unrealized_now, 2), delta=round(delta, 2))
        limit = self._limits.max_daily_loss
        if limit is not None and self.state.daily_pnl <= -abs(float(limit)) and not self._kill.active:
            self._kill.trip(f"daily loss incl. unrealized {self.state.daily_pnl:.2f} "
                            f"reached limit {abs(float(limit)):.2f}", source="automatic")
            self._alert("CRITICAL", "daily loss limit (mark-to-market) reached: kill switch on")

    def _safe_funds(self) -> float:
        try:
            value = float(self._read_funds())
            return value if value > 0 else 0.0
        except Exception:
            return 0.0  # no funds known: entries are refused

    def _scan(self, strat, sym, df, circuit_pct, stats, now, report) -> None:
        try:
            sig = strat.signal(df)
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

    def _slice_parents(self, bars_cache: Dict[str, Any], now: datetime, report: Dict[str, Any]) -> None:
        for key, p in list(self._parents.items()):
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
            bar_volume = float(df["volume"].iloc[-2])  # last closed bar
            qty = self._executor.child_qty(p.remaining, bar_volume)
            if qty < 1:
                self._log("child_zero", key=key, remaining=p.remaining, bar_volume=bar_volume)
                continue
            p.seq += 1
            child = PovExecutor.make_child(p.intent, qty, p.seq)
            try:
                outcome = self._executor.submit_child(child)
            except Exception as exc:
                self._close(key, f"child error: {exc!r}")
                continue
            if not outcome.sent:
                self._log("child_not_sent", key=key, reasons=list(outcome.reasons))
                continue
            status = str((outcome.response or {}).get("status", "")).lower()
            if status != "success":
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
            pos.entry = (pos.entry * pos.qty + fill * qty) / (pos.qty + qty)
            pos.qty += qty
            self._log("position_add", key=p.key, qty=qty, total=pos.qty, avg_entry=round(pos.entry, 4))

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
            price, reason = evaluate_bar(pos, bar["open"], bar["high"], bar["low"], self._exit_cfg)
            if price is None:
                if pos.stop != old_stop:
                    self._log("stop_moved", key=key, old=old_stop, new=pos.stop, be=pos.be_done)
                continue
            self._send_exit(key, pos, price, reason, now, report)

    def _send_exit(self, key: str, pos: Position, price: float, reason: str,
                   now: datetime, report: Dict[str, Any]) -> None:
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
            self._alert("CRITICAL", f"{pos.symbol}: exit error, position halted: {exc!r}")
            self._log("exit_halted", key=key, error=repr(exc))
            return
        finally:
            self._exit_held_qty = None
        if not outcome.sent:
            self._log("exit_not_sent", key=key, reason=reason, reasons=list(outcome.reasons))
            self._alert("WARNING", f"{pos.symbol}: exit not sent ({reason}); will retry on next bar")
            return
        response = outcome.response or {}
        status = str(response.get("status", "")).lower()
        if status != "success":
            pos.exit_halted = True
            self._alert("CRITICAL", f"{pos.symbol}: exit reply unclear, position halted; check the broker")
            self._log("exit_halted", key=key, status=status or "missing",
                      note="exit reply unclear; not resent; check the broker")
            return
        # Close the position and its parent. A parent still slicing must not buy again after a stop-out.
        self._positions.pop(key, None)
        # Drop this position's mark: it is realized now, so the next mark-to-market must not
        # compare against a stale unrealized value (otherwise the realized P&L is double-counted).
        self._mtm_unrealized = getattr(self, "_mtm_unrealized", 0.0) - self._position_mark(key, pos)
        self._close(key, f"position exited ({reason}); parent closed")
        booked = self._booked.pop(key, pos.qty * pos.entry)
        self.state.open_notional = max(0.0, self.state.open_notional - booked)
        fill = response.get("avg_price")
        fill_px = float(fill) if isinstance(fill, (int, float)) and fill > 0 else float(price)
        sign = 1.0 if pos.side == "BUY" else -1.0
        self._record_pnl(pos.strategy, sign * (fill_px - pos.entry) * pos.qty)
        self._log("exit_sent", key=key, reason=reason, qty=pos.qty, price=price, fill=fill_px)
        report["exits_sent"] = report.get("exits_sent", 0) + 1

    def _position_mark(self, key: str, pos: Position) -> float:
        """Last closed-bar mark used by the exit, or 0 when the symbol has no cached bar."""
        bar = self._last_bar.get(pos.symbol)
        if bar is None:
            return 0.0
        sign = 1.0 if pos.side == "BUY" else -1.0
        return sign * (float(bar.get("close", pos.entry)) - pos.entry) * pos.qty

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
