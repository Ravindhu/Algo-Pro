"""Live runner for the strategy bot. Refuses to start unless every live gate passes.

Usage (owner only; mode: live + live_enabled: true in config/bot.yaml, and the OpenAlgo
analyzer switched OFF; while it is on, no live order is sent):
  python scripts/run/run_strategy_bot_live.py --base-url http://127.0.0.1:5000

Gates, in order. Each one must pass before any order can be sent:
  1. config/bot.yaml: mode is "live" and live_enabled is true.
  2. scripts/run/run_live.startup_checks: limits complete, kill switch off, holiday file parses,
     API key present, live.max_capital_inr set. (No LiveApproval since 2026-11-25.)
  3. StrategyBot start: the same gate is run again inside the bot.
  4. OpenAlgo funds reply carries a numeric availablecash (audit fix 7, 2026-11-26).
  5. Startup reconciliation with the broker position book. No match = no entries.
  6. OpenAlgoClient live guard on EVERY order: OpenAlgo analyzer must be OFF, compliance
     gate (audit storage and clock), and an approved GateDecision for the same intent.

Not enforced by code (owner checks, before the analyzer is switched off): the analyze-mode soak,
the kill-switch drill, and the holiday calendar for the trade day.

Stop: create the file <state-dir>/stop.flag (the loop checks it every second).
Exits are never blocked by the startup reconciliation; entries are.

Flow stage: Algo router, Risk gate, Approved order queue, OpenAlgo, Audit log
Status: PHASE 1 IMPLEMENTED (live sender wired behind the gates). NOT RUN on a live account.
  Audit fixes 1, 2, 3, 7 (2026-11-26, branch fix/live-path-audit-2026-11-26): stop moves are
  modified in place, MIS square-off does not need bars, repeated cycle errors alert and block
  entries, funds field is checked at start.
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import math
import pathlib
import sys
from typing import Any, Callable, Dict, Optional

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.audit.log import AuditLog  # noqa: E402
from quant.core.config import RiskLimits  # noqa: E402
from quant.data.live_bars import LiveBars  # noqa: E402
from quant.data.exchange_bars import ExchangeBars  # noqa: E402
from quant.execution.exchange_router import listing_exchange, quote_from_reply  # noqa: E402
from quant.execution.openalgo_client import OpenAlgoClient, http_transport  # noqa: E402
from quant.risk.margin_daily import DailyMarginStore  # noqa: E402
from quant.execution.order_details import OrderDetailStore  # noqa: E402
from quant.execution.readonly_client import OpenAlgoReadOnly  # noqa: E402
from quant.execution.strategy_bot import load_bot_config, BotConfig, BotRefused, AnalyzeSender, LiveSender, StrategyBot  # noqa: E402
from quant.execution.pnl_store import PnlStore  # noqa: E402
from quant.execution.position_store import PositionStore  # noqa: E402
from quant.monitor.broker_recon import make_positionbook_reader  # noqa: E402
from quant.monitor.paper_loop import now_ist  # noqa: E402
from quant.monitor.safe_mode import SafeMode  # noqa: E402
from quant.notify.alerts import Alerts  # noqa: E402
from quant.risk.compliance import ComplianceConfig, ComplianceGate  # noqa: E402
from quant.risk.controls import HaltHandler  # noqa: E402
from quant.risk.gate import RiskState  # noqa: E402
from quant.risk.kill_switch import KillSwitch  # noqa: E402


def _load(name: str, rel_path: str):
    """Load a sibling script as a module. The scripts are not an importable package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_paper = _load("run_strategy_bot_paper", "scripts/run/run_strategy_bot.py")
_live_checks = _load("run_live_checks", "scripts/run/run_live.py")


