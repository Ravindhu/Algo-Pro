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

def test_cnc_product_refused_until_order_plumbing_exists():
    with pytest.raises(ValueError, match="CNC"):
        BotConfig(product="CNC")


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
    bot, _ = _bot(tmp_path, MORNING, sender)
    bot.cycle(MORNING)                      # entry opened in the morning
    assert len(sender.sent) >= 1 and not _exits(sender)
    _refresh_screen(tmp_path, AFTER)
    bot.cycle(AFTER)                        # past the cutoff: must close the MIS position
    assert _exits(sender), "MIS position was not flattened at square-off"


def test_cnc_policy_would_hold_position_past_cutoff(tmp_path):
    # Product CNC is refused by config, so this checks the policy the bot would use for CNC.
    assert session_policy(AFTER.time(), "CNC")["flatten"] is False
