"""Margin entries in the strategy bot (owner 2026-11-25). Paper fakes only; no broker call.

Flow stage: Risk gate (entry product, leverage check), exits and broker stops
Status: PHASE 2 IMPLEMENTED
Reuses the fakes of test_strategy_bot.py. The broker margin is a fake function.
"""
import sys
import pathlib
from datetime import datetime

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from test_strategy_bot import (  # noqa: E402
    FakeAudit, FakeBars, FakeSender, FixedSignal, _limits, _write_screen, _write_sources,
)
from quant.core.types import TradeIntent  # noqa: E402
from quant.execution.exit_manager import Position  # noqa: E402
from quant.execution.strategy_bot import BotConfig, StrategyBot, _Parent  # noqa: E402
from quant.monitor.safe_mode import SafeMode  # noqa: E402
from quant.risk.controls import HaltHandler  # noqa: E402
from quant.risk.gate import RiskState  # noqa: E402
from quant.risk.kill_switch import KillSwitch  # noqa: E402
from quant.risk.margin_entry import MarginSettings  # noqa: E402

NOW = datetime(2026, 11, 25, 10, 0)
MARGIN = MarginSettings(enabled=True, limit_inr=500_000.0, max_leverage=5.0)


@pytest.fixture(autouse=True)
def _owner_caps_for_tests(tmp_path, monkeypatch):
    from quant.core import owner_limits_store as ols
    p = tmp_path / "owner_caps_for_tests.yaml"
    p.write_text("max_order_quantity: 1000000\nmax_order_notional: 100000000\n"
                 "max_open_exposure: 100000000\nblocked: false\n", encoding="utf-8")
    monkeypatch.setattr(ols, "DEFAULT_PATH", p)
    monkeypatch.setattr(ols, "_loaded_from", None)
    monkeypatch.setattr(ols, "_cached", {})
    yield


def _honest_margin(intent):
    """Broker reply at 5x leverage: margin is a fifth of the full value."""
    return {"status": "success", "data": {"total_margin_required": intent.qty * intent.ref_price / 5.0}}


def _bot(tmp_path, margin=MARGIN, product="CNC", margin_check=_honest_margin):
    screen = _write_screen(tmp_path, as_of=NOW)
    sources = _write_sources(tmp_path, True)
    cfg = BotConfig(screen_path=str(screen), data_sources_path=str(sources), product=product, margin=margin)
    bot = StrategyBot(
        cfg=cfg, strategies=[FixedSignal(time="T1")], bars=FakeBars(), sender=FakeSender(),
        audit=FakeAudit(), api_key="KEY", read_funds=lambda: 1_000_000.0, limits=_limits(),
        kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(), halt=HaltHandler(), state=RiskState(),
        margin_check=margin_check,
    )
    bot.run_startup_recon(None)
    return bot


def _intent(symbol="INFY", qty=100, price=1000.0, product="CNC", side="BUY"):
    return TradeIntent(intent_id=f"{symbol}-IN", strategy_id="s", symbol=symbol, side=side, qty=qty,
                       ref_price=price, kind="entry", prev_close=None, trade_day=NOW.date(), product=product)


def _pos(key="INFY|s", qty=100, entry=1000.0, product=""):
    return Position(key=key, symbol="INFY", side="BUY", qty=qty, entry=entry, stop=950.0, target=None,
                    atr=10.0, strategy="s", product=product)


# --- entry product -----------------------------------------------------------

def test_buy_entry_with_verified_broker_margin_gets_mtf(tmp_path):
    assert _bot(tmp_path)._entry_product(_intent())[0] == "MTF"


def test_broker_margin_equal_to_value_keeps_cnc(tmp_path):
    bot = _bot(tmp_path, margin_check=lambda i: {"status": "success", "data": {"total_margin_required": 100000.0}})
    assert bot._entry_product(_intent())[0] == "CNC"


def test_broker_margin_implying_high_leverage_keeps_cnc(tmp_path):
    bot = _bot(tmp_path, margin_check=lambda i: {"status": "success", "data": {"total_margin_required": 1000.0}})
    product, reason = bot._entry_product(_intent())
    assert product == "CNC" and "above the maximum" in reason


def test_broker_check_error_keeps_cnc(tmp_path):
    bot = _bot(tmp_path, margin_check=lambda i: {"status": "error", "message": "x"})
    assert bot._entry_product(_intent())[0] == "CNC"


def test_broker_check_exception_keeps_cnc_and_names_no_secret(tmp_path):
    def boom(i):
        raise RuntimeError("secret-token-value")
    bot = _bot(tmp_path, margin_check=boom)
    product, reason = bot._entry_product(_intent())
    assert product == "CNC" and "RuntimeError" in reason and "secret-token-value" not in reason


def test_no_broker_check_configured_keeps_cnc(tmp_path):
    bot = _bot(tmp_path, margin_check=None)
    assert bot._entry_product(_intent())[0] == "CNC"


def test_broker_check_receives_mtf_product(tmp_path):
    seen = []

    def record(i):
        seen.append(i.product)
        return _honest_margin(i)
    bot = _bot(tmp_path, margin_check=record)
    bot._entry_product(_intent(product="CNC"))
    assert seen == ["MTF"]


