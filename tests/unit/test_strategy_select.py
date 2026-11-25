"""Dashboard strategy choice writes the bot's strategy list (bot.yaml), keeping the file intact.

Status: TESTS WRITTEN (2026-11-24). Plan: docs/IMPLEMENTATION_PLAN_2026-11-24_STRATEGY_SELECT.md
"""
import pathlib
import shutil

import pytest

from quant.core import setup_store as ss
from quant.execution.strategy_bot import load_bot_config

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIVE = ["engulfing", "engulfing_fail", "ocl_seq", "ocl_sell_fail_top_seq", "ocl_sell_fail_bottom_seq"]


@pytest.fixture
def cfg_dir(tmp_path):
    shutil.copy(ROOT / "config" / "bot.yaml", tmp_path / "bot.yaml")
    return tmp_path


def _paths(d):
    return d / "settings.yaml", d / "strategies", d / "risk_limits.yaml"


def test_offers_exactly_the_five_bot_strategies():
    assert list(ss.BOT_STRATEGIES) == FIVE


def test_reads_the_list_from_bot_yaml(cfg_dir):
    assert ss.read_bot_strategies(cfg_dir / "bot.yaml") == FIVE


def test_saving_a_subset_writes_bot_yaml_and_keeps_comments(cfg_dir):
    settings, strategies, limits = _paths(cfg_dir)
    before = (cfg_dir / "bot.yaml").read_text(encoding="utf-8")
    ss.apply_setup_updates(settings, strategies, limits, {"strategies": ["engulfing", "ocl_seq"]})
    text = (cfg_dir / "bot.yaml").read_text(encoding="utf-8")
    assert ss.read_bot_strategies(cfg_dir / "bot.yaml") == ["engulfing", "ocl_seq"]
    assert "# Strategy list." in text and "exits: {}" in text
    assert text.count("  - ") == before.count("  - ") - 3  # five names became two
    from quant.execution.strategy_bot import BotConfig
    assert BotConfig.from_yaml(cfg_dir / "bot.yaml").strategies == ("engulfing", "ocl_seq")


def test_read_setup_reports_selection_and_available(cfg_dir):
    settings, strategies, limits = _paths(cfg_dir)
    data = ss.read_setup(settings, strategies, limits)
    assert data["bot_strategies"] == FIVE
    assert data["available_bot_strategies"] == FIVE


@pytest.mark.parametrize("bad", [[], ["engulfing", "engulfing"], ["engulfing", "not_a_strategy"], "engulfing"])
def test_refused_selection_leaves_bot_yaml_unchanged(cfg_dir, bad):
    settings, strategies, limits = _paths(cfg_dir)
    before = (cfg_dir / "bot.yaml").read_bytes()
    with pytest.raises(ValueError):
        ss.apply_setup_updates(settings, strategies, limits, {"strategies": bad})
    assert (cfg_dir / "bot.yaml").read_bytes() == before


def test_live_switch_is_not_touched(cfg_dir):
    settings, strategies, limits = _paths(cfg_dir)
    before = (cfg_dir / "bot.yaml").read_text(encoding="utf-8")
    ss.apply_setup_updates(settings, strategies, limits, {"strategies": ["engulfing"]})
    after = (cfg_dir / "bot.yaml").read_text(encoding="utf-8")
    assert "live_enabled: false" in after and "mode: paper" in after
    assert [ln for ln in before.splitlines() if ln.startswith(("mode:", "live_enabled:"))] == \
           [ln for ln in after.splitlines() if ln.startswith(("mode:", "live_enabled:"))]


def test_pin_ignores_bytecode_caches(tmp_path):
    """Running code writes __pycache__; the upgrade pin must not move because of it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("upgrade_check", ROOT / "scripts/verify/upgrade_check.py")
    uc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(uc)
    oa = tmp_path / "openalgo"
    (oa / "install" / "lib").mkdir(parents=True)
    (oa / "install" / "x.sh").write_text("echo hi\n", encoding="utf-8")
    before = uc.fingerprint(oa)["_rolled"]
    (oa / "install" / "lib" / "__pycache__").mkdir()
    (oa / "install" / "lib" / "__pycache__" / "m.cpython-313.pyc").write_bytes(b"\x00\x01")
    assert uc.fingerprint(oa)["_rolled"] == before
