"""5-minute strategy bot: screened symbols -> strategies -> risk gate -> POV child orders.

Flow stage: Algo router, Risk gate, Approved order queue, Audit log
Status: PHASE 3 IMPLEMENTED (paper and analyze/sandbox only; live mode is refused in code)

One cycle (run every interval_minutes):
  1. Mode is "paper" (simulated fills) or "analyze" (OpenAlgo sandbox); live is refused. Skip the cycle unless the live data source is approved in
     config/data_sources.yaml (live_allowed: true). Both checks are fail closed.
  2. Read the screen file. Keep only fresh, selected symbols with a known circuit limit and no
     ASM/GSM flag. Anything missing or unreadable means no symbols.
  3. For each symbol: read closed bars and daily stats (ADV, previous close). No data means skip.
  4. Run each strategy's signal(df). An approved signal becomes a parent plan (full quantity
     checked by the gate). A parent is stored once, however many cycles it appears.
  5. Slice every open parent: child qty = pov share of the last closed bar's volume, never more
     than what is left. Each child goes through the POV executor (gate, queue, client).
  6. A parent is closed when fully sliced, when it is older than max_cycles, or when a child
     order gets a non-success reply (then the parent halts; it is never resent blindly).

Every decision is written to the audit log. The bot never builds an order client. The caller
injects the sender, so this module cannot place an order by itself.
Exits, stop-loss and take-profit monitoring are NOT in this step (see the flow doc).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.execution.pov_executor import PovExecutor
from quant.monitor.broker_recon import ReconGate, startup_reconcile
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch
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
        return self._client.place_child(intent, api_key, decision, price_type, limit_price)


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
        self._executor = PovExecutor(
            gate=self._child_gate, sender=sender, api_key=api_key, audit=audit, pov=cfg.pov,
        )

    # --- gate used by the POV executor for every child ---
    def _child_gate(self, intent: TradeIntent):
        return check_intent(
            intent, self.state, self._limits, self._kill, self._safe,
            available_funds=self._funds, halt=self._halt,
        )

    def _log(self, kind: str, **payload: Any) -> None:
        self._audit.append(kind, payload)

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
        if not live_feed_approved(self.cfg.data_sources_path):
            self._log("cycle_skipped", reason="live data source not approved (data_sources.yaml)")
            report["skipped"].append("data source not approved")
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
                report["skipped"].append(f"{sym}: no fresh bars")
                continue
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
        return report

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
        res = gate_signal(
            sig, symbol=sym, strategy_id=strat.name, equity=self._funds,
            risk_fraction=self.cfg.risk_per_trade, state=self.state, limits=self._limits,
            kill=self._kill, safe=self._safe, halt=self._halt, available_funds=self._funds,
            adv_shares=stats["adv_shares"], prev_close=stats["prev_close"],
            circuit_pct=circuit_pct, asm_gsm=False, trade_day=now.date(),
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
            self.state.open_notional += qty * p.intent.ref_price  # sent counts as exposure (conservative)
            self._record_fill(p, qty, outcome.response or {})
            report["children_sent"] += 1

    def _record_fill(self, p: _Parent, qty: int, response: Dict[str, Any]) -> None:
        """First accepted child opens the position. Later children add quantity only.

        Entry price is the first fill price (used for breakeven and the risk unit R)."""
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
            pos.qty += qty
            self._log("position_add", key=p.key, qty=qty, total=pos.qty)

    def _remember_bar(self, sym: str, df) -> Dict[str, Any]:
        """Store the last closed bar of a symbol. Used only when the feed is down."""
        bar = {"open": float(df["open"].iloc[-2]), "high": float(df["high"].iloc[-2]),
               "low": float(df["low"].iloc[-2]), "time": str(df.index[-2])}
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
                    report["skipped"].append(f"{pos.symbol}: no bars to manage position")
                    continue
                self._log("exit_used_last_bar", key=key, symbol=pos.symbol, bar_time=bar["time"])
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
        try:
            outcome = self._executor.submit_child(intent)
        except Exception as exc:
            pos.exit_halted = True
            self._log("exit_halted", key=key, error=repr(exc))
            return
        if not outcome.sent:
            self._log("exit_not_sent", key=key, reason=reason, reasons=list(outcome.reasons))
            return
        status = str((outcome.response or {}).get("status", "")).lower()
        if status != "success":
            pos.exit_halted = True
            self._log("exit_halted", key=key, status=status or "missing",
                      note="exit reply unclear; not resent; check the broker")
            return
        self._positions.pop(key, None)
        self.state.open_notional = max(0.0, self.state.open_notional - pos.qty * pos.entry)
        self._log("exit_sent", key=key, reason=reason, qty=pos.qty, price=price)
        report["exits_sent"] = report.get("exits_sent", 0) + 1

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
