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
from typing import Callable, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.audit.log import AuditLog  # noqa: E402
from quant.core.config import RiskLimits  # noqa: E402
from quant.execution.openalgo_client import OpenAlgoClient, PaperBroker, http_transport  # noqa: E402
from quant.execution.readonly_client import OpenAlgoReadOnly  # noqa: E402
from quant.execution.strategy_bot import BotConfig, BotRefused, PaperSender, StrategyBot  # noqa: E402
from quant.data.live_bars import LiveBars  # noqa: E402
from quant.monitor.paper_loop import now_ist  # noqa: E402
from quant.monitor.safe_mode import SafeMode  # noqa: E402
from quant.notify.alerts import Alerts  # noqa: E402
from quant.risk.controls import HaltHandler  # noqa: E402
from quant.risk.gate import RiskState  # noqa: E402
from quant.risk.kill_switch import KillSwitch  # noqa: E402
from quant.strategies.engulfing.engulfing import Engulfing  # noqa: E402

STOP_FLAG = "stop.flag"
PAPER_SYMBOL_LABEL = "PORTFOLIO"  # the engulfing class needs a symbol; the bot uses the screen's symbols instead


def _make_strategy(name: str):
    if name == "engulfing":
        return Engulfing(PAPER_SYMBOL_LABEL)
    raise BotRefused(f"strategy {name!r} is not wired into the bot runner")


def build_bot(*, bot_cfg_path, limits_path, state_dir, reader, api_key: str,
              paper_capital: float, broker=None, alerts: Optional[Alerts] = None,
              on_critical: Optional[Callable[[str], None]] = None) -> Tuple[StrategyBot, AuditLog]:
    """Wire the bot from config. Raises BotRefused for any mode other than paper."""
    cfg = BotConfig.from_yaml(bot_cfg_path)
    if cfg.mode != "paper":
        raise BotRefused(f"this runner is paper only; config mode is {cfg.mode!r}")
    if cfg.live_enabled:
        raise BotRefused("live_enabled must stay false")
    state_dir = pathlib.Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(state_dir / "audit.jsonl")
    alerts = alerts or Alerts(on_critical=on_critical)
    bot = StrategyBot(
        cfg=cfg,
        strategies=[_make_strategy(n) for n in cfg.strategies],
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
    )
    bot.run_startup_recon(None)  # paper: no broker book to compare, so entries are allowed
    return bot, audit


def run(bot: StrategyBot, audit: AuditLog, *, state_dir, clock: Callable[[], datetime] = now_ist,
        sleep: Callable[[float], None] = time.sleep, max_cycles: Optional[int] = None) -> int:
    """Run cycles until max_cycles or the stop flag. An error in one cycle is logged; the loop goes on."""
    stop = pathlib.Path(state_dir) / STOP_FLAG
    done = 0
    while max_cycles is None or done < max_cycles:
        if stop.exists():
            audit.append("runner_stopped", {"reason": "stop flag", "cycles": done})
            break
        try:
            bot.cycle(clock())
        except BotRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not kill the runner
            audit.append("cycle_error", {"error": repr(exc)})
        done += 1
        if max_cycles is not None and done >= max_cycles:
            break
        for _ in range(int(bot.cfg.interval_minutes * 60)):  # sleep 1 s at a time so STOP is quick
            if stop.exists():
                break
            sleep(1)
    return done


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default=str(ROOT / "config" / "bot.yaml"))
    p.add_argument("--limits", default=str(ROOT / "config" / "risk_limits.yaml"))
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
        on_critical=lambda msg: print(f"CRITICAL: {msg}", flush=True),
    )
    run(bot, audit, state_dir=args.state_dir, max_cycles=args.max_cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
