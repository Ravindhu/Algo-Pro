"""Order product from the MTF timeframe (owner 2026-11-25): MTF below 1d -> MIS, 1d or longer -> CNC.

Flow stage: Risk gate (product selection)
Status: PHASE 1 IMPLEMENTED (config and pure rule; no broker call)
"""
import sys
import pathlib

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from quant.execution.strategy_bot import BotRefused, load_bot_config  # noqa: E402
from quant.risk.squareoff import product_for_mtf  # noqa: E402


@pytest.mark.parametrize("mtf, product", [
    ("1m", "MIS"), ("5m", "MIS"), ("15m", "MIS"), ("75m", "MIS"), ("125m", "MIS"), ("1h", "MIS"),
    ("1d", "CNC"), ("1w", "CNC"), ("1M", "CNC"),
])
def test_mtf_below_daily_is_mis_daily_or_longer_is_cnc(mtf, product):
    assert product_for_mtf(mtf) == product


@pytest.mark.parametrize("bad", [None, "", "15", "1x", "fifteen", 5])
def test_unknown_or_missing_mtf_fails_closed(bad):
    with pytest.raises(ValueError):
        product_for_mtf(bad)


def _configs(tmp_path, product, mtf):
    bot = tmp_path / "bot.yaml"
    bot.write_text(f"product: {product}\n", encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    tf = {} if mtf is None else {"timeframes": {"ltf": "1m", "mtf": mtf, "htf": "1d"}}
    settings.write_text(yaml.safe_dump({"trade_setup": tf} if tf else {}), encoding="utf-8")
    return bot, settings


def test_auto_resolves_to_mis_for_intraday_mtf(tmp_path):
    bot, settings = _configs(tmp_path, "auto", "15m")
    assert load_bot_config(bot, settings).product == "MIS"


def test_auto_resolves_to_cnc_for_daily_mtf(tmp_path):
    bot, settings = _configs(tmp_path, "auto", "1d")
    assert load_bot_config(bot, settings).product == "CNC"


def test_auto_without_saved_mtf_refuses_to_load(tmp_path):
    bot, settings = _configs(tmp_path, "auto", None)
    with pytest.raises(ValueError, match="MTF"):
        load_bot_config(bot, settings)


def test_explicit_product_does_not_need_mtf(tmp_path):
    bot, settings = _configs(tmp_path, "CNC", None)
    assert load_bot_config(bot, settings).product == "CNC"


def test_bot_refuses_unresolved_auto(tmp_path):
    from test_square_off_wiring import _bot, FakeSender  # noqa: E402  (same folder)
    from test_square_off_wiring import MORNING  # noqa: E402
    with pytest.raises(BotRefused, match="auto"):
        _bot(tmp_path, MORNING, FakeSender(), product="auto")