def test_margin_not_configured_leaves_cnc(tmp_path):
    bot = _bot(tmp_path, margin=None)
    assert bot._entry_product(_intent())[0] == "CNC"


def test_margin_off_never_calls_broker(tmp_path):
    calls = []
    off = MarginSettings(enabled=False, limit_inr=500_000.0, max_leverage=5.0)
    bot = _bot(tmp_path, margin=off, margin_check=lambda i: calls.append(i) or _honest_margin(i))
    assert bot._entry_product(_intent())[0] == "CNC"
    assert calls == []


def test_intraday_bot_never_calls_broker(tmp_path):
    calls = []
    bot = _bot(tmp_path, product="MIS", margin_check=lambda i: calls.append(i) or _honest_margin(i))
    assert bot._entry_product(_intent(product="MIS"))[0] == "MIS"
    assert calls == []


# --- borrowed tally ----------------------------------------------------------

def test_open_mtf_position_counts_toward_limit(tmp_path):
    bot = _bot(tmp_path)
    bot._positions["INFY|s"] = _pos(qty=400, entry=1000.0, product="MTF")   # 400,000 borrowed
    assert bot._mtf_borrowed_inr() == pytest.approx(400_000.0)
    assert bot._entry_product(_intent(qty=100))[0] == "MTF"                 # 500,000 is exactly the limit
    assert bot._entry_product(_intent(qty=101))[0] == "CNC"                 # 501,000 is over


def test_cnc_position_does_not_count(tmp_path):
    bot = _bot(tmp_path)
    bot._positions["INFY|s"] = _pos(qty=400, entry=1000.0, product="CNC")
    assert bot._mtf_borrowed_inr() == 0.0


def test_unfilled_mtf_parent_counts_toward_limit(tmp_path):
    bot = _bot(tmp_path)
    bot._parents["INFY|p"] = _Parent(key="INFY|p", intent=_intent(qty=400, product="MTF"),
                                     remaining=400, circuit_pct=10.0)
    assert bot._mtf_borrowed_inr() == pytest.approx(400_000.0)
    assert bot._entry_product(_intent(qty=101))[0] == "CNC"


# --- product kept on the position: exits and broker stops ----------------------

def test_old_position_without_product_uses_bot_product(tmp_path):
    bot = _bot(tmp_path, product="CNC")
    assert bot._product_of(_pos(product="")) == "CNC"
    assert bot._product_of(_pos(product="MTF")) == "MTF"


def test_exit_of_mtf_position_sends_mtf(tmp_path, monkeypatch):
    bot = _bot(tmp_path)
    bot._positions["INFY|s"] = _pos(product="MTF")
    sent = []

    def capture(intent):
        sent.append(intent)
        raise RuntimeError("stop here: the test only needs the intent")
    monkeypatch.setattr(bot._executor, "submit_child", capture)
    bot._send_exit("INFY|s", bot._positions["INFY|s"], 990.0, "stop", NOW, {"skipped": []})
    assert sent and sent[0].product == "MTF"


def test_broker_stop_of_mtf_position_uses_mtf(tmp_path):
    bot = _bot(tmp_path)
    calls = []

    class FakeStops:
        def place_stop(self, symbol, action, qty, trigger, strategy, product="MIS"):
            calls.append(product)
            return {"status": "error", "reason": "test"}
    bot._stops = FakeStops()
    bot._positions["INFY|s"] = _pos(product="MTF")
    bot._sync_broker_stop("INFY|s")
    assert calls == ["MTF"]


# --- full gate path ----------------------------------------------------------

def test_gate_entry_returns_mtf_intent_when_leverage_verified(tmp_path):
    bot = _bot(tmp_path)
    bot._funds = 1_000_000.0  # set by the cycle in a live run
    sig = {"side": "BUY", "entry": 1000.0, "sl_dist": 10.0, "tp_dist": 20.0, "time": "T1", "atr": 10.0}
    stats = {"lot_size": 1, "adv_shares": 1_000_000.0, "prev_close": 999.0}
    intent = bot._gate_entry("INFY|s|BUY|T1", sig, FixedSignal(time="T1"), "INFY", 10.0, stats, NOW,
                             ref_price=1000.0, risk_fraction=0.0025, equity=1_000_000.0)
    assert intent is not None
    assert intent.product == "MTF"


def test_gate_entry_returns_cnc_intent_when_leverage_refused(tmp_path):
    # the broker reports margin equal to the full value of the order the gate actually sized
    bot = _bot(tmp_path, margin_check=lambda i: {"status": "success", "data": {"total_margin_required": i.qty * i.ref_price}})
    bot._funds = 1_000_000.0
    sig = {"side": "BUY", "entry": 1000.0, "sl_dist": 10.0, "tp_dist": 20.0, "time": "T1", "atr": 10.0}
    stats = {"lot_size": 1, "adv_shares": 1_000_000.0, "prev_close": 999.0}
    intent = bot._gate_entry("INFY|s|BUY|T1", sig, FixedSignal(time="T1"), "INFY", 10.0, stats, NOW,
                             ref_price=1000.0, risk_fraction=0.0025, equity=1_000_000.0)
    assert intent is not None
    assert intent.product == "CNC"
