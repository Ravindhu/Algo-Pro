"""Square-off wiring (owner 2026-11-25): no new entries from the cutoff; MIS flattened by the bot.

Flow stage: Risk gate, exits (square-off)
Status: PHASE 1 IMPLEMENTED (paper broker fakes; no live order path)
Reuses the fakes of test_strategy_bot.py. Only the order sender, bar feed and audit are fakes.
"""
import sys
import pathlib
from datetime import datetime, time, timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))  # sibling test module for the fakes

from test_strategy_bot import (  # noqa: E402
    FakeAudit, FakeBars, FakeSender, FixedSignal, _limits, _write_screen, _write_sources,
)
from quant.execution.strategy_bot import BotConfig, StrategyBot  # noqa: E402
from quant.monitor.safe_mode import SafeMode  # noqa: E402
from quant.risk.controls import HaltHandler  # noqa: E402
from quant.risk.gate import RiskState  # noqa: E402
from quant.risk.kill_switch import KillSwitch  # noqa: E402
from quant.risk.squareoff import session_policy  # noqa: E402

MORNING = datetime(2026, 11, 17, 10, 15)
BEFORE = datetime(2026, 11, 17, 15, 14, 59)
CUTOFF = datetime(2026, 11, 17, 15, 15)
AFTER = datetime(2026, 11, 17, 15, 20)


@pytest.fixture(autouse=True)
def _owner_caps_for_tests(tmp_path, monkeypatch):
    """Same test-only caps as test_strategy_bot.py: the real config file is not changed."""
    from quant.core import owner_limits_store as ols
    p = tmp_path / "owner_caps_for_tests.yaml"
    p.write_text("max_order_quantity: 1000000\nmax_order_notional: 100000000\n"
                 "max_open_exposure: 100000000\nblocked: false\n", encoding="utf-8")
    monkeypatch.setattr(ols, "DEFAULT_PATH", p)
    monkeypatch.setattr(ols, "_loaded_from", None)
    monkeypatch.setattr(ols, "_cached", {})
    yield


def _bot(tmp_path, now, sender, strategies=None, **cfg_over):
    screen = _write_screen(tmp_path, as_of=now)
    sources = _write_sources(tmp_path, True)
    cfg = BotConfig(screen_path=str(screen), data_sources_path=str(sources), **cfg_over)
    audit = FakeAudit()
    bot = StrategyBot(
        cfg=cfg, strategies=strategies or [FixedSignal(time="T1")], bars=FakeBars(),
        sender=sender, audit=audit, api_key="KEY", read_funds=lambda: 1_000_000.0,
        limits=_limits(), kill=KillSwitch(tmp_path / "kill.json"), safe=SafeMode(),
        halt=HaltHandler(), state=RiskState(),
    )
    bot.run_startup_recon(None)
    return bot, audit


def _refresh_screen(tmp_path, now):
    _write_screen(tmp_path, as_of=now)


def _exits(sender):
    return [i for i, _ in sender.sent if i.endswith("-EXIT")]


# --- the policy itself -------------------------------------------------------

@pytest.mark.parametrize("now, product, block, flatten", [
    (time(15, 14, 59), "MIS", False, False),
    (time(15, 15), "MIS", True, True),
    (time(15, 20), "MIS", True, True),
    (time(15, 15), "CNC", True, False),   # delivery: block entries only, never flatten
    (time(9, 30), "MIS", False, False),
    (time(15, 15), "??", True, False),    # unknown product: block entries, no flatten
])
def test_session_policy_matrix(now, product, block, flatten):
    assert session_policy(now, product) == {"block_entries": block, "flatten": flatten}


def test_session_policy_fails_closed_on_bad_time():
    assert session_policy("15:20", "MIS") == {"block_entries": True, "flatten": False}


# --- config ------------------------------------------------------------------

def test_product_defaults_to_cnc_and_unknown_products_refused():
    assert BotConfig().product == "CNC"
    assert BotConfig(product="MIS").product == "MIS"
    with pytest.raises(ValueError, match="MIS, CNC or auto"):
        BotConfig(product="NRML")


@pytest.mark.parametrize("bad", ["15.15", "25:00", "abc", "15:60"])
def test_bad_square_off_time_refused(bad):
    with pytest.raises(ValueError, match="square_off_time"):
        BotConfig(square_off_time=bad)


# --- the bot across the cutoff -----------------------------------------------

def test_entry_allowed_before_cutoff(tmp_path):
    sender = FakeSender()
    bot, _ = _bot(tmp_path, BEFORE, sender)
    bot.cycle(BEFORE)
    assert any(not i.endswith("-EXIT") for i, _ in sender.sent)


def test_no_new_entry_at_or_after_cutoff(tmp_path):
    sender = FakeSender()
    bot, audit = _bot(tmp_path, CUTOFF, sender)
    bot.cycle(CUTOFF)
    assert not sender.sent
    assert any(k == "parent_skipped" for k in audit.kinds())


def test_mis_position_flattened_at_cutoff(tmp_path):
    sender = FakeSender()
    bot, _ = _bot(tmp_path, MORNING, sender, product="MIS")
    bot.cycle(MORNING)                      # entry opened in the morning
    assert len(sender.sent) >= 1 and not _exits(sender)
    _refresh_screen(tmp_path, AFTER)
    bot.cycle(AFTER)                        # past the cutoff: must close the MIS position
    assert _exits(sender), "MIS position was not flattened at square-off"


