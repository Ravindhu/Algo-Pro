"""Offline end-to-end check of the analyze path: real OpenAlgoClient, fake OpenAlgo server.

Flow stage: Algo router, Risk gate, Approved order queue, OpenAlgo, Audit log
Status: TESTS (offline precursor to phase 2). This is NOT the sandbox soak. The soak
needs the OpenAlgo analyzer running on the owner's machine during market hours.

What is real: StrategyBot, risk gate, POV executor, AnalyzeSender, OpenAlgoClient
(including its analyzer-on check and fill confirmation), exit manager.
What is fake: the OpenAlgo HTTP transport, the bar feed, and the audit sink.
"""
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from quant.core.config import RiskLimits
from quant.execution.openalgo_client import OpenAlgoClient
from quant.execution.strategy_bot import AnalyzeSender, BotConfig, StrategyBot
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch

NOW = datetime(2026, 11, 17, 10, 15)
LIMITS = Path(__file__).resolve().parents[3] / "config" / "risk_limits.yaml"


class FakeOpenAlgo:
    """Answers the four OpenAlgo endpoints the analyze path uses. Records every call."""

    def __init__(self, analyzer_on=True, exit_fill=97.0):
        self.analyzer_on = analyzer_on
        self.exit_fill = exit_fill
        self.calls = []
        self._n = 0

    def __call__(self, url, payload):
        self.calls.append((url.rsplit("/", 1)[-1], dict(payload)))
        if url.endswith("/analyzer"):
            return {"status": "success", "data": {"analyze_mode": self.analyzer_on}}
        if url.endswith("/placeorder"):
            self._n += 1
            return {"status": "success", "orderid": f"ORD{self._n}"}
        if url.endswith("/orderstatus"):
            # Fill price depends on the side of the order being checked.
            side = self.calls[-2][1]["action"] if len(self.calls) >= 2 else "BUY"
            px = self.exit_fill if side == "SELL" else 100.0
            return {"status": "success", "data": {"order_status": "complete", "average_price": px}}
        raise AssertionError(f"unexpected OpenAlgo call: {url}")

    def placeorders(self):
        return [p for name, p in self.calls if name == "placeorder"]


class ScriptedBars:
    """Each cycle gets a new closed bar. bar_low[i] sets the low of cycle i (default: flat 100)."""

    def __init__(self, lows=None):
        self.lows = lows or []
        self.cycle = 0

    def bars(self, symbol, now):
        self.cycle += 1
        low = self.lows[self.cycle - 1] if self.cycle - 1 < len(self.lows) else 100.0
        n = 40
        idx = [f"bar-{self.cycle}-{i}" for i in range(n)]
        return pd.DataFrame({"open": [100.0] * n, "high": [100.5] * n, "low": [low] * n,
                             "close": [100.0] * n, "volume": [5000.0] * n}, index=idx)

    def daily_stats(self, symbol, now):
        return {"adv_shares": 5_000_000.0, "prev_close": 100.0, "lot_size": 1}


class OneSignal:
    name = "e2e"

    def signal(self, df):
        return {"side": "BUY", "entry": 100.0, "sl_dist": 2.0, "tp_dist": 6.0,
                "time": "T1", "atr": 1.0}


def _limits():
    return RiskLimits.from_yaml(LIMITS)


def _build(tmp_path, fake, bars):
    screen = tmp_path / "latest.json"
    screen.write_text(yaml_free_json(NOW.isoformat()), encoding="utf-8")
    sources = tmp_path / "data_sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": {"live_feed": {"live_allowed": True}}}), encoding="utf-8")
    cfg = BotConfig(mode="analyze", screen_path=str(screen), data_sources_path=str(sources))
    client = OpenAlgoClient("http://x", fake, mode="analyze")
    bot = StrategyBot(
        cfg=cfg, strategies=[OneSignal()], bars=bars, sender=AnalyzeSender(client),
        audit=_Audit(), api_key="KEY", read_funds=lambda: 1_000_000.0, limits=_limits(),
        kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(),
        state=RiskState(),
    )
    bot.run_startup_recon(None)  # analyze: no broker book to compare
    return bot


def yaml_free_json(as_of):
    import json
    return json.dumps({"as_of": as_of, "results": [
        {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]})


class _Audit:
    def append(self, kind, payload):
        return "h"


def test_analyzer_off_sends_no_order(tmp_path):
    """If OpenAlgo's analyzer is not on, the client refuses and no order reaches the server."""
    fake = FakeOpenAlgo(analyzer_on=False)
    bot = _build(tmp_path, fake, ScriptedBars())
    bot.cycle(NOW)
    assert fake.placeorders() == []
    assert bot._positions == {}


def test_entry_fill_then_stop_out_books_loss(tmp_path):
    """Cycle 1 opens a position at the confirmed fill. Cycle 2 stops it out at 98."""
    fake = FakeOpenAlgo(analyzer_on=True, exit_fill=97.0)
    bot = _build(tmp_path, fake, ScriptedBars(lows=[100.0, 97.0]))
    bot.cycle(NOW)
    pos = next(iter(bot._positions.values()))
    assert pos.side == "BUY" and pos.qty > 0 and pos.entry == 100.0
    held = pos.qty

    bot.cycle(NOW)  # low 97 touches the stop at 98
    assert bot._positions == {}
    buys = [p for p in fake.placeorders() if p["action"] == "BUY"]
    sells = [p for p in fake.placeorders() if p["action"] == "SELL"]
    assert buys and sells
    assert sells[-1]["quantity"] == held  # exit never exceeds what is held
    assert bot.state.daily_pnl == (97.0 - 100.0) * held