def funds_reply_has_cash(body: Any) -> bool:
    """True when the funds reply carries a finite number in data.availablecash (a numeric string is accepted)."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or "availablecash" not in data:
        return False
    raw = data.get("availablecash")
    if isinstance(raw, bool):
        return False
    try:
        return math.isfinite(float(raw))
    except (TypeError, ValueError):
        return False


def available_cash(reader: Any, api_key: str) -> float:
    """Sizing equity = available cash from OpenAlgo funds. Any doubt gives 0 (no entries).

    The reply key 'availablecash' matches the vendored OpenAlgo SDK funds response
    (Temp/openalgo/account.py, checked 2026-11-15). A missing key gives 0, so no
    entries. Still confirm it on the live account before the first session.
    """
    try:
        body = reader.funds(api_key)
        data = body.get("data") if isinstance(body, dict) else None
        cash = float(data.get("availablecash", 0)) if isinstance(data, dict) else 0.0
    except Exception:  # noqa: BLE001 - unknown funds means no entries, never a guess
        return 0.0
    return cash if math.isfinite(cash) and cash > 0 else 0.0


class LiveStops:
    """Broker stop adapter for the strategy bot: SL-M place, status and cancel through the live client.

    Every call returns a plain dict with 'status' ('success' or 'error'), and an order id or reason.
    Exceptions are turned into 'error' results, so the bot can fail closed.
    """

    def __init__(self, client, api_key: str) -> None:
        self._client = client
        self._key = api_key

    def place_stop(self, symbol, action, qty, trigger_price, strategy, product="MIS", exchange="NSE"):
        try:
            r = self._client.place_stop(symbol, action, qty, trigger_price, strategy, self._key,
                                        product=product, exchange=exchange)
        except Exception as exc:  # PermissionError (guard) or network: no stop, the bot blocks entries
            return {"status": "error", "reason": repr(exc)}
        order_id = str(r.get("orderid", "")) if isinstance(r, dict) else ""
        ok = isinstance(r, dict) and str(r.get("status", "")).lower() == "success" and bool(order_id)
        return {"status": "success" if ok else "error", "orderid": order_id,
                "reason": "" if ok else str(r.get("message", "no order id")) if isinstance(r, dict) else "bad reply"}

    def modify_stop(self, order_id, symbol, action, qty, trigger_price, strategy, product="MIS", exchange="NSE"):
        """Move the open stop in one broker request. A refusal keeps the old stop in place (never cancelled first)."""
        try:
            r = self._client.modify_stop(order_id, symbol, action, qty, trigger_price, strategy, self._key,
                                         product=product, exchange=exchange)
        except Exception as exc:
            return {"status": "error", "reason": repr(exc)}
        ok = isinstance(r, dict) and str(r.get("status", "")).lower() == "success"
        return {"status": "success" if ok else "error",
                "reason": "" if ok else str(r.get("message", "bad reply")) if isinstance(r, dict) else "bad reply"}

    def stop_status(self, order_id, strategy):
        try:
            r = self._client.get_order_status(self._key, order_id, strategy)
        except Exception as exc:
            return {"order_status": "", "reason": repr(exc)}
        data = r.get("data") if isinstance(r, dict) and isinstance(r.get("data"), dict) else {}
        return {"order_status": str(data.get("order_status", "")).lower(), "avg_price": data.get("average_price")}

    def cancel_stop(self, order_id, strategy):
        try:
            r = self._client.cancel(order_id, strategy, self._key)
        except Exception as exc:
            return {"status": "error", "reason": repr(exc)}
        ok = isinstance(r, dict) and str(r.get("status", "")).lower() == "success"
        return {"status": "success" if ok else "error",
                "reason": str(r.get("message", "")) if isinstance(r, dict) else "bad reply"}


from quant.execution.buyer_vwap import BuyerVwapBook  # noqa: E402
from quant.execution.peg_verify import BidVerifier, VerifyConfig  # noqa: E402


def _live_sender(client, cfg, sandbox: bool = False):
    """LiveSender, or AnalyzeSender for the sandbox run (2026-11-27). In peg mode, LIMIT entries
    wait peg_wait_seconds before the status check."""
    sender = AnalyzeSender(client) if sandbox else LiveSender(client)
    sender.limit_wait_seconds = cfg.peg_wait_seconds if cfg.pov_mode == "peg_band_buyer" else 0.0
    return sender


from quant.execution.strategy_bot import read_screen as _read_screen  # noqa: E402


def build_live_bot(*, bot_cfg_path, limits_path, state_dir, base_url: str, api_key: str,
                   transport: Callable = http_transport, reader=None,
                   startup_fn: Callable = _live_checks.startup_checks,
                   alerts: Optional[Alerts] = None,
                   on_critical: Optional[Callable[[str], None]] = None,
                   settings_path=None, sandbox: bool = False):
    """Wire the live bot. Raises BotRefused unless every start gate passes. Places no orders.

    ``sandbox=True`` (2026-11-27): the same bot, with orders through OpenAlgo's analyzer (sandbox,
    fake money). The mode is set to ``analyze`` IN MEMORY only; config/bot.yaml is not changed. The
    start is refused unless OpenAlgo confirms the analyzer is ON.
    """
    cfg = load_bot_config(bot_cfg_path)
    if sandbox:
        # live_enabled is turned off too: the bot refuses live_enabled outside live mode, and a sandbox
        # run must never carry the live switch.
        cfg = dataclasses.replace(cfg, mode="analyze", live_enabled=False)
    elif cfg.mode != "live" or not cfg.live_enabled:
        raise BotRefused("live runner needs mode: live and live_enabled: true in config/bot.yaml")

    state_dir = pathlib.Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(state_dir / "audit.jsonl")
    kill_path = state_dir / "kill.json"  # the file this bot's KillSwitch uses (2026-11-15 fix)
    limits_path = pathlib.Path(limits_path)  # audit fix 5: the bot re-reads this file every cycle

    # audit fix 5: the bot re-reads the owner's caps every cycle (frozen cfg -> replace)
    cfg = dataclasses.replace(cfg, limits_path=str(limits_path))

    def gate() -> tuple:
        res = startup_fn(limits_path=limits_path, audit=audit, kill_path=kill_path)
        return bool(res.get("ok")), list(res.get("reasons", []))

    # Gate 1+2: run the start checks now.
    checks = startup_fn(limits_path=limits_path, audit=audit, kill_path=kill_path)
    if not checks.get("ok"):
        raise BotRefused("live startup gate refused: " + "; ".join(checks.get("reasons", ["unknown"])))
    cap = checks.get("capital_cap")
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
        raise BotRefused("live startup gate gave no fixed capital cap (live.max_capital_inr)")

    client = OpenAlgoClient(
        base_url, transport, mode="analyze" if sandbox else "live",
        compliance=ComplianceGate(ComplianceConfig(audit_dir=str(state_dir))),
    )
    readonly = reader if reader is not None else OpenAlgoReadOnly(base_url, transport)
    if sandbox and readonly.analyzer_mode(api_key) is not True:
        # Sandbox orders must stay in OpenAlgo's sandbox. Anything but a confirmed ON is refused.
        raise BotRefused("sandbox refused: OpenAlgo analyzer is not confirmed ON")
    # Audit fix 7 (2026-11-26): sizing reads 'availablecash'. A missing or non-numeric field means the bot
    # would silently take no entries, so the start is refused and the owner is told why.
    try:
        funds_ok = funds_reply_has_cash(readonly.funds(api_key))
    except Exception as exc:  # noqa: BLE001 - an unreadable funds reply refuses the start
        raise BotRefused(f"live startup gate refused: OpenAlgo funds could not be read ({exc!r})")
    if not funds_ok:
        raise BotRefused("live startup gate refused: OpenAlgo funds reply has no numeric 'availablecash' "
                         "(check the field name on the live account)")
    alerts = alerts or Alerts(on_critical=on_critical)

    # Buyer-initiated VWAP per screened symbol, from quote polls. Only used in band mode.
    vwap_book = BuyerVwapBook(readonly, api_key, exchange="NSE")
    # Bid-level history for the peg verifier (absorption, pulls, dwell). Sampled on the same tick.
    peg_verifier = BidVerifier(VerifyConfig(
        window_seconds=cfg.peg_verify_window_seconds, dwell_seconds=cfg.peg_dwell_seconds,
        absorb_ratio=cfg.peg_absorb_ratio, max_pull_ratio=cfg.peg_max_pull_ratio))
    def vwap_tick(now):
        symbols = [r["symbol"] for r in _read_screen(cfg.screen_path, now, cfg.max_screen_age_minutes,
                                                     allow_sandbox_band=sandbox)]
        if symbols:
            vwap_book.poll(symbols, now)
            if cfg.pov_mode == "peg_band_buyer":
                for sym in symbols:
                    try:
                        peg_verifier.observe(sym, now.timestamp(),
                                             readonly.depth(api_key, sym, "NSE"),
                                             readonly.quotes(api_key, sym, "NSE"))
                    except Exception as exc:  # noqa: BLE001 - one symbol must not stop the others
                        audit.append("peg_sample_error", {"symbol": sym, "error": repr(exc)})

    tf = _paper.load_timeframes(settings_path or _live_checks.SETTINGS_FILE)
    feed = _paper.MultiTimeframeBars(readonly, api_key, tf) if all(tf.values()) else None
    bot = StrategyBot(
        cfg=cfg,
        vwap_provider=vwap_book.vwap,
        buyer_share_provider=vwap_book.buyer_share,
        depth_provider=lambda sym: readonly.depth(api_key, sym, "NSE"),
        quote_provider=lambda sym, exchange: readonly.quotes(api_key, sym, exchange),  # slice 2: exchange choice
        peg_verifier=peg_verifier,
        vwap_tick=vwap_tick if cfg.pov_mode == "peg_band_buyer" else None,
        strategies=[_paper._make_strategy(n, _paper._strategy_params(bot_cfg_path), feed) for n in cfg.strategies],
        bars=ExchangeBars(
            {"NSE": LiveBars(readonly, api_key),
             "BSE": LiveBars(readonly, api_key, exchange="BSE")},
            listing=lambda sym: listing_exchange(
                quote_from_reply(readonly.quotes(api_key, sym, "NSE")),
                quote_from_reply(readonly.quotes(api_key, sym, "BSE"))),  # slice 4: NSE-listed first
        ),
        sender=_live_sender(client, cfg, sandbox=sandbox),
        audit=audit,
        api_key=api_key,
        # MTF leverage check: the broker's margin reply for the order, read-only (margin_entry.verify_leverage).
        margin_check=lambda intent: readonly.margin_for_order(
            api_key, intent.symbol, "NSE", intent.side, intent.qty, intent.product, "MARKET", intent.ref_price),
        # One broker reading per stock per day, kept in state (margin_daily.py). Cleared each trade day.
        margin_store=DailyMarginStore(state_dir / "margin_daily.json"),
        order_details=OrderDetailStore(state_dir / "order_details.json"),
        # Broker MTF holdings for the daily sync (margin_sync.py); a read error blocks MTF entries.
        mtf_holdings=lambda: readonly.positionbook(api_key),
        # Sizing equity is the fixed live cap, or the broker's cash when that is smaller.
        read_funds=lambda: min(available_cash(readonly, api_key), float(cap)),
        limits=RiskLimits.from_yaml(limits_path),
        kill=KillSwitch(state_dir / "kill.json"),
        safe=SafeMode(audit),
        halt=HaltHandler(),
        state=RiskState(),
        alerts=alerts,
        live_gate=gate,
        # Audit fixes (2026-11-15): daily P&L, open positions and broker stops survive a restart.
        pnl_store=PnlStore(state_dir / "pnl_state.json"),
        position_store=PositionStore(state_dir / "positions.json"),
        stops=LiveStops(client, api_key),
        # No extra per-cycle check: the egress IP check was removed (owner, 2026-11-23).
        # The OpenAlgo analyzer switch is checked on every order (2026-11-25).
        # The bot still requires a guard object; this one always passes. The live gate and
        # OpenAlgoClient's live guard still run on every order.
        entry_guard=lambda: (True, []),
    )
    # Restore the saved book first, then reconcile it with the broker. Entries stay blocked
    # unless the two match. require_broker: no reader means "unavailable", which blocks entries.
    bot.restore_positions(now_ist())
    bot.run_startup_recon(make_positionbook_reader(readonly, api_key), require_broker=True)
    return bot, audit


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default=str(ROOT / "config" / "bot.yaml"))
    p.add_argument("--limits", default=str(ROOT / "config" / "risk_limits.yaml"))
    p.add_argument("--state-dir", default=str(ROOT / "data" / "bot_live"))
    p.add_argument("--base-url", required=True, help="OpenAlgo server URL")
    p.add_argument("--max-cycles", type=int, default=None)
    p.add_argument("--sandbox", action="store_true",
                   help="orders go to OpenAlgo's sandbox (analyzer ON); config mode is not changed")
    args = p.parse_args(argv)

    api_key = OpenAlgoClient.api_key_from_env()
    bot, audit = build_live_bot(
        bot_cfg_path=args.config, limits_path=args.limits, state_dir=args.state_dir,
        base_url=args.base_url, api_key=api_key, sandbox=args.sandbox,
        on_critical=lambda msg: print(f"CRITICAL: {msg}", flush=True),
    )
    # Screen refresh every 5 minutes, inside the loop (starts and stops with the bot).
    from quant.data.screen_refresh import make_screen_refresher, make_screener_scraper
    source = ROOT / "data" / "screened" / "last_screen.json"
    refresh = make_screen_refresher(
        OpenAlgoReadOnly(args.base_url, http_transport), api_key,
        source=source,
        target=ROOT / bot.cfg.screen_path,
        scrape=make_screener_scraper(source=source),
        manual_path=ROOT / "data" / "screened" / "manual_symbols.json",
        mode_path=ROOT / "data" / "screened" / "source_mode.json",
    )
    _paper.run(bot, audit, state_dir=args.state_dir, clock=now_ist, max_cycles=args.max_cycles,
               refresh=refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
