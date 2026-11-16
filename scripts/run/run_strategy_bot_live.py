"""Live runner for the strategy bot. Refuses to start unless every live gate passes.

Usage (owner only; after scripts/ops/approve_live.py and mode: live + live_enabled: true
in config/bot.yaml):
  python scripts/run/run_strategy_bot_live.py --base-url http://127.0.0.1:5000

Gates, in order. Each one must pass before any order can be sent:
  1. config/bot.yaml: mode is "live" and live_enabled is true.
  2. scripts/run/run_live.startup_checks: approval on file, limits hash unchanged,
     limits complete, kill switch off, holiday calendar verified, API key present,
     egress IP (when the owner set allowed_egress_ip).
  3. StrategyBot start: the same gate is run again inside the bot.
  4. Startup reconciliation with the broker position book. No match = no entries.
  5. OpenAlgoClient live guard on EVERY order: approval, limits hash, compliance
     gate (audit storage and clock), and an approved GateDecision for the same intent.

Stop: create the file <state-dir>/stop.flag (the loop checks it every second).
Exits are never blocked by the startup reconciliation; entries are.

Flow stage: Algo router, Risk gate, Approved order queue, OpenAlgo, Audit log
Status: PHASE 1 IMPLEMENTED (live sender wired behind the gates). NOT RUN. Still open:
  owner sign-off, the holiday/calendar check (run_live check 6), kill-switch drill (check 4),
  and the analyze-mode soak test (phase 2).
"""
from __future__ import annotations

import argparse
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
from quant.execution.openalgo_client import OpenAlgoClient, http_transport  # noqa: E402
from quant.execution.readonly_client import OpenAlgoReadOnly  # noqa: E402
from quant.execution.strategy_bot import load_bot_config, BotConfig, BotRefused, LiveSender, StrategyBot  # noqa: E402
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


def available_cash(reader: Any, api_key: str) -> float:
    """Sizing equity = available cash from OpenAlgo funds. Any doubt gives 0 (no entries).

    The reply key 'availablecash' matches the vendored OpenAlgo SDK funds response
    (src/quant/vendor/openalgo/account.py, checked 2026-11-15). A missing key gives 0, so no
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

    def place_stop(self, symbol, action, qty, trigger_price, strategy):
        try:
            r = self._client.place_stop(symbol, action, qty, trigger_price, strategy, self._key)
        except Exception as exc:  # PermissionError (guard) or network: no stop, the bot blocks entries
            return {"status": "error", "reason": repr(exc)}
        order_id = str(r.get("orderid", "")) if isinstance(r, dict) else ""
        ok = isinstance(r, dict) and str(r.get("status", "")).lower() == "success" and bool(order_id)
        return {"status": "success" if ok else "error", "orderid": order_id,
                "reason": "" if ok else str(r.get("message", "no order id")) if isinstance(r, dict) else "bad reply"}

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


def make_egress_guard(allowed_ip: str, egress_fn=None):
    """Per-cycle guard: the machine's public IP must still be the whitelisted one (audit fix 8)."""
    def guard():
        ip = (egress_fn or _live_checks._egress_ip)()
        if ip is None:
            return False, ["egress IP could not be checked"]
        if ip != allowed_ip:
            return False, [f"egress IP {ip} is not the whitelisted {allowed_ip}"]
        return True, []
    return guard


from quant.execution.buyer_vwap import BuyerVwapBook  # noqa: E402
from quant.execution.strategy_bot import read_screen as _read_screen  # noqa: E402


def build_live_bot(*, bot_cfg_path, limits_path, state_dir, base_url: str, api_key: str,
                   transport: Callable = http_transport, reader=None,
                   startup_fn: Callable = _live_checks.startup_checks,
                   alerts: Optional[Alerts] = None,
                   on_critical: Optional[Callable[[str], None]] = None,
                   settings_path=None, egress_fn=None):
    """Wire the live bot. Raises BotRefused unless every start gate passes. Places no orders."""
    cfg = load_bot_config(bot_cfg_path)
    if cfg.mode != "live" or not cfg.live_enabled:
        raise BotRefused("live runner needs mode: live and live_enabled: true in config/bot.yaml")

    state_dir = pathlib.Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(state_dir / "audit.jsonl")
    limits_path = pathlib.Path(limits_path)
    kill_path = state_dir / "kill.json"  # the file this bot's KillSwitch uses (2026-11-15 fix)

    def gate() -> tuple:
        res = startup_fn(limits_path=limits_path, audit=audit, kill_path=kill_path)
        return bool(res.get("ok")), list(res.get("reasons", []))

    # Gate 1+2: run the start checks now. The approval they return is needed for the client.
    checks = startup_fn(limits_path=limits_path, audit=audit, kill_path=kill_path)
    if not checks.get("ok") or checks.get("approval") is None:
        raise BotRefused("live startup gate refused: " + "; ".join(checks.get("reasons", ["no approval"])))
    cap = checks.get("capital_cap")
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
        raise BotRefused("live startup gate gave no fixed capital cap (live.max_capital_inr)")

    client = OpenAlgoClient(
        base_url, transport, mode="live", live_approval=checks["approval"],
        limits_path=limits_path,
        compliance=ComplianceGate(ComplianceConfig(audit_dir=str(state_dir))),
    )
    readonly = reader if reader is not None else OpenAlgoReadOnly(base_url, transport)
    alerts = alerts or Alerts(on_critical=on_critical)
    live_settings = _live_checks._live_settings(settings_path or _live_checks.SETTINGS_FILE)
    allowed_ip = str(live_settings.get("allowed_egress_ip") or "").strip()
    if not allowed_ip:
        raise BotRefused("live needs live.allowed_egress_ip in config/settings.yaml")

    # Buyer-initiated VWAP per screened symbol, from quote polls. Only used in band mode.
    vwap_book = BuyerVwapBook(readonly, api_key, exchange="NSE")
    def vwap_tick(now):
        symbols = [r["symbol"] for r in _read_screen(cfg.screen_path, now, cfg.max_screen_age_minutes)]
        if symbols:
            vwap_book.poll(symbols, now)

    bot = StrategyBot(
        cfg=cfg,
        vwap_provider=vwap_book.vwap,
        buyer_share_provider=vwap_book.buyer_share,
        vwap_tick=vwap_tick if cfg.pov_mode in ("band", "buyer") else None,
        strategies=[_paper._make_strategy(n, _paper._strategy_params(bot_cfg_path)) for n in cfg.strategies],
        bars=LiveBars(readonly, api_key),
        sender=LiveSender(client),
        audit=audit,
        api_key=api_key,
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
        entry_guard=make_egress_guard(allowed_ip, egress_fn),
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
    args = p.parse_args(argv)

    api_key = OpenAlgoClient.api_key_from_env()
    bot, audit = build_live_bot(
        bot_cfg_path=args.config, limits_path=args.limits, state_dir=args.state_dir,
        base_url=args.base_url, api_key=api_key,
        on_critical=lambda msg: print(f"CRITICAL: {msg}", flush=True),
    )
    _paper.run(bot, audit, state_dir=args.state_dir, clock=now_ist, max_cycles=args.max_cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