class RecordingSender(FakeSender):
    """Records the product carried on each child order intent."""

    def __init__(self):
        super().__init__()
        self.products = []

    def place_child(self, intent, api_key, decision, price_type="MARKET", limit_price=None):
        self.products.append(intent.product)
        return super().place_child(intent, api_key, decision, price_type=price_type, limit_price=limit_price)


def test_cnc_orders_carry_cnc_product(tmp_path):
    sender = RecordingSender()
    bot, _ = _bot(tmp_path, MORNING, sender)  # default config: CNC
    bot.cycle(MORNING)
    assert sender.products and set(sender.products) == {"CNC"}


def test_mis_orders_carry_mis_product(tmp_path):
    sender = RecordingSender()
    bot, _ = _bot(tmp_path, MORNING, sender, product="MIS")
    bot.cycle(MORNING)
    assert sender.products and set(sender.products) == {"MIS"}


def test_cnc_holds_position_past_cutoff(tmp_path):
    sender = FakeSender()
    bot, _ = _bot(tmp_path, MORNING, sender)  # CNC
    bot.cycle(MORNING)
    _refresh_screen(tmp_path, AFTER)
    bot.cycle(AFTER)
    assert not _exits(sender), "CNC position must not be flattened at the cutoff"


def test_cnc_refuses_short_entries(tmp_path):
    sender = FakeSender()
    bot, audit = _bot(tmp_path, MORNING, sender, strategies=[FixedSignal(time="T1", side="SELL")])
    bot.cycle(MORNING)
    assert not sender.sent, "no short order may be sent"
    # The bot is long-only already (long_only_refused); the CNC guard is a second line.
    assert any(k in ("long_only_refused", "parent_skipped") for k in audit.kinds())


# --- the order client sends the product it is given --------------------------

from quant.execution.openalgo_client import _order_product  # noqa: E402


@pytest.mark.parametrize("given, sent", [("CNC", "CNC"), ("mis", "MIS"), (" Cnc ", "CNC")])
def test_order_client_sends_cnc_or_mis(given, sent):
    assert _order_product(given) == sent


@pytest.mark.parametrize("bad", ["NRML", "", None, "MARGIN"])
def test_order_client_refuses_unknown_product(bad):
    # Fail closed: an unknown product is never sent to the broker.
    with pytest.raises(ValueError, match="MIS, CNC or MTF"):
        _order_product(bad)


# --- owner 2026-11-25: MIS squared before 15:15; CNC held overnight -----------

@pytest.mark.parametrize("now, product, block, flatten", [
    (time(15, 9, 59), "MIS", False, False),
    (time(15, 10), "MIS", True, True),       # MIS: flatten and no new entries from 15:10
    (time(15, 14, 59), "MIS", True, True),
    (time(15, 12), "CNC", False, False),     # CNC: no flatten; entries only from 15:15
    (time(15, 15), "CNC", True, False),
])
def test_policy_with_mis_flatten_at_1510(now, product, block, flatten):
    policy = session_policy(now, product, time(15, 15), time(15, 10))
    assert policy == {"block_entries": block, "flatten": flatten}


def test_mis_flatten_time_must_not_be_after_entry_cutoff():
    with pytest.raises(ValueError, match="mis_flatten_time"):
        BotConfig(mis_flatten_time="15:20", square_off_time="15:15")


def test_mis_is_squared_off_at_1510_not_at_1515(tmp_path):
    sender = FakeSender()
    bot, _ = _bot(tmp_path, MORNING, sender, product="MIS")
    bot.cycle(MORNING)
    _refresh_screen(tmp_path, datetime(2026, 11, 17, 15, 12))
    bot.cycle(datetime(2026, 11, 17, 15, 12))
    assert _exits(sender), "MIS must be squared off before 15:15"


def test_cnc_is_not_squared_off_at_1512(tmp_path):
    sender = FakeSender()
    bot, _ = _bot(tmp_path, MORNING, sender)  # CNC
    bot.cycle(MORNING)
    _refresh_screen(tmp_path, datetime(2026, 11, 17, 15, 12))
    bot.cycle(datetime(2026, 11, 17, 15, 12))
    assert not _exits(sender), "CNC is held overnight, not squared off"


def test_cnc_position_survives_to_next_day_and_entries_reopen(tmp_path):
    day1_close = datetime(2026, 11, 17, 15, 20)
    day2_open = datetime(2026, 11, 18, 10, 15)
    sender = FakeSender()
    bot, _ = _bot(tmp_path, MORNING, sender)  # CNC
    bot.cycle(MORNING)
    _refresh_screen(tmp_path, day1_close)
    bot.cycle(day1_close)
    assert len(bot._positions) == 1 and not _exits(sender)  # held overnight
    _refresh_screen(tmp_path, day2_open)
    bot.cycle(day2_open)
    assert len(bot._positions) == 1, "the held CNC position must still be tracked next day"
    assert bot._policy["block_entries"] is False, "entries reopen on the next trading day"
