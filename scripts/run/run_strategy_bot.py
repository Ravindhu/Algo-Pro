"""Strategy bot runner: config/bot.yaml -> StrategyBot -> one cycle every interval_minutes. PAPER ONLY.

Usage:
  python scripts/run/run_strategy_bot.py --base-url http://127.0.0.1:5000 --paper-capital 100000
  Stop: create the file <state-dir>/stop.flag (the loop checks it every second).

Needs OPENALGO_API_KEY in the environment or the repo-root .env.
Paper capital is simulated. With --paper-capital 0 (the default) no entries are approved.
This runner refuses analyze and live mode. The bot itself also refuses live (live_enabled must stay false).
Flow stage: Algo router, Risk gate, Approved order queue, Audit log
Status: PHASE 3 IMPLEMENTED (paper only; the data source must be approved in config/data_sources.yaml)
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
from datetime import datetime
from typing import Any, Callable, Optional, Tuple

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.audit.log import AuditLog  # noqa: E402
from quant.core.config import RiskLimits  # noqa: E402
from quant.execution.openalgo_client import OpenAlgoClient, PaperBroker, http_transport  # noqa: E402
from quant.execution.readonly_client import OpenAlgoReadOnly  # noqa: E402
from quant.execution.strategy_bot import load_bot_config, BotConfig, BotRefused, PaperSender, StrategyBot  # noqa: E402
from quant.data.live_bars import LiveBars  # noqa: E402
from quant.data.mtf_bars import MultiTimeframeBars  # noqa: E402
from quant.core.setup_store import load_timeframes  # noqa: E402
from quant.engines.alpha.strategies.engulf_sequence_strategy import SequenceStrategy  # noqa: E402
from quant.engines.alpha.strategies.owner_patterns import SEQUENCE_KINDS  # noqa: E402
from quant.monitor.paper_loop import now_ist  # noqa: E402
from quant.monitor.safe_mode import SafeMode  # noqa: E402
from quant.notify.alerts import Alerts  # noqa: E402
from quant.risk.controls import HaltHandler  # noqa: E402
from quant.risk.gate import RiskState  # noqa: E402
from quant.risk.kill_switch import KillSwitch  # noqa: E402

STOP_FLAG = "stop.flag"


def _strategy_params(bot_cfg_path) -> dict:
    """Read strategies.<name>.params from config/bot.yaml. Empty dict when absent.

    Schema:
      strategies:
        - ocl
          params:
            symbol: RELIANCE
            htf_interval: 5m
    Unknown top-level keys are already refused by BotConfig.from_yaml; the params block is
    read separately here so the dataclass schema stays untouched.
    """
    try:
        data = yaml.safe_load(pathlib.Path(bot_cfg_path).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out = {}
    for entry in data.get("strategies", []):
        if isinstance(entry, str):
            out.setdefault(entry, {})
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
            out[entry["name"]] = entry.get("params") or {}
    return out
PAPER_SYMBOL_LABEL = "PORTFOLIO"  # the engulfing class needs a symbol; the bot uses the screen's symbols instead


def _make_strategy(name: str, strategy_params: Optional[dict] = None, feed=None,
                   equity: Optional[float] = None, loss_ceiling: Optional[float] = None):
    if name in SEQUENCE_KINDS:
        if feed is None:
            raise BotRefused(f"{name} needs MTF, HTF and LTF set in the trade setup; the runner does not start without them")
        from quant.engines.alpha.strategies.risk_ladder import load_cycle_settings
        cyc = load_cycle_settings(ROOT / "config" / "settings.yaml")
        return SequenceStrategy(feed, name, SEQUENCE_KINDS[name], target_r=cyc["target_r"],
                                equity=equity, loss_ceiling=loss_ceiling,
                                symbol_allocation_pct=cyc["symbol_allocation_pct"],
                                aggressive_share=cyc["aggressive_share"])
    raise BotRefused(f"strategy {name!r} is not wired into the bot runner")


def build_bot(*, bot_cfg_path, limits_path, state_dir, reader, api_key: str,
              paper_capital: float, broker=None, alerts: Optional[Alerts] = None,
              on_critical: Optional[Callable[[str], None]] = None,
              timeframes: Optional[dict] = None) -> Tuple[StrategyBot, AuditLog]:
    """Wire the bot from config. Raises BotRefused for any mode other than paper."""
    cfg = load_bot_config(bot_cfg_path)
    if cfg.mode != "paper":
        raise BotRefused(f"this runner is paper only; config mode is {cfg.mode!r}")
    if cfg.live_enabled:
        raise BotRefused("live_enabled must stay false")
    strategy_params = _strategy_params(bot_cfg_path)
    from quant.engines.alpha.strategies.risk_ladder import load_cycle_settings as _load_cycle
    cycle_cfg = _load_cycle(ROOT / "config" / "settings.yaml")  # owner N and loss ceiling (None = not set)
    tf = timeframes or {}
    feed = None
    if all(tf.get(k) for k in ("mtf", "htf", "ltf")):
        feed = MultiTimeframeBars(reader, api_key, {k: tf[k] for k in ("mtf", "htf", "ltf")})
    state_dir = pathlib.Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(state_dir / "audit.jsonl")
    alerts = alerts or Alerts(on_critical=on_critical)
    bot = StrategyBot(
        cfg=cfg,
        strategies=[_make_strategy(n, strategy_params, feed, equity=float(paper_capital),
                                   loss_ceiling=cycle_cfg["loss_ceiling"]) for n in cfg.strategies],
        bars=LiveBars(reader, api_key),
        sender=PaperSender(broker or PaperBroker()),
        audit=audit,
        api_key=api_key,
        read_funds=lambda: float(paper_capital),
        limits=RiskLimits.from_yaml(limits_path),
        kill=KillSwitch(state_dir / "kill.json"),
        safe=SafeMode(audit),
        halt=HaltHandler(),
        state=RiskState(),
        alerts=alerts,
        cycle_loss_ceiling=cycle_cfg["loss_ceiling"],
        cycle_portfolio_loss_ceiling=cycle_cfg["portfolio_loss_ceiling"],
        cycle_symbol_allocation_pct=cycle_cfg["symbol_allocation_pct"],
    )
    bot.run_startup_recon(None)  # paper: no broker book to compare, so entries are allowed
    return bot, audit


CYCLE_ERROR_ALERT_AFTER = 3


def _escalate_cycle_failures(bot, failures: int, exc: Exception) -> None:
    """Repeated cycle errors: CRITICAL alert, and entries blocked by the kill switch. Exits still run each cycle."""
    msg = f"{failures} cycles in a row failed ({exc!r}); new entries blocked, check exits and positions"
    alert = getattr(bot, "_alert", None)
    if callable(alert):
        alert("CRITICAL", msg)
    kill = getattr(bot, "_kill", None)
    if kill is not None and not kill.active:
        kill.trip(f"repeated cycle failures: {failures}", source="automatic")


SCREEN_REFRESH_EVERY_SECONDS = 300  # the bot refuses a screen older than 10 minutes; refresh every 5


def run(bot: StrategyBot, audit: AuditLog, *, state_dir, clock: Callable[[], datetime] = now_ist,
        sleep: Callable[[float], None] = time.sleep, max_cycles: Optional[int] = None,
        refresh: Optional[Callable[[datetime], Any]] = None,
        refresh_every_seconds: float = SCREEN_REFRESH_EVERY_SECONDS) -> int:
    """Run cycles until max_cycles or the stop flag. An error in one cycle is logged; the loop goes on.

    refresh (optional): called before a cycle when refresh_every_seconds have passed since the last
    refresh (and once at the start). It rewrites the screen file. A refresh error is audited and never
    stops the loop; the screen then goes stale and the bot stops NEW entries after its 10-minute limit.
    """
    stop = pathlib.Path(state_dir) / STOP_FLAG
    done = 0
    failures = 0  # consecutive cycles that raised (audit fix 3, 2026-11-26)
    last_refresh: Optional[datetime] = None
    while max_cycles is None or done < max_cycles:
        if stop.exists():
            audit.append("runner_stopped", {"reason": "stop flag", "cycles": done})
            break
        if refresh is not None:
            now = clock()
            if last_refresh is None or (now - last_refresh).total_seconds() >= refresh_every_seconds:
                last_refresh = now
                try:
                    audit.append("screen_refresh", {"result": refresh(now)})
                except Exception as exc:  # noqa: BLE001 - a refresh problem must not stop the bot
                    audit.append("screen_refresh_error", {"error": repr(exc)})
        try:
            bot.cycle(clock())
            failures = 0
        except BotRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not kill the runner
            failures += 1
            audit.append("cycle_error", {"error": repr(exc), "consecutive": failures})
            if failures % CYCLE_ERROR_ALERT_AFTER == 0:
                _escalate_cycle_failures(bot, failures, exc)
        done += 1
        if max_cycles is not None and done >= max_cycles:
            break
        tick = getattr(bot, "vwap_tick", None)
        poll_s = max(1, int(bot.cfg.vwap_poll_seconds))
        for i in range(int(bot.cfg.interval_minutes * 60)):  # sleep 1 s at a time so STOP is quick
            if stop.exists():
                break
            if tick is not None and i % poll_s == 0:
                try:
                    tick(clock())
                except Exception as exc:  # noqa: BLE001 - a quote problem must not stop the bot
                    audit.append("vwap_tick_error", {"error": repr(exc)})
            peg_tick = getattr(bot, "peg_slicer_tick", None)
            if peg_tick is not None and i % poll_s == 0:
                try:
                    peg_tick(clock())
                except Exception as exc:  # noqa: BLE001 - one peg problem must not stop the bot
                    audit.append("peg_tick_error", {"error": repr(exc)})
            sleep(1)
    return done


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default=str(ROOT / "config" / "bot.yaml"))
    p.add_argument("--limits", default=str(ROOT / "config" / "risk_limits.yaml"))
    p.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    p.add_argument("--state-dir", default=str(ROOT / "data" / "bot"))
    p.add_argument("--base-url", required=True, help="OpenAlgo server URL (read-only history)")
    p.add_argument("--paper-capital", type=float, default=0.0, help="simulated capital in INR")
    p.add_argument("--max-cycles", type=int, default=None)
    args = p.parse_args(argv)

    api_key = OpenAlgoClient.api_key_from_env()
    reader = OpenAlgoReadOnly(args.base_url, http_transport)
    bot, audit = build_bot(
        bot_cfg_path=args.config, limits_path=args.limits, state_dir=args.state_dir, reader=reader,
        api_key=api_key, paper_capital=args.paper_capital,
        timeframes=load_timeframes(args.settings),
        on_critical=lambda msg: print(f"CRITICAL: {msg}", flush=True),
    )
    run(bot, audit, state_dir=args.state_dir, max_cycles=args.max_cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
