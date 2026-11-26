"""Re-check of the 2026-11-26 full audit, items the first fix round missed. Offline.

1. The live clock is timezone-aware IST, bar and screen timestamps are naive IST. A cycle must not crash.
2. An unknown ASM/GSM flag must never be written to the screen as tradable.
8. A negative value must fail an upper-bound rule, not raise.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from quant.core.types import TradeIntent  # noqa: F401  (import check: package loads)
from quant.data.live_bars import is_fresh
from quant.data.screen_rules import Rule
from quant.data.screener_engine import screen_rows
from quant.execution.position_store import PositionStore
from quant.execution.strategy_bot import BotConfig, StrategyBot, read_screen
from quant.core.config import RiskLimits
from quant.monitor.safe_mode import SafeMode
from quant.risk.controls import HaltHandler
from quant.risk.gate import RiskState
from quant.risk.kill_switch import KillSwitch

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parents[3]
LIMITS = ROOT / "config" / "risk_limits.yaml"


def _bars(last_naive):
    idx = pd.DatetimeIndex([last_naive - timedelta(minutes=5), last_naive])
    return pd.DataFrame({"open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
                         "close": [1.0, 1.0], "volume": [1, 1]}, index=idx)


class _Bars:
    """Bars as the live feed hands them over: naive IST index, fresh for the clock the bot is given."""

    def __init__(self):
        self.calls = 0

    def bars(self, sym, now, exchange=None):
        self.calls += 1
        df = _bars(datetime(now.year, now.month, now.day, now.hour, now.minute))
        return df if is_fresh(df, now, 10) else None

    def daily_stats(self, sym, now):
        return None


class _Audit:
    def __init__(self):
        self.rows = []

    def append(self, kind, payload):
        self.rows.append((kind, payload))


def _bot(tmp_path):
    (tmp_path / "data.yaml").write_text("sources:\n  live_feed:\n    live_allowed: true\n", encoding="utf-8")
    screen = tmp_path / "screen.json"
    now_naive = datetime(2026, 11, 26, 10, 0)
    screen.write_text(json.dumps({"as_of": now_naive.isoformat(), "results": [
        {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]}), encoding="utf-8")
    cfg = BotConfig(mode="paper", data_sources_path=str(tmp_path / "data.yaml"), screen_path=str(screen))
    audit = _Audit()
    bot = StrategyBot(
        cfg=cfg, strategies=[], bars=_Bars(), sender=type("S", (), {"bot_mode": "paper"})(),
        audit=audit, api_key="KEY", read_funds=lambda: 1_000_000.0, limits=RiskLimits.from_yaml(LIMITS),
        kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(), state=RiskState(),
        position_store=PositionStore(tmp_path / "pos.json"),
    )
    bot.run_startup_recon(None)   # paper: "skipped" = entries allowed, as the paper runner does
    return bot, audit


# ---- 1: aware IST clock ---------------------------------------------------------------------

def test_cycle_with_an_aware_ist_clock_does_not_raise(tmp_path):
    bot, audit = _bot(tmp_path)
    now = datetime(2026, 11, 26, 10, 0, tzinfo=IST)          # what now_ist() returns in the live runner
    bot.cycle(now)                                             # must not raise
    errors = [p for k, p in audit.rows if k == "cycle_error"]
    assert errors == []


def test_aware_clock_reads_the_screen_and_the_bars(tmp_path):
    bot, audit = _bot(tmp_path)
    bot.cycle(datetime(2026, 11, 26, 10, 0, tzinfo=IST))
    screen_logs = [p for k, p in audit.rows if k == "screen"]
    assert screen_logs and screen_logs[0]["count"] == 1
    assert bot._bars.calls == 1


def test_read_screen_accepts_an_aware_now_and_a_naive_as_of(tmp_path):
    screen = tmp_path / "s.json"
    screen.write_text(json.dumps({"as_of": "2026-11-26T10:00:00", "results": [
        {"symbol": "INFY", "selected": True, "asm_gsm": False, "circuit_pct": 0.2}]}), encoding="utf-8")
    rows = read_screen(screen, datetime(2026, 11, 26, 10, 1, tzinfo=IST), 10)
    assert [r["symbol"] for r in rows] == ["INFY"]


def test_is_fresh_with_aware_now_is_not_a_crash_and_still_checks_age():
    df = _bars(datetime(2026, 11, 26, 10, 0))
    assert is_fresh(df, datetime(2026, 11, 26, 10, 3, tzinfo=IST), 10) is True
    assert is_fresh(df, datetime(2026, 11, 26, 12, 0, tzinfo=IST), 10) is False


# ---- 2: unknown ASM/GSM is never tradable ----------------------------------------------------

def _payload(**flags):
    row = {"symbol": "INFY", "circuit_pct": 0.2}
    row.update(flags)
    return {"candidates": [row]}


def test_unknown_asm_flag_is_dropped_not_written_as_clean():
    out = screen_rows(_payload(asm_gsm=None), as_of=datetime(2026, 11, 26, 10, 0))
    assert out["results"] == []
    assert any("unknown" in d["reason"] for d in out["dropped"])


def test_missing_asm_keys_are_dropped():
    out = screen_rows(_payload(), as_of=datetime(2026, 11, 26, 10, 0))
    assert out["results"] == []


def test_known_clean_flag_is_kept_and_known_flagged_is_dropped():
    clean = screen_rows(_payload(asm_gsm=False), as_of=datetime(2026, 11, 26, 10, 0))
    assert [r["symbol"] for r in clean["results"]] == ["INFY"]
    flagged = screen_rows(_payload(asm_gsm=True), as_of=datetime(2026, 11, 26, 10, 0))
    assert flagged["results"] == []


# ---- 8: a negative ratio fails an upper bound, it does not raise ----------------------------

def test_negative_debt_equity_fails_the_upper_bound_without_raising():
    r = Rule(metric="debt_equity", op="<", threshold=1.0).evaluate({"debt_equity": -2.5})
    assert r.outcome == "fail"


def test_positive_debt_equity_still_passes_and_fails_normally():
    rule = Rule(metric="debt_equity", op="<", threshold=1.0)
    assert rule.evaluate({"debt_equity": 0.5}).outcome == "pass"
    assert rule.evaluate({"debt_equity": 1.5}).outcome == "fail"


def test_whitelisted_negative_metric_is_still_allowed():
    r = Rule(metric="roe", op="<", threshold=0.0).evaluate({"roe": -3.0})
    assert r.outcome == "pass"
